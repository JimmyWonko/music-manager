"""The library index: SQLite (stdlib sqlite3) at $XDG_DATA_HOME/music-manager/library.db, WAL mode.

A cache of what the scanner saw plus the user's match decisions (status,
chosen release, held problems) and the operation history. Files and tags
stay the source of truth: deleting the database loses only those decisions
and the history list (journals stay on disk).

One connection, shared by the UI thread and the job worker, behind a lock.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS albums(
    id INTEGER PRIMARY KEY,
    folder TEXT NOT NULL UNIQUE,
    root TEXT NOT NULL,
    artist TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    year TEXT NOT NULL DEFAULT '',
    catno TEXT NOT NULL DEFAULT '',
    artist_sort TEXT NOT NULL DEFAULT '',
    track_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    confidence REAL,
    artwork TEXT,
    added_at TEXT NOT NULL,
    held_for TEXT NOT NULL DEFAULT '[]',
    chosen_release INTEGER,
    guess TEXT NOT NULL DEFAULT '{}',
    extras TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS tracks(
    id INTEGER PRIMARY KEY,
    album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    path TEXT NOT NULL UNIQUE,
    disc INTEGER NOT NULL DEFAULT 1,
    number INTEGER,
    title TEXT NOT NULL DEFAULT '',
    duration REAL,
    size INTEGER,
    mtime_ns INTEGER
);
CREATE INDEX IF NOT EXISTS tracks_album ON tracks(album_id, seq);
CREATE TABLE IF NOT EXISTS candidates(
    album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    release_id INTEGER NOT NULL,
    rank INTEGER NOT NULL,
    search TEXT NOT NULL,
    release TEXT,
    master_year TEXT,
    fetched_at REAL,
    PRIMARY KEY(album_id, release_id)
);
CREATE TABLE IF NOT EXISTS operations(
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    started TEXT NOT NULL,
    finished TEXT,
    albums INTEGER NOT NULL DEFAULT 0,
    files INTEGER NOT NULL DEFAULT 0,
    summary TEXT NOT NULL DEFAULT '',
    journal TEXT,
    journal_format INTEGER NOT NULL DEFAULT 2,
    undone INTEGER NOT NULL DEFAULT 0,
    undone_by INTEGER,
    undo_of INTEGER
);
CREATE TABLE IF NOT EXISTS operation_albums(
    op_id INTEGER NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    album_id INTEGER NOT NULL,
    folder_before TEXT NOT NULL,
    folder_after TEXT NOT NULL,
    release_id INTEGER,
    PRIMARY KEY(op_id, album_id)
);
"""


class LibraryIndex:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.create_function("cf", 1, lambda s: (s or "").casefold(), deterministic=True)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.fts = self._init_fts()
        self.conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('schema', ?)", (str(SCHEMA_VERSION),))

    def _init_fts(self) -> bool:
        try:
            self.conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS album_fts USING fts5(artist, title, tracks)")
            return True
        except sqlite3.OperationalError:  # SQLite built without FTS5: search falls back to LIKE
            return False

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """One write transaction (BEGIN IMMEDIATE ... COMMIT / ROLLBACK)."""
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    def query(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params=()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def update_fts(self, conn: sqlite3.Connection, album_id: int, artist: str, title: str, tracks: str) -> None:
        if not self.fts:
            return
        conn.execute("DELETE FROM album_fts WHERE rowid = ?", (album_id,))
        conn.execute("INSERT INTO album_fts(rowid, artist, title, tracks) VALUES(?, ?, ?, ?)",
                     (album_id, artist, title, tracks))

    def delete_fts(self, conn: sqlite3.Connection, album_id: int) -> None:
        if self.fts:
            conn.execute("DELETE FROM album_fts WHERE rowid = ?", (album_id,))
