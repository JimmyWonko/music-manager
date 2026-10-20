"""service.match: find Discogs candidates, review them, choose or skip."""

from __future__ import annotations

import json
from pathlib import Path

from ..match import AUTO_MIN_MARGIN, AUTO_MIN_SCORE, TITLE_MATCH_MIN, score_result
from ._common import (
    album_record,
    cluster_guess,
    local_tracks,
    row_to_album_row,
    set_status,
)
from ._pins import pin_rows, to_pin
from ._planning import album_target, chosen_release_id, resolve_template
from ._releases import (
    build_candidates,
    candidate_row,
    fetch_release,
    parse_release_ref,
    provider_call,
)
from ._state import events, state
from ._types import (
    AlbumFilter,
    AutoRules,
    Candidate,
    ClusterRow,
    Destination,
    Job,
    JobKind,
    LocalTrack,
    MatchStatus,
    MatchView,
    MMError,
    NotFound,
    Page,
    Pin,
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

    Ranked by Candidate.auto_score (the --auto score), not Candidate.score,
    so it accepts exactly what discogs-tag --auto would.

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
    def auto(c: Candidate) -> float:
        return c.auto_score if c.auto_score is not None else c.score

    ranked = sorted(candidates, key=lambda c: -auto(c))
    top = ranked[0]
    second = auto(ranked[1]) if len(ranked) > 1 else 0.0
    if auto(top) < rules.min_score or auto(top) - second < rules.min_margin:
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
    cands = build_candidates(album_id, st)
    best = auto_accept(cands, local_track_count=row["track_count"])
    if previous not in KEEP_STATUS:
        confidence = cands[0].score if cands else top_score  # the best service score, as the review shows it
        set_status(album_id, MatchStatus.MATCHED if best else MatchStatus.NEEDS_REVIEW, confidence=confidence)
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


def _resolve_id(ident: int | str) -> tuple[int | None, str | None, list[LocalTrack]]:
    """(album id, cluster id, files of a cluster not adopted yet) for an album id, "album-<id>" or a cluster id."""
    from .library import cluster as find_cluster

    if isinstance(ident, str) and ident.startswith("album-") and ident[6:].isdigit():
        ident = int(ident[6:])
    if isinstance(ident, str) and not ident.isdigit():
        c = find_cluster(ident)
        if c.album_id is None:
            return None, c.id, [LocalTrack(0, f.path, f.disc, f.number, f.title, f.duration_s) for f in c.files]
        return c.album_id, c.id, []
    return int(ident), None, []


def view(album_or_cluster_id: int | str, release_id: int | None = None) -> MatchView:
    """The album-match view: candidates and file -> release-track pairing for one release. Read-only.

    album_or_cluster_id: an album id, "album-<id>" (ClusterRow.id) or a cluster id. A cluster that
    is not adopted yet has no candidates: every file is unmatched (adopt it, then match.find).
    release_id None: the chosen release, else the best candidate. Never calls Discogs: until the
    release is fetched, fetched is False and every file is in unmatched_files. problems are what
    plan.build would report (TRACK_MISMATCH before any override, PIN_CONFLICT for unusable pins);
    pairs the user made have method "manual" (pins lists them).
    """
    st = state()
    album_id, cluster_id, files = _resolve_id(album_or_cluster_id)
    if album_id is None:
        return MatchView(None, cluster_id, None, False, [], [], files, [], [])
    row = album_record(album_id, st)
    cg = cluster_guess(row)
    cluster_id = cluster_id or (cg["cluster"] if cg else None)
    cands = build_candidates(album_id, st)
    rid = int(release_id) if release_id is not None else chosen_release_id(album_id, st)
    locals_ = local_tracks(album_id, st)
    if rid is None:
        return MatchView(album_id, cluster_id, None, False, cands, [], locals_, [], [])
    candidate_row(album_id, rid, st)  # NotFound for a release that is not a candidate
    target = album_target(album_id, rid, resolve_template(None), st=st, fetch=False)
    pin_list = [to_pin(r) for r in pin_rows(st, album_id, rid)]
    cand = next((c for c in cands if c.release_id == rid), None)
    pairs = list(cand.alignment or []) if cand is not None else []
    if not target.fetched:
        return MatchView(album_id, cluster_id, rid, False, cands, [], locals_, [], list(target.problems), pin_list)
    return MatchView(album_id, cluster_id, rid, True, cands, pairs,
                     [p.local for p in pairs if p.remote is None and p.local is not None],
                     [p.remote for p in pairs if p.local is None and p.remote is not None], list(target.problems),
                     pin_list)


# --- manual pairs ------------------------------------------------------------------

def pin(album_id: int, file: Path, position: str, override_hold: bool = False,
        release_id: int | None = None) -> Pin:
    """Pair one file with one release track by hand (drag-and-drop). Index only; replaces the file's pin.

    release_id None: the chosen release, else the best candidate (pins apply to that release only).
    position: the track's position as Discogs lists it, or "<disc>-<number>". Only bad input raises
    (NotFound album, ValidationError for a file that is not the album's or an empty position);
    everything else (unknown position, two files on one track, a disc hold) is a PIN_CONFLICT in
    view / plan.build. A pin that overrides a disc hold needs override_hold=True. Emits
    match_view_changed(album_id).
    """
    st = state()
    album_record(album_id, st)
    path = Path(file)
    if path not in {t.path for t in local_tracks(album_id, st)}:
        raise ValidationError(f"{path} is not a file of album {album_id}", field="file")
    if not str(position or "").strip():
        raise ValidationError("position is empty", field="position")
    rid = int(release_id) if release_id is not None else chosen_release_id(album_id, st)
    if rid is None:
        raise MMError(f"album {album_id} has no candidates: run match.find first")
    candidate_row(album_id, rid, st)
    try:
        s = path.stat()
        size, mtime = s.st_size, s.st_mtime_ns
    except OSError:
        size = mtime = None
    with st.index.tx() as conn:
        conn.execute("INSERT OR REPLACE INTO pins(album_id, path, release_id, position, override_hold, size, mtime_ns)"
                     " VALUES(?, ?, ?, ?, ?, ?, ?)",
                     (album_id, str(path), rid, str(position).strip(), int(bool(override_hold)), size, mtime))
    events.match_view_changed.emit(album_id)
    return to_pin(st.index.one("SELECT * FROM pins WHERE album_id = ? AND path = ?", (album_id, str(path))))


def unpin(album_id: int, file: Path | None = None) -> None:
    """Drop one file's pin, or every pin of the album (file None). Emits match_view_changed(album_id)."""
    st = state()
    album_record(album_id, st)
    with st.index.tx() as conn:
        if file is None:
            conn.execute("DELETE FROM pins WHERE album_id = ?", (album_id,))
        else:
            conn.execute("DELETE FROM pins WHERE album_id = ? AND path = ?", (album_id, str(Path(file))))
    events.match_view_changed.emit(album_id)


def pins(album_id: int) -> list[Pin]:
    """The album's pins for every release, by path. plan.apply uses them up (journal.manual_pairs keeps them)."""
    st = state()
    album_record(album_id, st)
    return [to_pin(r) for r in pin_rows(st, album_id)]


# --- one list for the Match window -------------------------------------------------

def clusters(filter: AlbumFilter | None = None, offset: int = 0, limit: int = 200) -> Page[ClusterRow]:
    """Folder albums and clusters in one paged list (like library.albums): ClusterRow per item.

    Folder albums (kind "folder", id "album-<id>"), adopted and not-adopted tag clusters and
    unclustered files (library.clusters). Loose-tracks rows themselves are not listed: their files
    are in clusters. filter.status / max_confidence match albums only (a cluster not adopted has no
    status, so it is left out by either); filter.text matches artist, title and file titles.
    Order: artist, title, folder (case-insensitive), then id. Computed in memory.
    """
    from .library import clusters as all_clusters

    st = state()
    f = filter or AlbumFilter()
    text = (f.text or "").strip().casefold()
    rows: list[ClusterRow] = []
    for row in st.index.query("SELECT * FROM albums"):
        ar = row_to_album_row(row)
        if ar.loose_tracks:
            continue
        if f.status is not None and ar.status not in {MatchStatus(s) for s in f.status}:
            continue
        if f.max_confidence is not None and (ar.confidence is None or ar.confidence >= f.max_confidence):
            continue
        titles = " ".join(t.title for t in local_tracks(ar.id, st)) if text else ""
        if text and text not in f"{ar.artist} {ar.title} {titles}".casefold():
            continue
        kind = "tag_cluster" if ar.cluster_id else "folder"
        rows.append(ClusterRow(ar.cluster_id or f"album-{ar.id}", kind, ar.artist, ar.title, ar.track_count,
                               ar.status, ar.id, bool(ar.cluster_id), ar.folder))
    if f.status is None and f.max_confidence is None:
        for c in all_clusters():
            if c.album_id is not None:
                continue
            if text and text not in " ".join([c.artist, c.title, *(x.title for x in c.files)]).casefold():
                continue
            rows.append(ClusterRow(c.id, c.kind, c.artist, c.title, len(c.files), None, None, True, c.folder))
    rows.sort(key=lambda r: (r.kind == "unclustered", r.artist.casefold(), r.title.casefold(), str(r.folder), r.id))
    offset, limit = max(0, int(offset)), max(0, int(limit))
    return Page(rows[offset:offset + limit], len(rows), offset)


__all__ = ["auto_accept", "auto_rules", "candidates", "choose", "destination", "find", "manual", "skip", "view"]
