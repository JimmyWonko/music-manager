"""service.library: scan folders into the index, list and search albums."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from ..fs import AlbumFolder, collect_albums
from ..match import _disc_from_folder, _title_from_stem, guess_from_album_dir
from ..naming import year4
from ..tags.read import _read_tag_file, album_release_ids
from ._common import iso, load_album, now_utc, row_to_album_row
from ._state import State, events, state
from ._types import (
    STATUS_ORDER,
    Album,
    AlbumFilter,
    AlbumRow,
    AlbumSort,
    ConfigError,
    Job,
    JobKind,
    MatchStatus,
    Page,
    ValidationError,
)

ARTWORK_NAMES = ("cover", "folder", "front")
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


# --- scanning -------------------------------------------------------------------

def scan(roots: list[Path] | None = None) -> Job:
    """Index every album under roots (None = the configured library roots). Index-only; nothing on disk changes."""
    st = state()
    if roots is None:
        roots = list(st.config().library_roots)
        if not roots:
            raise ConfigError("No library root configured: settings.set(library_roots=[...]) first")
    roots = [Path(r) for r in roots]
    for root in roots:
        if not root.is_absolute():
            raise ValidationError(f"library root must be an absolute path: {root}", field="roots")

    def work(ctx) -> str:
        found = changed = removed = 0
        for root in roots:
            if not root.is_dir():
                raise ConfigError(f"library root does not exist or is not a folder: {root}")
            albums = collect_albums(root)
            seen: set[str] = set()
            for i, album in enumerate(albums):
                ctx.progress(i, len(albums), f"scanning {root}: {album.path.name}")
                album_id, is_change = index_album(st, root, album)
                seen.add(str(album.path))
                found += 1
                if is_change:
                    changed += 1
                    events.album_changed.emit(album_id)
            removed += _drop_vanished(st, root, seen)
        return f"{found} albums ({changed} new or changed, {removed} gone)"

    return st.runner.submit(JobKind.SCAN, work, message="scanning")


def _drop_vanished(st: State, root: Path, seen: set[str]) -> int:
    gone = [r["id"] for r in st.index.query("SELECT id, folder FROM albums WHERE root = ?", (str(root),))
            if r["folder"] not in seen and not Path(r["folder"]).is_dir()]
    with st.index.tx() as conn:
        for album_id in gone:
            conn.execute("DELETE FROM albums WHERE id = ?", (album_id,))
            st.index.delete_fts(conn, album_id)
    for album_id in gone:
        events.album_changed.emit(album_id)
    return len(gone)


def _first(tags, key: str) -> str:
    try:
        value = tags.get(key) if tags else None
    except Exception:  # noqa: BLE001
        return ""
    if not value:
        return ""
    return str(value[0] if isinstance(value, list) else value).strip()


def _leading_int(text: str) -> int | None:
    match = re.match(r"\s*(\d+)", text or "")
    return int(match.group(1)) if match else None


def _track_info(path: Path, album_path: Path) -> dict:
    audio, tags = None, None
    try:
        audio, tags = _read_tag_file(path, easy=True)
    except Exception:  # noqa: BLE001 - an unreadable file is still listed
        pass
    duration = None
    info = getattr(audio, "info", None)
    if info is not None and getattr(info, "length", None):
        duration = round(float(info.length), 3)
    disc_track = re.match(r"\s*(\d{1,2})-(\d{1,3})(?!\d)", path.stem)  # "2-01 Title"
    number = _leading_int(_first(tags, "tracknumber")) or (int(disc_track.group(2)) if disc_track else None) \
        or _leading_int(path.stem)
    disc = (_leading_int(_first(tags, "discnumber")) or _disc_from_folder(path, album_path)
            or (int(disc_track.group(1)) if disc_track else None) or 1)
    title = _first(tags, "title") or _title_from_stem(path.stem) or path.stem
    return {"number": number, "disc": disc, "title": title, "duration": duration}


def _artwork(album: AlbumFolder) -> Path | None:
    images = [p for p in album.sidecars if p.suffix.lower() in IMAGE_EXTS and p.parent == album.path]
    for name in ARTWORK_NAMES:
        for image in images:
            if image.stem.casefold() == name:
                return image
    return images[0] if images else None


def index_album(st: State, root: Path, album: AlbumFolder, status: MatchStatus | None = None,
                album_id: int | None = None) -> tuple[int, bool]:
    """Insert or refresh one album folder. Returns (album id, new or changed).

    Unchanged tracks (same size and mtime) are not re-read. A new album whose
    tracks all carry one DISCOGS_RELEASE_ID is APPLIED; others start
    UNMATCHED. An existing album keeps its status and decisions, except
    that an UNMATCHED one that is now fully tagged becomes APPLIED. With
    album_id the row is moved to this folder (after an apply or undo).
    """
    folder = str(album.path)
    existing = (st.index.one("SELECT * FROM albums WHERE id = ?", (album_id,)) if album_id is not None
                else st.index.one("SELECT * FROM albums WHERE folder = ?", (folder,)))
    old_tracks = {}
    if existing is not None:
        old_tracks = {r["path"]: r
                      for r in st.index.query("SELECT * FROM tracks WHERE album_id = ?", (existing["id"],))}

    rows = []
    changed = existing is None or existing["folder"] != folder
    for seq, path in enumerate(album.tracks):
        try:
            stat = path.stat()
            size, mtime = stat.st_size, stat.st_mtime_ns
        except OSError:
            size = mtime = None
        old = old_tracks.get(str(path))
        if old is not None and old["size"] == size and old["mtime_ns"] == mtime:
            info = {"number": old["number"], "disc": old["disc"], "title": old["title"], "duration": old["duration"]}
        else:
            info = _track_info(path, album.path)
            changed = True
        rows.append((seq, str(path), info["disc"], info["number"], info["title"], info["duration"], size, mtime))
    if set(old_tracks) != {r[1] for r in rows}:
        changed = True

    artwork = _artwork(album)
    extras = [str(p) for p in album.extra_dirs] + [str(p) for p in album.sidecars]
    same_art = existing is not None and existing["artwork"] == (str(artwork) if artwork else None)
    if (existing is not None and not changed and status is None and same_art
            and json.loads(existing["extras"] or "[]") == extras):
        return existing["id"], False  # incremental rescan: nothing to re-read

    guess = guess_from_album_dir(album.path, album.tracks) if album.tracks else {}
    first_tags = None
    if album.tracks:
        try:
            first_tags = _read_tag_file(album.tracks[0], easy=True)[1]
        except Exception:  # noqa: BLE001
            first_tags = None
    artist = guess.get("artist", "")
    artist_sort = _first(first_tags, "albumartistsort") or artist
    years = [y for y in (guess.get("originalyear", ""), guess.get("year", "")) if y]
    year = min(years) if years else ""
    ids = set(album_release_ids(album.tracks).values()) if album.tracks else {None}
    tagged_id = int(next(iter(ids))) if len(ids) == 1 and None not in ids and str(next(iter(ids))).isdigit() else None
    values = {
        "folder": folder, "root": str(root), "artist": artist, "title": guess.get("album", album.path.name),
        "year": year4(year), "catno": guess.get("catno", ""), "artist_sort": artist_sort,
        "track_count": len(album.tracks), "artwork": str(artwork) if artwork else None,
        "guess": json.dumps(guess, ensure_ascii=False), "extras": json.dumps(extras, ensure_ascii=False),
    }
    with st.index.tx() as conn:
        if existing is None:
            new_status = status or (MatchStatus.APPLIED if tagged_id else MatchStatus.UNMATCHED)
            cur = conn.execute(
                "INSERT INTO albums(folder, root, artist, title, year, catno, artist_sort, track_count, artwork, guess,"
                " extras, status, added_at, chosen_release) VALUES(:folder, :root, :artist, :title, :year, :catno,"
                " :artist_sort, :track_count, :artwork, :guess, :extras, :status, :added_at, :chosen)",
                {**values, "status": new_status.value, "added_at": iso(now_utc()), "chosen": tagged_id})
            aid = cur.lastrowid
        else:
            aid = existing["id"]
            for key in ("artist", "title", "year", "catno", "artist_sort", "track_count", "artwork"):
                if existing[key] != values[key]:
                    changed = True
            new_status = status or MatchStatus(existing["status"])
            chosen = existing["chosen_release"]
            if status is None and new_status is MatchStatus.UNMATCHED and tagged_id:
                new_status, chosen = MatchStatus.APPLIED, tagged_id
            conn.execute(
                "UPDATE albums SET folder=:folder, root=:root, artist=:artist, title=:title, year=:year, catno=:catno,"
                " artist_sort=:artist_sort, track_count=:track_count, artwork=:artwork, guess=:guess, extras=:extras,"
                " status=:status, chosen_release=:chosen WHERE id=:id",
                {**values, "status": new_status.value, "chosen": chosen, "id": aid})
            conn.execute("DELETE FROM tracks WHERE album_id = ?", (aid,))
        conn.executemany(
            "INSERT OR REPLACE INTO tracks(album_id, seq, path, disc, number, title, duration, size, mtime_ns)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)", [(aid, *r) for r in rows])
        st.index.update_fts(conn, aid, f"{artist} {artist_sort}", values["title"], " ".join(r[4] for r in rows))
    return aid, changed


# --- listing ----------------------------------------------------------------------

_STATUS_RANK = "CASE status " + " ".join(f"WHEN '{s.value}' THEN {i}" for i, s in enumerate(STATUS_ORDER)) + " END"
_YEAR = "COALESCE(NULLIF(year, ''), '9999')"


def _order(sort: AlbumSort, descending: bool) -> str:
    d = "DESC" if descending else "ASC"
    if sort is AlbumSort.ARTIST:
        return f"cf(artist_sort) {d}, {_YEAR}, cf(title), folder"
    if sort is AlbumSort.TITLE:
        return f"cf(title) {d}, folder"
    if sort is AlbumSort.YEAR:
        return f"{_YEAR} {d}, cf(artist_sort), folder"
    if sort is AlbumSort.ADDED:  # newest first; descending = oldest first
        return f"added_at {'ASC' if descending else 'DESC'}, folder"
    if sort is AlbumSort.CONFIDENCE:  # unmatched (None) last in both directions
        return f"confidence IS NULL, confidence {d}, folder"
    if sort is AlbumSort.STATUS:
        return f"{_STATUS_RANK} {d}, folder"
    return f"folder {d}"


def _parse_sort(sort) -> AlbumSort:
    if isinstance(sort, AlbumSort):
        return sort
    try:
        return AlbumSort(str(sort))
    except ValueError:
        raise ValidationError(f"Unknown sort key {sort!r}", field="sort") from None


def _fts_query(text: str) -> str:
    tokens = re.findall(r"\w+", text, flags=re.UNICODE)
    return " ".join(f'"{t}"*' for t in tokens)


def _where(st: State, filter: AlbumFilter | None, with_status: bool = True) -> tuple[str, list]:
    clauses, params = [], []
    if filter is None:
        return "", []
    if with_status and filter.status is not None:
        statuses = [s.value if isinstance(s, MatchStatus) else MatchStatus(s).value for s in filter.status]
        if not statuses:
            clauses.append("0")
        else:
            clauses.append(f"status IN ({', '.join('?' * len(statuses))})")
            params += statuses
    if filter.max_confidence is not None:
        clauses.append("confidence IS NOT NULL AND confidence < ?")
        params.append(float(filter.max_confidence))
    text = (filter.text or "").strip()
    if text:
        if st.index.fts and _fts_query(text):
            clauses.append("id IN (SELECT rowid FROM album_fts WHERE album_fts MATCH ?)")
            params.append(_fts_query(text))
        else:
            like = f"%{text}%"
            clauses.append("(artist LIKE ? OR artist_sort LIKE ? OR title LIKE ? OR id IN "
                           "(SELECT album_id FROM tracks WHERE title LIKE ?))")
            params += [like] * 4
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def albums(filter: AlbumFilter, sort: AlbumSort, offset: int, limit: int, descending: bool = False) -> Page[AlbumRow]:
    st = state()
    sort = _parse_sort(sort)
    where, params = _where(st, filter or AlbumFilter())
    offset, limit = max(0, int(offset)), max(0, int(limit))
    try:
        total = st.index.one(f"SELECT COUNT(*) AS n FROM albums{where}", params)["n"]
        rows = st.index.query(f"SELECT * FROM albums{where} ORDER BY {_order(sort, descending)} LIMIT ? OFFSET ?",
                              [*params, limit, offset])
    except sqlite3.OperationalError as exc:  # e.g. an FTS syntax the tokenizer rejects
        raise ValidationError(f"bad search text: {exc}", field="text") from exc
    return Page([row_to_album_row(r) for r in rows], total, offset)


def status_counts(filter: AlbumFilter | None = None) -> dict[MatchStatus, int]:
    """One query; filter.status is ignored, every status is counted."""
    st = state()
    where, params = _where(st, filter, with_status=False)
    counts = {s: 0 for s in MatchStatus}
    for row in st.index.query(f"SELECT status, COUNT(*) AS n FROM albums{where} GROUP BY status", params):
        counts[MatchStatus(row["status"])] = row["n"]
    return counts


def album(album_id: int) -> Album:
    return load_album(album_id)


def search(query: str, limit: int = 200) -> list[AlbumRow]:
    """Full-text search over artist, album and track titles; ARTIST order."""
    if not (query or "").strip():
        return []
    return list(albums(AlbumFilter(text=query), AlbumSort.ARTIST, 0, limit).items)


__all__ = ["album", "albums", "scan", "search", "status_counts"]
