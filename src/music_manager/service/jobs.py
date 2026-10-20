"""service.jobs: state and control of background jobs."""

from __future__ import annotations

from ._state import state
from ._types import Job


def get(job_id: int) -> Job:
    return state().runner.get(int(job_id))


def cancel(job_id: int) -> None:
    state().runner.cancel(int(job_id))


def pause(job_id: int) -> None:
    state().runner.pause(int(job_id))


def resume(job_id: int) -> None:
    state().runner.resume(int(job_id))


def wait(job_id: int, timeout: float | None = 30.0) -> Job:
    """Block until the job finishes. Not part of the UI contract: for the CLI and tests, never the Qt thread."""
    return state().runner.wait(int(job_id), timeout)


__all__ = ["cancel", "get", "pause", "resume", "wait"]
