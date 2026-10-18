"""Helpers shared by the service namespaces: index rows to API types, statuses, roots."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from ..conflicts import DiscChoice, DiscConflict, DiscFolderMismatch
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


def title_match_min() -> float:
    """The title-pass pairing threshold every service pairing uses: match.auto_rules().title_match_min."""
    from .match import auto_rules

    return auto_rules().title_match_min


def now_utc() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def from_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _choice_json(c: DiscChoice) -> dict:
    return {"disc": c.disc, "position": c.position, "number": c.number, "title": c.title,
            "title_similarity": c.title_similarity}


def _conflict_json(c: DiscConflict) -> dict:
    return {"path": str(c.path), "number": c.number, "title": c.title, "reason": c.reason,
            "same_number": [_choice_json(x) for x in c.same_number],
            "title_matches": [_choice_json(x) for x in c.title_matches],
            "folder_disc": c.folder_disc, "tag_disc": c.tag_disc, "sidecar_disc": c.sidecar_disc,
            "sidecar": str(c.sidecar) if c.sidecar is not None else None}


def problems_to_json(problems: list[Problem]) -> str:
    out = []
    for p in problems:
        item: dict = {"kind": str(p.kind), "message": p.message, "paths": [str(x) for x in p.paths]}
        if p.disc_conflicts:
            item["disc_conflicts"] = [_conflict_json(c) for c in p.disc_conflicts]
        if p.layout_reason or p.blocking_subdirs or p.loose_files:
            item["layout_reason"] = p.layout_reason
            item["blocking_subdirs"] = [str(x) for x in p.blocking_subdirs]
            item["loose_files"] = [str(x) for x in p.loose_files]
        if p.folder_mismatches:
            item["folder_mismatches"] = [{"path": str(m.path), "destination": str(m.destination),
                                          "folder": str(m.folder), "folder_disc": m.folder_disc,
                                          "tag_disc": m.tag_disc} for m in p.folder_mismatches]
        out.append(item)
    return json.dumps(out, ensure_ascii=False)


def _choice_from(d: dict) -> DiscChoice:
    return DiscChoice(int(d["disc"]), d.get("position", ""), int(d["number"]), d.get("title", ""),
                      float(d.get("title_similarity", 0.0)))


def _conflict_from(d: dict) -> DiscConflict:
    return DiscConflict(Path(d["path"]), d.get("number"), d.get("title", ""), d.get("reason", "numbering"),
                        [_choice_from(x) for x in d.get("same_number", [])],
                        [_choice_from(x) for x in d.get("title_matches", [])],
                        d.get("folder_disc"), d.get("tag_disc"), d.get("sidecar_disc"),
                        Path(d["sidecar"]) if d.get("sidecar") else None)


def problems_from_json(text: str | None) -> list[Problem]:
    out = []
    for item in json.loads(text or "[]"):
        try:
            kind = ProblemKind(item["kind"])
        except (KeyError, ValueError):
            continue
        out.append(Problem(
            kind, item.get("message", ""), [Path(p) for p in item.get("paths", [])],
            disc_conflicts=[_conflict_from(c) for c in item.get("disc_conflicts", [])],
            layout_reason=item.get("layout_reason", ""),
            blocking_subdirs=[Path(x) for x in item.get("blocking_subdirs", [])],
            loose_files=[Path(x) for x in item.get("loose_files", [])],
            folder_mismatches=[DiscFolderMismatch(Path(m["path"]), Path(m["destination"]), Path(m["folder"]),
                                                  int(m["folder_disc"]), int(m["tag_disc"]))
                               for m in item.get("folder_mismatches", [])],
        ))
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
