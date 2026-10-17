"""service.match: find Discogs candidates, review them, choose or skip."""

from __future__ import annotations

import json

from ..match import AUTO_MIN_MARGIN, AUTO_MIN_SCORE, TITLE_MATCH_MIN, score_result
from ._common import album_record, set_status
from ._planning import album_target, resolve_template
from ._releases import (
    build_candidates,
    candidate_row,
    fetch_release,
    parse_release_ref,
    provider_call,
)
from ._state import events, state
from ._types import (
    AutoRules,
    Candidate,
    Destination,
    Job,
    JobKind,
    MatchStatus,
    NotFound,
    ProviderError,
    ValidationError,
)

SEARCH_KEEP = 15  # search results kept per album
FETCH_TOP = 4  # full releases fetched in the background per album
KEEP_STATUS = (MatchStatus.ACCEPTED, MatchStatus.HELD, MatchStatus.APPLIED)  # find() refreshes candidates only


def auto_rules() -> AutoRules:
    """The exact rules discogs-tag --auto uses."""
    return AutoRules(min_score=AUTO_MIN_SCORE, min_margin=AUTO_MIN_MARGIN, require_track_count=True,
                     title_match_min=TITLE_MATCH_MIN)


def auto_accept(candidates: list[Candidate], rules: AutoRules | None = None,
                local_track_count: int | None = None) -> Candidate | None:
    """The --auto check: the best candidate when it clears min_score and leads the runner-up by min_margin.

    Like --auto, a lone candidate leads by its whole score. With
    require_track_count and a local_track_count, the release must have
    that many tracks; a candidate that is not fetched yet cannot be checked
    and is not accepted (None).

    rules.title_match_min: alignments are made at
    match.auto_rules().title_match_min (the --auto value, 0.80). With a
    stricter rules.title_match_min, a file paired by title below it does
    not count as a sure pair, so the candidate is not a clear winner
    (None). With the default rules this never happens, exactly as with
    --auto.
    """
    rules = rules or auto_rules()
    if not candidates:
        return None
    ranked = sorted(candidates, key=lambda c: -c.score)
    top = ranked[0]
    second = ranked[1].score if len(ranked) > 1 else 0.0
    if top.score < rules.min_score or top.score - second < rules.min_margin:
        return None
    if rules.require_track_count and local_track_count is not None:
        if top.alignment is None:
            return None
        remote = sum(1 for p in top.alignment if p.remote is not None)
        if remote != local_track_count:
            return None
    if top.alignment is not None and any(
            p.method == "title" and p.title_similarity < rules.title_match_min for p in top.alignment):
        return None
    return top


def candidates(album_id: int) -> list[Candidate]:
    """Best first; partial while the background fetch runs (fetched=False, alignment None)."""
    return build_candidates(album_id)


def _search(client, guess: dict) -> list[dict]:
    results = provider_call(client.search_releases, guess.get("artist", ""), guess.get("album", ""),
                            guess.get("catno", ""), guess.get("year", ""))
    if not results and guess.get("album"):
        results = provider_call(client.search_releases, "", guess.get("album", ""), guess.get("catno", ""),
                                guess.get("year", ""))
    return list(results or [])


def _match_one(st, client, album_id: int, ctx) -> str:
    row = album_record(album_id, st)
    previous = MatchStatus(row["status"])
    guess = json.loads(row["guess"] or "{}")
    results = _search(client, guess)
    ranked = sorted(results, key=lambda r: -score_result(r, guess))[:SEARCH_KEEP]
    keep = row["chosen_release"]
    with st.index.tx() as conn:
        conn.execute("DELETE FROM candidates WHERE album_id = ? AND release_id IS NOT ? AND rank >= 0",
                     (album_id, keep))
        for rank, result in enumerate(ranked):
            rid = int(result.get("id") or 0)
            if not rid:
                continue
            conn.execute(
                "INSERT INTO candidates(album_id, release_id, rank, search) VALUES(?, ?, ?, ?)"
                " ON CONFLICT(album_id, release_id) DO UPDATE SET rank = excluded.rank, search = excluded.search",
                (album_id, rid, rank, json.dumps(result, ensure_ascii=False)))
    if not ranked:
        if previous not in KEEP_STATUS:
            set_status(album_id, MatchStatus.NO_MATCH, confidence=None)
        return "no_match"
    top_score = score_result(ranked[0], guess)
    if previous not in KEEP_STATUS:
        set_status(album_id, MatchStatus.NEEDS_REVIEW, confidence=top_score)
    for result in ranked[:FETCH_TOP]:
        ctx.check()
        try:
            fetch_release(album_id, int(result["id"]), st, search=result)
        except ProviderError:
            continue
    best = auto_accept(build_candidates(album_id, st), local_track_count=row["track_count"])
    if previous not in KEEP_STATUS:
        set_status(album_id, MatchStatus.MATCHED if best else MatchStatus.NEEDS_REVIEW, confidence=top_score)
    return "matched" if best else "review"


def find(album_ids: list[int]) -> Job:
    """Search Discogs for each album and fetch the top candidates, paced to the rate limit.

    Albums wait as QUEUED (saved in the index); cancelling leaves the rest
    QUEUED, and calling find on them again resumes. ACCEPTED, HELD and
    APPLIED albums keep their status; only their candidates are refreshed.
    """
    st = state()
    ids = [int(a) for a in album_ids]
    rows = {aid: album_record(aid, st) for aid in ids}  # NotFound before anything is queued
    for aid, row in rows.items():
        if MatchStatus(row["status"]) not in KEEP_STATUS:
            set_status(aid, MatchStatus.QUEUED)

    def work(ctx) -> str:
        try:
            client = st.client()
        except Exception:
            for aid, row in rows.items():
                if MatchStatus(row["status"]) not in KEEP_STATUS:
                    set_status(aid, MatchStatus(row["status"]), confidence=row["confidence"])
            raise
        tally = {"matched": 0, "review": 0, "no_match": 0, "failed": 0}
        errors = []
        for i, aid in enumerate(ids):
            ctx.progress(i, len(ids), f"matching {i + 1} of {len(ids)}")
            try:
                tally[_match_one(st, client, aid, ctx)] += 1
            except ProviderError as exc:
                tally["failed"] += 1
                errors.append(f"album {aid}: {exc}")
                row = rows[aid]
                if MatchStatus(row["status"]) not in KEEP_STATUS:
                    set_status(aid, MatchStatus(row["status"]), confidence=row["confidence"])
            except NotFound:
                tally["failed"] += 1
        msg = (f"{tally['matched']} matched, {tally['review']} need review, {tally['no_match']} no match"
               + (f", {tally['failed']} failed" if tally["failed"] else ""))
        if errors:
            msg += " (" + "; ".join(errors[:3]) + ")"
        return msg

    return st.runner.submit(JobKind.MATCH, work, total=len(ids), message="queued")


def choose(album_id: int, release_id: int) -> None:
    """Accept a candidate: status ACCEPTED (queued for a plan); nothing moves. Clears HELD."""
    album_record(album_id)
    candidate_row(album_id, release_id)
    set_status(album_id, MatchStatus.ACCEPTED, chosen_release=int(release_id))


def skip(album_id: int) -> None:
    album_record(album_id)
    set_status(album_id, MatchStatus.SKIPPED)


def manual(album_id: int, release_id_or_url: str) -> Job:
    """"Other release...": fetch a release by id or discogs.com URL and add it as a candidate."""
    st = state()
    row = album_record(album_id, st)
    release_id = parse_release_ref(str(release_id_or_url))
    if release_id is None:
        raise ValidationError("need a Discogs release id or a discogs.com/release/... URL", field="release_id_or_url")
    status = MatchStatus(row["status"])
    if status is MatchStatus.HELD:
        set_status(album_id, MatchStatus.NEEDS_REVIEW)  # manual clears HELD
        status = MatchStatus.NEEDS_REVIEW

    def work(ctx) -> str:
        release, _year = fetch_release(album_id, release_id, st)
        current = MatchStatus(album_record(album_id, st)["status"])
        if current not in (MatchStatus.ACCEPTED, MatchStatus.APPLIED):
            set_status(album_id, MatchStatus.NEEDS_REVIEW)
        else:
            events.album_changed.emit(album_id)
        return f"added release {release_id}: {release.get('title') or ''}"

    return st.runner.submit(JobKind.FETCH_CANDIDATES, work, total=1, message=f"fetching release {release_id}")


def destination(album_id: int, release_id: int, template: str | None = None) -> Destination:
    """Where the album would go with this release. Read-only; works before choose.

    files is empty until the release is fetched.
    """
    compiled = resolve_template(template)
    target = album_target(album_id, int(release_id), compiled)
    return Destination(target.root, target.target, target.files, target.problems)


__all__ = ["auto_accept", "auto_rules", "candidates", "choose", "destination", "find", "manual", "skip"]
