"""Discogs data for the service: stored candidates, fetching full releases, building Candidate objects."""

from __future__ import annotations

import json
import re
import time

from ..fs import AlbumFolder
from ..match import TITLE_MATCH_MIN, align_tracks, score_components, score_result, similar
from ..naming import year4
from ..providers.discogs import (
    CACHE_MAX_AGE,
    DiscogsError,
    DiscogsRelease,
    DiscogsTrack,
    SearchResult,
    flatten_tracklist,
    release_artists,
    release_catno,
    split_result_title,
)
from ._common import album_folder, album_record, local_tracks
from ._common import title_match_min as rules_title_match_min
from ._state import State, events, state
from ._types import (
    DISCOGS_CREDIT,
    Candidate,
    LocalTrack,
    NotFound,
    ProviderError,
    RemoteTrack,
    ScorePart,
    TrackPair,
)

WEIGHTS = {"album": 0.40, "artist": 0.35, "catno": 0.18, "year": 0.07}
DURATION_TOLERANCE_S = 5.0


def provider_call(fn, *args):
    """Run a Discogs call; its errors become ProviderError (with retry_after for rate limits)."""
    try:
        return fn(*args)
    except DiscogsError as exc:
        text = str(exc)
        retry = getattr(exc, "retry_after", None)
        if retry is None and "429" in text:
            retry = 60.0
        raise ProviderError(text, retry_after=retry) from exc
    except OSError as exc:  # requests' errors are OSErrors
        raise ProviderError(f"Discogs request failed: {exc}") from exc


def candidate_rows(album_id: int, st: State | None = None):
    st = st or state()
    return st.index.query("SELECT * FROM candidates WHERE album_id = ? ORDER BY rank, release_id", (album_id,))


def candidate_row(album_id: int, release_id: int, st: State | None = None):
    st = st or state()
    row = st.index.one("SELECT * FROM candidates WHERE album_id = ? AND release_id = ?", (album_id, int(release_id)))
    if row is None:
        raise NotFound(f"release {release_id} is not a candidate for album {album_id}")
    return row


def search_from_release(release: DiscogsRelease) -> SearchResult:
    """A search-result-shaped summary of a full release, for scoring a release found by id."""
    labels = [lab.get("name") or "" for lab in release.get("labels") or []]
    formats = [f.get("name") or "" for f in release.get("formats") or []]
    return {
        "id": int(release.get("id") or 0),
        "title": f"{release_artists(release)} - {release.get('title') or ''}",
        "year": str(release.get("year") or ""),
        "catno": release_catno(release),
        "label": labels,
        "format": formats,
        "country": release.get("country") or "",
        "master_id": int(release.get("master_id") or 0),
    }


def release_from_search(search: SearchResult) -> DiscogsRelease:
    """A partial release built from a search result: enough to name the folder before the full fetch."""
    artist, title = split_result_title(search.get("title") or "")
    formats = search.get("format") or []
    return {
        "id": int(search.get("id") or 0),
        "title": title,
        "year": str(search.get("year") or ""),
        "artists": [{"name": artist}] if artist else [],
        "labels": [{"name": (search.get("label") or [""])[0], "catno": search.get("catno") or ""}],
        "formats": [{"name": formats[0], "qty": "1"}] if formats else [],
        "master_id": int(search.get("master_id") or 0),
        "tracklist": [],
    }


def fetch_release(album_id: int, release_id: int, st: State | None = None, search: SearchResult | None = None,
                  rank: int | None = None) -> tuple[DiscogsRelease, str]:
    """Fetch a full release (and its master's year), store it with the album's candidates, emit candidate_ready."""
    st = st or state()
    client = st.client()
    release = provider_call(client.release, int(release_id))
    master_year = ""
    master_id = release.get("master_id")
    if master_id:
        try:
            master_year = year4(provider_call(client.master, int(master_id)).get("year"))
        except ProviderError:
            master_year = ""
    with st.index.tx() as conn:
        exists = conn.execute("SELECT 1 FROM candidates WHERE album_id = ? AND release_id = ?",
                              (album_id, int(release_id))).fetchone()
        if exists:
            conn.execute("UPDATE candidates SET release = ?, master_year = ?, fetched_at = ? WHERE album_id = ?"
                         " AND release_id = ?", (json.dumps(release, ensure_ascii=False), master_year, time.time(),
                                                 album_id, int(release_id)))
        else:
            conn.execute("INSERT INTO candidates(album_id, release_id, rank, search, release, master_year, fetched_at)"
                         " VALUES(?, ?, ?, ?, ?, ?, ?)",
                         (album_id, int(release_id), -1 if rank is None else rank,
                          json.dumps(search or search_from_release(release), ensure_ascii=False),
                          json.dumps(release, ensure_ascii=False), master_year, time.time()))
    events.candidate_ready.emit(album_id, int(release_id))
    return release, master_year


def release_is_fresh(album_id: int, release_id: int, st: State | None = None) -> bool:
    """The full release is stored and younger than the 6-hour Discogs caching limit (no network)."""
    st = st or state()
    row = candidate_row(album_id, release_id, st)
    return bool(row["release"]) and (time.time() - (row["fetched_at"] or 0)) < CACHE_MAX_AGE


def release_for(album_id: int, release_id: int, st: State | None = None, fetch: bool = True
                ) -> tuple[DiscogsRelease | None, str, SearchResult]:
    """(full release or None, master year, search result) for one candidate.

    A stored release older than the 6-hour Discogs caching limit is fetched
    again when fetch is set (through the client's response cache).
    """
    st = st or state()
    row = candidate_row(album_id, release_id, st)
    search = json.loads(row["search"])
    if row["release"] and (time.time() - (row["fetched_at"] or 0)) < CACHE_MAX_AGE:
        return json.loads(row["release"]), row["master_year"] or "", search
    if not fetch:
        return (json.loads(row["release"]) if row["release"] else None), row["master_year"] or "", search
    release, master_year = fetch_release(album_id, release_id, st)
    return release, master_year, search


def album_tracks(release: DiscogsRelease) -> list[DiscogsTrack]:
    return flatten_tracklist(release, release_artists(release))


def _remote(dt: DiscogsTrack) -> RemoteTrack:
    return RemoteTrack(dt.position, dt.disc, dt.number, dt.title, dt.artist, dt.duration_s)


def alignment(album: AlbumFolder, locals_: list[LocalTrack], dtracks: list[DiscogsTrack],
              title_match_min: float | None = None) -> list[TrackPair]:
    """The engine's pairing, as TrackPairs. title_match_min: None = match.auto_rules().title_match_min."""
    if title_match_min is None:
        title_match_min = rules_title_match_min()
    by_path = {t.path: t for t in locals_}
    triples, _legacy = align_tracks(album, dtracks, title_match_min)
    used: set[int] = set()
    pairs: list[TrackPair] = []
    for path, dt, method in triples:
        local = by_path.get(path) or LocalTrack(0, path, 1, None, path.stem, None)
        if dt is None:
            pairs.append(TrackPair("unmatched", local, None, 0.0, None, False))
            continue
        used.add(id(dt))
        sim = similar(local.title, dt.title)
        delta = None
        if local.duration_s is not None and dt.duration_s is not None:
            delta = round(local.duration_s - dt.duration_s, 3)
        ok = sim >= title_match_min or (delta is not None and abs(delta) <= DURATION_TOLERANCE_S)
        pairs.append(TrackPair(method, local, _remote(dt), round(sim, 4), delta, ok))
    for dt in dtracks:
        if id(dt) not in used:
            pairs.append(TrackPair("unmatched", None, _remote(dt), 0.0, None, False))
    return pairs


def _mmss(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def breakdown(search: SearchResult, guess: dict, pairs: list[TrackPair] | None, local_count: int,
              remote_count: int | None) -> list[ScorePart]:
    comps = score_components(search, guess)
    r_artist, r_album = split_result_title(search.get("title") or "")
    remote = {"album": r_album, "artist": r_artist, "catno": search.get("catno") or "",
              "year": str(search.get("year") or "")}
    parts = []
    for name, weight in WEIGHTS.items():
        value = round(comps[name] / weight, 4) if weight else 0.0
        ok = value >= TITLE_MATCH_MIN if name in ("album", "artist") else value >= 1.0
        parts.append(ScorePart(name, weight, value, str(guess.get(name, "")), remote[name], ok))
    if pairs is not None and remote_count is not None:
        diff = abs(remote_count - local_count)
        value = 1.0 if diff == 0 else max(0.0, 1.0 - diff / max(remote_count, local_count, 1))
        # weight 0: today's score (and so --auto / auto_accept) is the four search-result parts only
        parts.append(ScorePart("track_count", 0.0, round(value, 4), str(local_count), str(remote_count), diff == 0))
        timed = [p for p in pairs if p.duration_delta_s is not None]
        if timed:
            close = sum(1 for p in timed if abs(p.duration_delta_s) <= DURATION_TOLERANCE_S)
            local_total = sum(p.local.duration_s for p in timed if p.local and p.local.duration_s)
            remote_total = sum(p.remote.duration_s for p in timed if p.remote and p.remote.duration_s)
            value = close / len(timed)
            parts.append(ScorePart("durations", 0.0, round(value, 4), _mmss(local_total), _mmss(remote_total),
                                   value >= 0.8))
        else:
            parts.append(ScorePart("durations", 0.0, 0.0, "", "", False))
    return parts


def _primary_image(release: DiscogsRelease | None) -> tuple[str | None, str | None]:
    images = list((release or {}).get("images") or [])
    if not images:
        return None, None
    primary = next((i for i in images if i.get("type") == "primary"), images[0])
    return primary.get("uri") or None, primary.get("uri150") or None


def build_candidates(album_id: int, st: State | None = None) -> list[Candidate]:
    st = st or state()
    row = album_record(album_id, st)
    guess = json.loads(row["guess"] or "{}")
    locals_ = local_tracks(album_id, st)
    folder = None
    scored = []
    for crow in candidate_rows(album_id, st):
        search = json.loads(crow["search"])
        release = json.loads(crow["release"]) if crow["release"] else None
        pairs = None
        remote_count = None
        if release is not None:
            if folder is None:
                folder = album_folder(album_id, st)
            dtracks = album_tracks(release)
            pairs = alignment(folder, locals_, dtracks)
            remote_count = len(dtracks)
        score = score_result(search, guess)  # exactly the --auto score
        scored.append((score, crow["rank"], crow, search, release, pairs, remote_count))
    scored.sort(key=lambda x: (-x[0], x[1]))
    out = []
    for i, (score, _rank, crow, search, release, pairs, remote_count) in enumerate(scored):
        margin = score - scored[i + 1][0] if i + 1 < len(scored) else None
        r_artist, r_title = split_result_title(search.get("title") or "")
        if release is not None:
            r_artist, r_title = release_artists(release), release.get("title") or r_title
        art_url, thumb = _primary_image(release)
        labels = search.get("label") or []
        formats = list(search.get("format") or [])
        out.append(Candidate(
            release_id=int(crow["release_id"]),
            master_id=int(search.get("master_id") or (release or {}).get("master_id") or 0) or None,
            artist=r_artist,
            title=r_title,
            year=str(search.get("year") or (release or {}).get("year") or ""),
            country=search.get("country") or (release or {}).get("country") or "",
            label=labels[0] if labels else "",
            catno=search.get("catno") or (release_catno(release) if release else ""),
            formats=formats,
            score=score,
            margin=margin,
            breakdown=breakdown(search, guess, pairs, len(locals_), remote_count),
            alignment=pairs,
            artwork_url=art_url or search.get("cover_image") or None,
            thumb_url=search.get("thumb") or thumb,
            artwork=None,
            fetched=release is not None,
            source=DISCOGS_CREDIT,
        ))
    return out


RELEASE_ID = re.compile(r"discogs\.com/(?:.+/)?release/(\d+)|^\s*\[?r?(\d+)\]?\s*$", re.I)


def parse_release_ref(text: str) -> int | None:
    match = RELEASE_ID.search(text or "")
    if not match:
        return None
    return int(match.group(1) or match.group(2))

