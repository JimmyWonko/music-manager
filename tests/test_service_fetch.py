"""plan.build never downloads on the calling (UI) thread: missing or stale releases come from a job."""

from __future__ import annotations

import threading
import time

import pytest
from service_fixtures import Library, direct, tree
from test_discogs_pacing import FakeClock, Resp

from music_manager import service
from music_manager.providers.discogs import Discogs
from music_manager.service import JobKind, JobState, MatchStatus, ProblemKind
from music_manager.service._state import state


@pytest.fixture
def lib(tmp_path):
    library = Library(tmp_path).start(tmp_path)
    yield library
    service.reset()


def wait(job_id):
    return service.jobs.wait(job_id)


def accept(lib, folder, release_id):
    aid = lib.album_id(folder)
    lib.find(aid)
    service.match.choose(aid, release_id)
    return aid


def make_stale(aid, release_id, forget=False):
    with state().index.tx() as conn:
        if forget:
            conn.execute("UPDATE candidates SET release = NULL WHERE album_id = ? AND release_id = ?",
                         (aid, release_id))
        else:
            conn.execute("UPDATE candidates SET fetched_at = ? WHERE album_id = ? AND release_id = ?",
                         (time.time() - 7 * 3600, aid, release_id))


class Gate:
    """Wraps FakeDiscogs.release: records the calling thread and blocks until opened."""

    def __init__(self, discogs):
        self.inner = discogs.release
        self.open = threading.Event()
        self.threads: list[threading.Thread] = []
        discogs.release = self

    def __call__(self, release_id):
        self.threads.append(threading.current_thread())
        assert self.open.wait(10), "test never opened the gate"
        return self.inner(release_id)


@pytest.mark.parametrize("forget", [False, True], ids=["stale", "never-fetched"])
def test_plan_build_returns_at_once_and_fetches_on_the_worker(lib, forget):
    d = accept(lib, lib.double, 1000004)
    make_stale(d, 1000004, forget=forget)
    gate = Gate(lib.discogs)
    ready = []
    direct(service.events.candidate_ready, lambda a, r: ready.append((a, r)))
    before = tree(lib.root)
    started = time.monotonic()
    plan = service.plan.build([d])
    assert time.monotonic() - started < 2.0  # the worker is blocked in the gate: build did not wait for it
    assert tree(lib.root) == before
    assert all(t is not threading.main_thread() for t in gate.threads)
    (album,) = plan.albums
    assert [p.kind for p in album.problems] == [ProblemKind.RELEASE_NOT_FETCHED]
    assert album.files == [] and album.release_id == 1000004
    assert plan.fetch_job is not None and (plan.files_to_move, plan.tags_to_write) == (0, 0)
    assert any("being fetched" in w for w in plan.warnings)
    job = service.jobs.get(plan.fetch_job)
    assert job.kind is JobKind.FETCH_CANDIDATES
    assert service.plan.build([d]).fetch_job == plan.fetch_job  # in flight: reused, not queued twice
    gate.open.set()
    done = wait(plan.fetch_job)
    assert done.state is JobState.DONE, done.message
    assert gate.threads and all(t is not threading.main_thread() for t in gate.threads)
    assert ready == [(d, 1000004)]
    calls = len(lib.discogs.calls)
    again = service.plan.build([d])
    assert again.fetch_job is None and again.albums[0].problems == []
    assert len(again.albums[0].files) > 0 and again.files_to_move > 0
    assert len(lib.discogs.calls) == calls  # the rebuild made no Discogs call
    assert service.library.album(d).status is MatchStatus.ACCEPTED  # build never writes a status


def test_apply_leaves_out_unfetched_albums_without_holding_them(lib):
    d = accept(lib, lib.double, 1000004)
    s = accept(lib, lib.side, lib.vinyl["id"])
    make_stale(s, lib.vinyl["id"])
    plan = service.plan.build([d, s], allow_track_mismatch={s})
    assert [a.album_id for a in plan.albums] == [d, s]
    assert plan.albums[0].problems == [] and plan.albums[1].problems[0].kind is ProblemKind.RELEASE_NOT_FETCHED
    wait(plan.fetch_job)
    job = wait(service.plan.apply(plan.id).id)
    assert job.state is JobState.DONE, job.message
    assert "left out 1" in job.message
    assert service.library.album(d).status is MatchStatus.APPLIED
    side = service.library.album(s)
    assert side.status is MatchStatus.ACCEPTED and side.held_for == []


def test_failed_fetch_is_reported_and_not_retried_in_a_loop(lib):
    d = accept(lib, lib.double, 1000004)
    make_stale(d, 1000004, forget=True)
    lib.discogs.fail_release.add(1000004)
    plan = service.plan.build([d])
    job = wait(plan.fetch_job)
    assert job.state is JobState.FAILED and "1000004" in job.message
    calls = len(lib.discogs.calls)
    again = service.plan.build([d])
    assert again.fetch_job is None and len(lib.discogs.calls) == calls
    (problem,) = again.albums[0].problems
    assert problem.kind is ProblemKind.RELEASE_NOT_FETCHED and "could not be fetched" in problem.message
    assert any("could not be fetched" in w for w in again.warnings)


def test_real_client_paces_the_fetch_job_and_reports_rate_limits(lib):
    """The real Discogs client on a fake clock and transport: pauses and 429s reach events.rate_limited."""
    d = accept(lib, lib.double, 1000004)
    make_stale(d, 1000004)
    clock = FakeClock()
    client = Discogs("token", None, None)
    client.clock, client.sleeper = clock, clock.sleep
    client._last = clock.now  # a request just finished: the next one must wait

    class Transport:
        def __init__(self):
            self.calls: list[tuple[str, threading.Thread, float]] = []
            self.queue = [Resp(429, {"Retry-After": "3"})]

        def get(self, url, params=None, timeout=None):
            self.calls.append((url, threading.current_thread(), clock.now))
            clock.now += 0.05
            if self.queue:
                return self.queue.pop(0)
            if "/masters/" in url:
                return Resp(body={"year": 1998})
            return Resp(body=lib.multi)

    transport = Transport()
    client.session = transport
    state().discogs = client
    paused = []
    direct(service.events.rate_limited, lambda s: paused.append((s, threading.current_thread())))
    plan = service.plan.build([d])
    assert transport.calls == []  # nothing on the calling thread
    done = wait(plan.fetch_job)
    assert done.state is JobState.DONE, done.message
    urls = [u for u, _t, _c in transport.calls]
    assert urls[0].endswith("/releases/1000004") and urls[1].endswith("/releases/1000004")
    assert urls[2].endswith("/masters/2000004")
    assert all(t is not threading.main_thread() for _u, t, _c in transport.calls)
    seconds = [s for s, _t in paused]
    assert seconds[0] == pytest.approx(1.1) and 3.0 in seconds
    assert all(t is not threading.main_thread() for _s, t in paused)
    starts = [c for _u, _t, c in transport.calls]
    assert all(b - a >= 1.1 for a, b in zip(starts, starts[1:], strict=False))
    assert service.plan.build([d]).albums[0].problems == []
