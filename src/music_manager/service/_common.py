"""Helpers shared by the service namespaces: index rows to API types, statuses, roots."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from ..fs import AlbumFolder
from ._state import State, events, state
from ._types import (
    Album,
    AlbumRow,
    LocalTrack,
    MatchStatus,
    NotFound,
    Problem,
    ProblemKind,
)


def now_utc() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def from_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def problems_to_json(problems: list[Problem]) -> str:
    return json.dumps([{"kind": str(p.kind), "message": p.message, "paths": [str(x) for x in p.paths]}
                       for p in problems], ensure_ascii=False)


def problems_from_json(text: str | None) -> list[Problem]:
    out = []
    for item in json.loads(text or "[]"):
        try:
            kind = ProblemKind(item["kind"])
        except (KeyError, ValueError):
            continue
        out.append(Problem(kind, item.get("message", ""), [Path(p) for p in item.get("paths", [])]))
    return out


def row_to_album_row(row: sqlite3.Row) -> AlbumRow:
    return AlbumRow(
        id=row["id"],
        folder=Path(row["folder"]),
        artist=row["artist"],
        title=row["title"],
        year=row["year"],
        catno=row["catno"],
        track_count=row["track_count"],
        status=MatchStatus(row["status"]),
        confidence=row["confidence"],
        artwork=Path(row["artwork"]) if row["artwork"] else None,
        artist_sort=row["artist_sort"],
        added_at=from_iso(row["added_at"]),
        held_for=problems_from_json(row["held_for"]),
    )


def album_record(album_id: int, st: State | None = None) -> sqlite3.Row:
    st = st or state()
    row = st.index.one("SELECT * FROM albums WHERE id = ?", (album_id,))
    if row is None:
        raise NotFound(f"album {album_id}")
    return row


def local_tracks(album_id: int, st: State | None = None) -> list[LocalTrack]:
    st = st or state()
    rows = st.index.query("SELECT * FROM tracks WHERE album_id = ? ORDER BY seq", (album_id,))
    return [LocalTrack(r["id"], Path(r["path"]), r["disc"], r["number"], r["title"], r["duration"]) for r in rows]


def load_album(album_id: int, st: State | None = None) -> Album:
    st = st or state()
    row = album_record(album_id, st)
    base = row_to_album_row(row)
    extras = [Path(p) for p in json.loads(row["extras"] or "[]")]
    return Album(**base.__dict__, tracks=local_tracks(album_id, st), extras=extras)


def album_folder(album_id: int, st: State | None = None) -> AlbumFolder:
    """The album as the engine sees it now: re-collected from disk when the folder exists."""
    st = st or state()
    row = album_record(album_id, st)
    folder = Path(row["folder"])
    from ..fs import collect_albums

    if folder.is_dir():
        for found in collect_albums(folder):
            if found.path == folder:
                return found
    tracks = [t.path for t in local_tracks(album_id, st)]
    return AlbumFolder(folder, tracks)


def root_for(folder: Path, st: State | None = None) -> Path | None:
    st = st or state()
    roots = sorted(st.config().library_roots, key=lambda r: len(str(r)), reverse=True)
    for root in roots:
        if folder == root or root in folder.parents:
            return root
    return None


def set_status(album_id: int, status: MatchStatus, *, held_for: list[Problem] | None = None,
               emit: bool = True, **columns) -> None:
    """Write an album's status. held_for is cleared unless the status is HELD."""
    st = state()
    held = problems_to_json(held_for or []) if status is MatchStatus.HELD else "[]"
    sets = ["status = ?", "held_for = ?"] + [f"{k} = ?" for k in columns]
    with st.index.tx() as conn:
        cur = conn.execute(f"UPDATE albums SET {', '.join(sets)} WHERE id = ?",
                           (status.value, held, *columns.values(), album_id))
        if cur.rowcount == 0:
            raise NotFound(f"album {album_id}")
    if emit:
        events.album_changed.emit(album_id)
