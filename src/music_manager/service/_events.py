"""service.events: one object whose signals report job progress and index changes.

With PySide6 installed this is a QObject with Qt Signals: they are emitted
from the worker thread, and a default (Auto) connection queues them onto the
receiver's thread. Without PySide6 (CLI, tests) a small stand-in with the
same connect/disconnect/emit calls is used; its slots run on the emitting
thread.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

try:  # pragma: no cover - depends on the environment
    from PySide6.QtCore import QObject, Signal

    class Events(QObject):
        job_progress = Signal(object)  # Job
        job_finished = Signal(object)  # Job (state DONE/FAILED/CANCELLED)
        album_changed = Signal(int)  # album_id: refetch it
        candidate_ready = Signal(int, int)  # album_id, release_id: full release fetched
        rate_limited = Signal(float)  # seconds until the next Discogs request
        clusters_changed = Signal(object)  # list[str]: cluster ids now current (after a cluster scan, adopt, ...)
        cluster_changed = Signal(str, str)  # old id, new id ("" = gone): a cluster's files changed
        match_view_changed = Signal(int)  # album_id: match.view changed (pins)

    QT = True
except ImportError:  # pragma: no cover - depends on the environment
    QT = False

    class _Signal:
        def __init__(self) -> None:
            self._slots: list[Callable] = []
            self._lock = threading.Lock()

        def connect(self, slot: Callable) -> None:
            with self._lock:
                self._slots.append(slot)

        def disconnect(self, slot: Callable | None = None) -> None:
            with self._lock:
                if slot is None:
                    self._slots.clear()
                else:
                    self._slots.remove(slot)

        def emit(self, *args) -> None:
            with self._lock:
                slots = list(self._slots)
            for slot in slots:
                slot(*args)

    class Events:  # type: ignore[no-redef]
        def __init__(self) -> None:
            self.job_progress = _Signal()
            self.job_finished = _Signal()
            self.album_changed = _Signal()
            self.candidate_ready = _Signal()
            self.rate_limited = _Signal()
            self.clusters_changed = _Signal()
            self.cluster_changed = _Signal()
            self.match_view_changed = _Signal()
