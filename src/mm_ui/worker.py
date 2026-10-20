"""Run one blocking backend call off the Qt thread and deliver the result back on it.

The real service's plan.build can fetch releases from Discogs synchronously (API-DEVIATIONS #5),
so the UI never calls it on the Qt thread. Results come back through a QObject living on the UI
thread, so the default (Auto) connection queues them there.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

_live: set = set()      # keep the signal objects alive until they've delivered


class _Relay(QObject):
    done = Signal(object)
    failed = Signal(object)


class _Call(QRunnable):
    def __init__(self, fn, relay):
        super().__init__()
        self.fn, self.relay = fn, relay
        self.setAutoDelete(True)

    def run(self):
        try:
            result = self.fn()
        except Exception as e:      # delivered to on_error on the UI thread
            self.relay.failed.emit(e)
        else:
            self.relay.done.emit(result)


def run_async(fn, on_done, on_error) -> _Relay:
    relay = _Relay()            # created on the calling (UI) thread: its slots run there

    def finish(cb, value):
        _live.discard(relay)
        cb(value)
    relay.done.connect(lambda r: finish(on_done, r))
    relay.failed.connect(lambda e: finish(on_error, e))
    _live.add(relay)
    QThreadPool.globalInstance().start(_Call(fn, relay))
    return relay
