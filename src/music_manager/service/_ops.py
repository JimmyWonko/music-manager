"""Operation history in the index, and the journals behind it."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from .. import journal as journal_mod
from ._common import from_iso, iso, now_utc
from ._state import State, state
from ._types import NotFound, Operation


def journal_dir(st: State | None = None) -> Path:
    """Where the service's own journals go."""
    st = st or state()
    return st.data_dir / "journals"


def record_operation(st: State, *, kind: str, started: datetime, finished: datetime | None, albums: int, files: int,
                     summary: str, journal: Path | None, journal_format: int = 2, undo_of: int | None = None,
                     album_rows: list[tuple[int, Path, Path, int | None]] = ()) -> int:
    with st.index.tx() as conn:
        cur = conn.execute(
            "INSERT INTO operations(kind, started, finished, albums, files, summary, journal, journal_format, undo_of)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, iso(started), iso(finished) if finished else None, albums, files, summary,
             str(journal) if journal else None, journal_format, undo_of))
        op_id = cur.lastrowid
        conn.executemany(
            "INSERT OR REPLACE INTO operation_albums(op_id, album_id, folder_before, folder_after, release_id)"
            " VALUES(?, ?, ?, ?, ?)",
            [(op_id, aid, str(before), str(after), rid) for aid, before, after, rid in album_rows])
    return op_id


def operation_row(op_id: int, st: State | None = None) -> sqlite3.Row:
    st = st or state()
    row = st.index.one("SELECT * FROM operations WHERE id = ?", (op_id,))
    if row is None:
        raise NotFound(f"operation {op_id}")
    return row


def to_operation(row: sqlite3.Row) -> Operation:
    journal = row["journal"]
    exists = bool(journal) and Path(journal).is_file()
    fmt = int(row["journal_format"] or 2)
    return Operation(
        id=row["id"],
        started=from_iso(row["started"]),
        finished=from_iso(row["finished"]),
        albums=row["albums"],
        files=row["files"],
        kind=row["kind"],
        undoable=row["kind"] != "undo" and not row["undone"] and exists,
        undone=bool(row["undone"]),
        undone_by=row["undone_by"],
        undo_of=row["undo_of"],
        summary=row["summary"],
        journal_format=fmt,
        legacy=fmt < 2,
    )


def journal_format(entries: list[dict]) -> int:
    """1 when any tag snapshot is an old pickle one (no "v", has "data"), else 2."""
    for entry in entries:
        snap = entry.get("snapshot")
        if entry.get("action") == "tags" and isinstance(snap, dict):
            if snap.get("kind") != "none" and "v" not in snap and "data" in snap:
                return 1
    return 2


def _entry_time(entry: dict) -> datetime | None:
    text = entry.get("time") or ""
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return dt.astimezone() if dt.tzinfo is None else dt
    return None


def register_cli_journals(st: State) -> None:
    """Add journals written by discogs-tag (and not yet listed) to the history."""
    known = {r["journal"] for r in st.index.query("SELECT journal FROM operations WHERE journal IS NOT NULL")}
    folders = [journal_mod.state_dir() / "journal", journal_dir(st)]
    paths = []
    for folder in folders:
        if folder.is_dir():
            paths += sorted(folder.glob("*.jsonl"))
    for path in paths:
        if str(path) in known:
            continue
        try:
            entries = journal_mod.read_journal(path, log=lambda _line: None)
        except OSError:
            continue
        if not entries:
            continue
        kind = path.name.split("-", 1)[0]
        kind = kind if kind in ("apply", "undo", "normalize") else "apply"
        times = [t for t in (_entry_time(e) for e in entries) if t is not None]
        started = times[0] if times else datetime.fromtimestamp(path.stat().st_mtime).astimezone()
        finished = times[-1] if times else started
        starts = [e for e in entries if e.get("action") == "start"]
        files = sum(1 for e in entries if e.get("action") == "rename")
        undo_of = None
        if kind == "undo":
            target = next((s.get("journal") for s in starts if s.get("journal")), None)
            row = st.index.one("SELECT id FROM operations WHERE journal = ?", (target,)) if target else None
            undo_of = row["id"] if row else None
            summary = f"Undid (discogs-tag): {Path(target).name if target else path.name}"
        else:
            names = [Path(s.get("album") or s.get("root") or "").name for s in starts]
            summary = f"{kind} (discogs-tag): " + (", ".join(n for n in names if n) or path.name)
        rows = []
        for s in starts:
            if s.get("album") and s.get("dest"):
                found = st.index.one("SELECT id FROM albums WHERE folder IN (?, ?)", (s["dest"], s["album"]))
                if found:
                    rows.append((found["id"], Path(s["album"]), Path(s["dest"]), None))
        op_id = record_operation(st, kind=kind, started=started, finished=finished, albums=len(starts), files=files,
                                 summary=summary, journal=path, journal_format=journal_format(entries),
                                 undo_of=undo_of, album_rows=rows)
        if undo_of:
            with st.index.tx() as conn:
                conn.execute("UPDATE operations SET undone_by = ? WHERE id = ?", (op_id, undo_of))


def started_now() -> datetime:
    return now_utc()


