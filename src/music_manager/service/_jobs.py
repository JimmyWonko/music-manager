"""The job runner: one worker thread runs jobs one at a time, in order.

Disk writes (apply, undo) and Discogs requests are serial by design: one
rate limit, one journal at a time. A job's work function gets a JobContext
to report progress and to honour pause and cancel between steps.
"""

from __future__ import annotations

import itertools
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import replace

from ._types import Job, JobKind, JobState, MMError, NotFound

FINAL = (JobState.DONE, JobState.FAILED, JobState.CANCELLED)


class JobCancelled(Exception):
    pass


class JobContext:
    def __init__(self, runner: JobRunner, job_id: int):
        self._runner = runner
        self.job_id = job_id

    def check(self) -> None:
        """Between steps: wait while paused, stop when cancelled."""
        self._runner._checkpoint(self.job_id)

    def progress(self, done: int, total: int | None = None, message: str | None = None, check: bool = True) -> None:
        """Report progress; with check, also wait while paused and stop when cancelled."""
        self._runner._progress(self.job_id, done, total, message, check)

    @property
    def cancelled(self) -> bool:
        return self.job_id in self._runner._cancel


class JobRunner:
    def __init__(self, events):
        self.events = events
        self._ids = itertools.count(1)
        self._jobs: dict[int, Job] = {}
        self._work: dict[int, Callable[[JobContext], str | None]] = {}
        self._queue: deque[int] = deque()
        self._cancel: set[int] = set()
        self._paused: set[int] = set()
        self._cond = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stopping = False
        self._outbox: deque[Job] = deque()
        self._announced: set[int] = set()  # finished jobs whose job_finished has been emitted

    # --- public -------------------------------------------------------------
    def submit(self, kind: JobKind, work: Callable[[JobContext], str | None], total: int | None = None,
               message: str = "") -> Job:
        with self._cond:
            job = Job(next(self._ids), kind, JobState.QUEUED, 0, total, message)
            self._jobs[job.id] = job
            self._work[job.id] = work
            self._queue.append(job.id)
            if self._thread is None or not self._thread.is_alive():
                self._stopping = False
                self._thread = threading.Thread(target=self._loop, name="music-manager-jobs", daemon=True)
                self._thread.start()
            self._cond.notify_all()
        return job

    def get(self, job_id: int) -> Job:
        with self._cond:
            try:
                return self._jobs[job_id]
            except KeyError:
                raise NotFound(f"job {job_id}") from None

    def cancel(self, job_id: int) -> None:
        with self._cond:
            job = self.get(job_id)
            if job.state in FINAL:
                return
            self._cancel.add(job_id)
            self._paused.discard(job_id)
            if job_id in self._queue:
                self._queue.remove(job_id)
                self._finish(job_id, JobState.CANCELLED, "cancelled before it started")
            self._cond.notify_all()
        self._flush()

    def pause(self, job_id: int) -> None:
        with self._cond:
            job = self.get(job_id)
            if job.state in FINAL:
                return
            self._paused.add(job_id)
            self._set(job_id, state=JobState.PAUSED)
            self._cond.notify_all()

    def resume(self, job_id: int) -> None:
        with self._cond:
            job = self.get(job_id)
            if job.state is not JobState.PAUSED:
                return
            self._paused.discard(job_id)
            running = job_id not in self._queue
            self._set(job_id, state=JobState.RUNNING if running else JobState.QUEUED)
            self._cond.notify_all()

    def wait(self, job_id: int, timeout: float | None = 30.0) -> Job:
        """Block until the job is DONE, FAILED or CANCELLED and job_finished was emitted (CLI and tests only)."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while self.get(job_id).state not in FINAL or job_id not in self._announced:
                left = None if deadline is None else deadline - time.monotonic()
                if left is not None and left <= 0:
                    raise TimeoutError(f"job {job_id} still {self.get(job_id).state.name}")
                self._cond.wait(left)
            return self._jobs[job_id]

    def idle(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        with self._cond:
            while any(j.state not in FINAL or i not in self._announced for i, j in self._jobs.items()):
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError("jobs still running")
                self._cond.wait(left)

    def shutdown(self) -> None:
        with self._cond:
            self._stopping = True
            for job_id in list(self._queue):
                self._queue.remove(job_id)
                self._finish(job_id, JobState.CANCELLED, "shut down")
            for job_id, job in self._jobs.items():
                if job.state not in FINAL:
                    self._cancel.add(job_id)
            self._paused.clear()
            self._cond.notify_all()
            thread = self._thread
        self._flush()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=10)

    # --- worker ---------------------------------------------------------------
    def _next(self) -> int | None:
        with self._cond:
            while True:
                if self._stopping:
                    return None
                for job_id in self._queue:
                    if job_id not in self._paused:
                        self._queue.remove(job_id)
                        return job_id
                self._cond.wait(1.0)

    def _loop(self) -> None:
        while True:
            job_id = self._next()
            if job_id is None:
                return
            work = self._work.pop(job_id)
            with self._cond:
                job = self._set(job_id, state=JobState.RUNNING)
            self.events.job_progress.emit(job)
            ctx = JobContext(self, job_id)
            try:
                ctx.check()
                message = work(ctx)
            except JobCancelled:
                with self._cond:
                    self._finish(job_id, JobState.CANCELLED, "cancelled")
                self._flush()
            except MMError as exc:
                with self._cond:
                    self._finish(job_id, JobState.FAILED, str(exc))
                self._flush()
            except Exception as exc:  # noqa: BLE001 - a job failure is reported, never raised into the UI
                with self._cond:
                    self._finish(job_id, JobState.FAILED, f"{type(exc).__name__}: {exc}")
                self._flush()
            else:
                with self._cond:
                    current = self._jobs[job_id]
                    done = current.total if current.total is not None else max(current.done, 1)
                    self._set(job_id, done=done)
                    self._finish(job_id, JobState.DONE, message if message is not None else current.message)
                self._flush()

    def _checkpoint(self, job_id: int) -> None:
        with self._cond:
            while job_id in self._paused and job_id not in self._cancel:
                self._cond.wait(0.5)
            if job_id in self._cancel:
                raise JobCancelled()

    def _progress(self, job_id: int, done: int, total: int | None, message: str | None, check: bool = True) -> None:
        with self._cond:
            changes = {"done": done}
            if total is not None:
                changes["total"] = total
            if message is not None:
                changes["message"] = message
            job = self._set(job_id, **changes)
        self.events.job_progress.emit(job)
        if check:
            self._checkpoint(job_id)

    def _set(self, job_id: int, **changes) -> Job:
        job = replace(self._jobs[job_id], **changes)
        self._jobs[job_id] = job
        return job

    def _finish(self, job_id: int, state: JobState, message: str) -> None:
        job = self._set(job_id, state=state, message=message)
        self._cancel.discard(job_id)
        self._paused.discard(job_id)
        self._cond.notify_all()
        self._outbox.append(job)

    def _flush(self) -> None:
        """Emit finished jobs; called with the lock released."""
        while True:
            with self._cond:
                if not self._outbox:
                    return
                job = self._outbox.popleft()
            try:
                self.events.job_progress.emit(job)
                self.events.job_finished.emit(job)
            finally:
                with self._cond:
                    self._announced.add(job.id)
                    self._cond.notify_all()
