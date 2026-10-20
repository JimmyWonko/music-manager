"""service.library: scan folders into the index, list and search albums."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import replace
from pathlib import Path

from ..clustering import Item
from ..clustering import cluster as cluster_items
from ..fs import AlbumFolder, collect_albums, is_audio
from ..match import _disc_from_folder, _title_from_stem, guess_from_album_dir
from ..naming import year4
from ..tags.read import _read_tag_file, album_release_ids
from ._common import (
    cluster_guess,
    is_loose_tracks,
    iso,
    load_album,
    local_tracks,
    now_utc,
    root_for,
    row_to_album_row,
)
from ._state import State, events, state
from ._types import (
    STATUS_ORDER,
    Album,
    AlbumFilter,
    AlbumRow,
    AlbumSort,
    Cluster,
    ClusterFile,
    ConfigError,
    Job,
    JobKind,
    MatchStatus,
    MMError,
    NotFound,
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


def _vanished(st: State, row) -> bool:
    if cluster_guess(row):  # an adopted cluster lives as long as one of its files does
        return not any(Path(t["path"]).is_file()
                       for t in st.index.query("SELECT path FROM tracks WHERE album_id = ?", (row["id"],)))
    return not Path(row["folder"]).is_dir()


def _drop_vanished(st: State, root: Path, seen: set[str]) -> int:
    gone = [r["id"] for r in st.index.query("SELECT id, folder, guess FROM albums WHERE root = ?", (str(root),))
            if r["folder"] not in seen and _vanished(st, r)]
    with st.index.tx() as conn:
        for album_id in gone:
            conn.execute("DELETE FROM albums WHERE id = ?", (album_id,))
            st.index.delete_fts(conn, album_id)
    for album_id in gone:
        events.album_changed.emit(album_id)  # kept for older slots
        events.album_removed.emit(album_id)
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

    claimed = _claimed(st, existing["id"] if existing is not None else album_id)
    if claimed and not (existing is not None and cluster_guess(existing)):
        album = replace(album, tracks=[p for p in album.tracks if str(p) not in claimed])
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
    loose = is_loose_tracks(album)
    if (existing is not None and not changed and status is None and same_art
            and json.loads(existing["extras"] or "[]") == extras
            and bool(json.loads(existing["guess"] or "{}").get("loose_tracks")) == loose):
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
    title = guess.get("album", album.path.name)
    catno = guess.get("catno", "")
    if loose:  # loose files next to subfolders with audio: named after their own folder
        artist = artist_sort = album.path.name
        title = year = catno = ""
        guess["loose_tracks"] = True
    ids = set(album_release_ids(album.tracks).values()) if album.tracks else {None}
    tagged_id = int(next(iter(ids))) if len(ids) == 1 and None not in ids and str(next(iter(ids))).isdigit() else None
    values = {
        "folder": folder, "root": str(root), "artist": artist, "title": title,
        "year": year4(year), "catno": catno, "artist_sort": artist_sort,
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


# --- clusters ---------------------------------------------------------------------

CLUSTER_KEY = ".matrixwell-cluster-"  # albums.folder of an adopted cluster: <folder>/<CLUSTER_KEY><id> (never on disk)


def _claimed(st: State, album_id: int | None) -> set[str]:
    """Paths owned by adopted clusters (other than album_id): a folder's own row leaves them out."""
    rows = st.index.query("SELECT t.path FROM tracks t JOIN albums a ON a.id = t.album_id"
                          " WHERE a.id IS NOT ? AND a.folder LIKE ?", (album_id, f"%{CLUSTER_KEY}%"))
    return {r["path"] for r in rows}


def _cluster_id(paths: list[Path], prefix: str = "cl-") -> str:
    return prefix + hashlib.sha1("\n".join(sorted(str(p) for p in paths)).encode()).hexdigest()[:12]


def _cluster_file(path: Path, folder: Path, forced: str = "") -> tuple[ClusterFile, Item]:
    try:
        tags = _read_tag_file(path, easy=True)[1]
    except Exception:  # noqa: BLE001 - an unreadable file still clusters by folder name
        tags = None
    info = _track_info(path, folder)
    album, albumartist, artist = _first(tags, "album"), _first(tags, "albumartist"), _first(tags, "artist")
    cf = ClusterFile(path, info["disc"], info["number"], info["title"], info["duration"], album,
                     albumartist or artist)
    return cf, Item(cf, album, albumartist, artist, path.parent.name, forced)


def _moves(st: State) -> dict[str, str]:
    """Files the user moved (move_to_cluster): path -> group key, "" = unclustered."""
    return {r["path"]: r["key"] for r in st.index.query("SELECT path, key FROM cluster_moves")}


def _group(st: State, paths: list[Path], folder: Path) -> list[Cluster]:
    moves = _moves(st)
    unclustered = sorted(p for p in paths if moves.get(str(p)) == "")
    files = [_cluster_file(p, folder, moves.get(str(p), "")) for p in sorted(paths) if p not in unclustered]
    out = []
    for fc in cluster_items([item for _cf, item in files]):
        members = sorted((item.key for item in fc.items), key=lambda f: str(f.path))
        c = Cluster(_cluster_id([f.path for f in members]), fc.artist, fc.title, folder, members,
                    len({f.disc for f in members}))
        st.cluster_keys[c.id] = fc.key
        out.append(c)
    if unclustered:
        members = [_cluster_file(p, folder)[0] for p in unclustered]
        out.append(Cluster(_cluster_id(unclustered, "un-"), "", "", folder, members, len({f.disc for f in members}),
                           kind="unclustered"))
    return out


def _adopted(st: State, folder: Path | None = None) -> list[Cluster]:
    out = []
    for row in st.index.query("SELECT * FROM albums WHERE folder LIKE ?", (f"%{CLUSTER_KEY}%",)):
        cg = cluster_guess(row)
        if not cg or (folder is not None and Path(cg["cluster_folder"]) != folder):
            continue
        where = Path(cg["cluster_folder"])
        files = [f for f, _item in (_cluster_file(t.path, where) for t in local_tracks(row["id"], st)
                                    if t.path.is_file())]
        out.append(Cluster(cg["cluster"], row["artist"], row["title"], where, files, len({f.disc for f in files}),
                           album_id=row["id"]))
    return out


def _loose_rows(st: State, folder: Path | None = None):
    rows = st.index.query("SELECT * FROM albums WHERE folder NOT LIKE ?", (f"%{CLUSTER_KEY}%",))
    return [r for r in rows if json.loads(r["guess"] or "{}").get("loose_tracks")
            and (folder is None or Path(r["folder"]) == folder)]


def _folder_clusters(st: State, row) -> list[Cluster]:
    files = [t.path for t in local_tracks(row["id"], st) if t.path.is_file()]
    return _group(st, files, Path(row["folder"]))


def _remember(st: State, found: list[Cluster]) -> list[Cluster]:
    found.sort(key=lambda c: (str(c.folder), c.kind == "unclustered", str(c.files[0].path) if c.files else ""))
    with st.lock:
        st.clusters.update({c.id: c for c in found})
    return found


def _all_clusters(st: State, progress=None) -> list[Cluster]:
    found: list[Cluster] = []
    rows = _loose_rows(st)
    for i, row in enumerate(rows):
        if progress is not None:
            progress(i, len(rows), f"clustering {Path(row['folder']).name}")
        found += _folder_clusters(st, row)
    return _remember(st, found + _adopted(st))


def _in_folder(st: State, folder: Path) -> list[Cluster]:
    found: list[Cluster] = []
    for row in _loose_rows(st, folder):
        found += _folder_clusters(st, row)
    return _remember(st, found + _adopted(st, folder))


def clusters(paths: list[Path] | None = None) -> list[Cluster]:
    """Group loose files into album clusters by their album-level tags (Picard-style). Read-only.

    paths None: the files of every loose-tracks album (AlbumRow.loose_tracks), clustered folder
    by folder, plus every adopted cluster (album_id set). paths: exactly these audio files
    (absolute, inside a library root), clustered together. Files moved with move_to_cluster
    follow the move; files moved out of every cluster come back as one kind="unclustered"
    cluster per folder (last in that folder). Sorted by folder, then first file.
    """
    st = state()
    if paths is None:
        return _all_clusters(st)
    files = [Path(p) for p in paths]
    if not files:
        raise ValidationError("no files given", field="paths")
    for p in files:
        if not p.is_absolute() or not p.is_file() or not is_audio(p):
            raise ValidationError(f"not an audio file: {p}", field="paths")
        if root_for(p, st) is None:
            raise ValidationError(f"not inside a library root: {p}", field="paths")
    folder = Path(os.path.commonpath([str(p.parent) for p in files]))
    adopted = {c.id: c for c in _adopted(st)}
    return _remember(st, [adopted.get(c.id, c) for c in _group(st, files, folder)])


def scan_clusters() -> Job:
    """clusters() as a Job (JobKind.CLUSTER) with progress per folder; emits clusters_changed(ids) when done."""
    st = state()

    def work(ctx) -> str:
        found = _all_clusters(st, progress=ctx.progress)
        events.clusters_changed.emit([c.id for c in found])
        tags = sum(1 for c in found if c.kind == "tag_cluster")
        return f"{tags} cluster(s), {sum(len(c.files) for c in found)} file(s)"

    return st.runner.submit(JobKind.CLUSTER, work, message="clustering")


def cluster(cluster_id: str) -> Cluster:
    """A cluster from the last clusters() call (or adopted). NotFound otherwise."""
    st = state()
    for c in _adopted(st):
        if c.id == cluster_id:
            return c
    with st.lock:
        found = st.clusters.get(cluster_id)
    if found is None:
        raise NotFound(f"cluster {cluster_id}")
    return found


def _track_row(aid: int, seq: int, path: Path, folder: Path) -> tuple:
    size, mtime = _stat(path)
    info = _track_info(path, folder)
    return (aid, seq, str(path), info["disc"], info["number"], info["title"], info["duration"], size, mtime)


def _stat(path: Path) -> tuple[int | None, int | None]:
    try:
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns
    except OSError:
        return None, None


def _rekey(conn, aid: int) -> tuple[str, str]:
    """An adopted cluster's files changed: new id (path hash), folder key and track count. (old id, new id)."""
    row = conn.execute("SELECT guess FROM albums WHERE id = ?", (aid,)).fetchone()
    guess = json.loads(row["guess"])
    paths = [Path(r["path"]) for r in conn.execute("SELECT path FROM tracks WHERE album_id = ? ORDER BY path", (aid,))]
    old, new = guess["cluster"], _cluster_id(paths)
    conn.executemany("UPDATE tracks SET seq = ? WHERE path = ?", [(i, str(p)) for i, p in enumerate(paths)])
    guess["cluster"] = new
    conn.execute("UPDATE albums SET folder = ?, guess = ?, track_count = ? WHERE id = ?",
                 (str(Path(guess["cluster_folder"]) / f"{CLUSTER_KEY}{new}"), json.dumps(guess), len(paths), aid))
    return old, new


def _refresh_loose(st: State, folder: Path) -> None:
    """Re-read a folder's loose-tracks row from disk (it takes back every file no adopted cluster holds)."""
    found = next((a for a in collect_albums(folder) if a.path == folder), None) if folder.is_dir() else None
    root = root_for(folder, st)
    if found is None or root is None:
        return
    rows = _loose_rows(st, folder)
    aid, _changed = index_album(st, root, found, album_id=rows[0]["id"] if rows else None)
    events.album_changed.emit(aid)


def adopt_cluster(cluster_id: str) -> AlbumRow:
    """Make a cluster an album (index only; nothing on disk changes) so match / plan / apply take it.

    Its files leave their folder's own row; the album holds exactly them (no sidecars or extra
    folders: they stay put). Status UNMATCHED; artist and title are the cluster's, the Discogs
    search uses them. Idempotent: adopting again returns the same album.
    """
    st = state()
    c = cluster(cluster_id)
    if c.album_id is not None:
        return row_to_album_row(st.index.one("SELECT * FROM albums WHERE id = ?", (c.album_id,)))
    if c.kind != "tag_cluster":
        raise ValidationError("unclustered files can't be adopted: move them into a cluster first", field="cluster_id")
    if not c.files:
        raise ValidationError(f"cluster {cluster_id} has no files", field="cluster_id")
    root = root_for(c.folder, st) or root_for(c.files[0].path, st)
    if root is None:
        raise ValidationError(f"not inside a library root: {c.folder}", field="cluster_id")
    years = []
    try:
        guess = guess_from_album_dir(c.folder, [f.path for f in c.files])
        years = [y for y in (guess.get("originalyear", ""), guess.get("year", "")) if y]
    except Exception:  # noqa: BLE001
        guess = {}
    guess = {"artist": c.artist, "album": c.title, "year": min(years) if years else "",
             "catno": guess.get("catno", ""), "cluster": c.id, "cluster_folder": str(c.folder)}
    paths = [str(f.path) for f in c.files]
    marks = ", ".join("?" * len(paths))
    owners = {r["album_id"] for r in st.index.query(f"SELECT DISTINCT album_id FROM tracks WHERE path IN ({marks})",
                                                     paths)}
    with st.index.tx() as conn:
        conn.execute(f"DELETE FROM tracks WHERE path IN ({marks})", paths)
        conn.execute(f"DELETE FROM cluster_moves WHERE path IN ({marks})", paths)  # membership is the row now
        for owner in owners:
            conn.execute("UPDATE albums SET track_count = (SELECT COUNT(*) FROM tracks WHERE album_id = ?)"
                         " WHERE id = ?", (owner, owner))
        cur = conn.execute(
            "INSERT INTO albums(folder, root, artist, title, year, catno, artist_sort, track_count, status, added_at,"
            " guess) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (str(c.folder / f"{CLUSTER_KEY}{c.id}"), str(root), c.artist, c.title, guess["year"], guess["catno"],
             c.artist, len(c.files), MatchStatus.UNMATCHED.value, iso(now_utc()), json.dumps(guess)))
        aid = cur.lastrowid
        conn.executemany("INSERT INTO tracks(album_id, seq, path, disc, number, title, duration, size, mtime_ns)"
                         " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         [(aid, seq, str(f.path), f.disc, f.number, f.title, f.duration_s, *_stat(f.path))
                          for seq, f in enumerate(c.files)])
        st.index.update_fts(conn, aid, c.artist, c.title, " ".join(f.title for f in c.files))
    for owner in owners:
        events.album_changed.emit(owner)
    events.album_changed.emit(aid)
    events.clusters_changed.emit([c.id])
    return row_to_album_row(st.index.one("SELECT * FROM albums WHERE id = ?", (aid,)))


def unadopt_cluster(album_id: int) -> Cluster:
    """Back out adopt_cluster before any apply: the album is deleted (with its candidates, choice and
    pins) and its files go back to their folder's loose-tracks row. Index only. Returns the cluster
    they form again. MMError if the album was ever applied (undo that in History instead);
    ValidationError if it is not an adopted cluster.
    """
    st = state()
    row = st.index.one("SELECT * FROM albums WHERE id = ?", (int(album_id),))
    if row is None:
        raise NotFound(f"album {album_id}")
    applied = st.index.one("SELECT oa.op_id FROM operation_albums oa JOIN operations o ON o.id = oa.op_id"
                           " WHERE oa.album_id = ? AND o.kind = 'apply' ORDER BY oa.op_id DESC", (int(album_id),))
    if applied is not None:
        raise MMError(f"album {album_id} was applied (operation {applied['op_id']}): undo that in History instead")
    cg = cluster_guess(row)
    if not cg:
        raise ValidationError(f"album {album_id} is not an adopted cluster", field="album_id")
    folder = Path(cg["cluster_folder"])
    paths = [t.path for t in local_tracks(int(album_id), st)]
    with st.index.tx() as conn:
        conn.execute("DELETE FROM albums WHERE id = ?", (int(album_id),))
        st.index.delete_fts(conn, int(album_id))
    events.album_removed.emit(int(album_id))
    _refresh_loose(st, folder)
    found = _in_folder(st, folder)
    back = next((c for c in found if paths and any(f.path == paths[0] for f in c.files)), None)
    if back is None:
        raise NotFound(f"the files of album {album_id} are gone")
    events.clusters_changed.emit([back.id])
    return back


def _holding(found: list[Cluster], path: Path) -> Cluster | None:
    return next((c for c in found if any(f.path == path for f in c.files)), None)


def move_to_cluster(file: Path, cluster_id: str | None) -> Cluster:
    """Move one loose file into another cluster of its folder, or out of every cluster (None). Index only.

    Cluster ids are hashes of their files, so both clusters get new ids: returns the cluster the
    file is in now, and emits cluster_changed(old id, new id) for it, then for the cluster it left
    (new id "" when that one is now empty). Into an adopted cluster, the file joins that album;
    out of one, it goes back to the folder's loose-tracks row (an adopted cluster left empty is
    un-adopted). The move is kept across scans. ValidationError: not a loose or cluster file, or
    the target is in another folder.
    """
    st = state()
    path = Path(file)
    owner = st.index.one("SELECT a.* FROM tracks t JOIN albums a ON a.id = t.album_id WHERE t.path = ?", (str(path),))
    if owner is None:
        raise ValidationError(f"not a file of the library index: {path}", field="file")
    cg = cluster_guess(owner)
    if not cg and not json.loads(owner["guess"] or "{}").get("loose_tracks"):
        raise ValidationError(f"{path.name} belongs to the album folder {owner['folder']}: only loose files move "
                              "between clusters", field="file")
    folder = Path(cg["cluster_folder"]) if cg else Path(owner["folder"])
    before = _in_folder(st, folder)
    source = _holding(before, path)
    target = None
    if cluster_id is not None:
        target = next((c for c in before if c.id == cluster_id), None)
        if target is None:
            cluster(cluster_id)  # NotFound for an unknown id
            raise ValidationError(f"cluster {cluster_id} is in another folder than {path.name}", field="cluster_id")
    if target is not None and source is not None and target.id == source.id:
        return source
    old_target = target.id if target is not None else next((c.id for c in before if c.kind == "unclustered"), "")
    others = [f.path for f in source.files if f.path != path] if source is not None else []
    with st.index.tx() as conn:
        if cg:  # out of an adopted cluster
            conn.execute("DELETE FROM tracks WHERE path = ?", (str(path),))
            if conn.execute("SELECT COUNT(*) AS n FROM tracks WHERE album_id = ?", (owner["id"],)).fetchone()["n"]:
                _rekey(conn, owner["id"])
            else:
                conn.execute("DELETE FROM albums WHERE id = ?", (owner["id"],))
                st.index.delete_fts(conn, owner["id"])
        if target is not None and target.album_id is not None:  # into an adopted cluster
            conn.execute("DELETE FROM tracks WHERE path = ?", (str(path),))
            conn.execute("DELETE FROM cluster_moves WHERE path = ?", (str(path),))
            conn.execute("INSERT INTO tracks(album_id, seq, path, disc, number, title, duration, size, mtime_ns)"
                         " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)", _track_row(target.album_id, 10**6, path, folder))
            _rekey(conn, target.album_id)
            if not cg:
                conn.execute("UPDATE albums SET track_count = (SELECT COUNT(*) FROM tracks WHERE album_id = ?)"
                             " WHERE id = ?", (owner["id"], owner["id"]))
        else:
            key = "" if target is None or target.kind == "unclustered" else st.cluster_keys.get(target.id, "")
            conn.execute("INSERT OR REPLACE INTO cluster_moves(path, key) VALUES(?, ?)", (str(path), key))
    if cg and (target is None or target.album_id is None):
        _refresh_loose(st, folder)
    for aid in {owner["id"], target.album_id if target is not None else None} - {None}:
        if st.index.one("SELECT id FROM albums WHERE id = ?", (aid,)) is None:
            events.album_removed.emit(aid)  # an adopted cluster left empty
        else:
            events.album_changed.emit(aid)
    after = _in_folder(st, folder)
    now = _holding(after, path)
    if now is None:
        raise NotFound(f"{path} is gone")
    left = next((c.id for c in after if others and any(f.path == others[0] for f in c.files)), "")
    events.cluster_changed.emit(old_target, now.id)
    if source is not None:
        events.cluster_changed.emit(source.id, left)
    return now


__all__ = ["adopt_cluster", "album", "albums", "cluster", "clusters", "move_to_cluster", "scan", "scan_clusters",
           "search", "status_counts", "unadopt_cluster"]
