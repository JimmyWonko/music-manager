"""The Discogs client's pacing and 429 handling, on a fake clock and a fake transport (no network)."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from email.utils import format_datetime

import pytest

from music_manager.providers import discogs as discogs_mod
from music_manager.providers.discogs import Discogs, DiscogsError, retry_after_seconds


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class Resp:
    def __init__(self, status=200, headers=None, body=None):
        self.status_code = status
        self.headers = headers or {}
        self._body = {"id": 1} if body is None else body
        self.text = str(self._body)

    def json(self):
        return self._body


class FakeTransport:
    """session.get stand-in: records the fake time each request starts; each takes `latency` seconds."""

    def __init__(self, clock: FakeClock, responses=None, latency=0.05):
        self.clock = clock
        self.latency = latency
        self.responses = list(responses or [])
        self.starts: list[float] = []

    def get(self, url, params=None, timeout=None):
        self.starts.append(self.clock.now)
        self.clock.now += self.latency
        return self.responses.pop(0) if self.responses else Resp(body={"id": url.rsplit("/", 1)[-1]})


def paced_client(responses=None, latency=0.05):
    clock = FakeClock()
    client = Discogs("token", None, None)
    client.clock, client.sleeper = clock, clock.sleep
    transport = FakeTransport(clock, responses, latency)
    client.session = transport
    waits: list[float] = []
    client.on_wait = waits.append
    return client, clock, transport, waits


def test_pacing_stays_under_60_requests_a_minute():
    client, clock, transport, waits = paced_client()
    for rid in range(150):  # distinct ids: every call reaches the transport
        client.release(rid)
    starts = transport.starts
    assert len(starts) == 150
    gaps = [b - a for a, b in zip(starts, starts[1:], strict=False)]
    assert min(gaps) >= discogs_mod.MIN_INTERVAL
    worst = max(sum(1 for t in starts if s <= t < s + 60.0) for s in starts)
    assert worst <= 55 < 60
    assert waits and all(w > 0 for w in waits)  # each pause is reported (events.rate_limited in the service)
    assert clock.slept == waits


def test_interval_counts_from_the_end_of_the_previous_response():
    client, _clock, transport, waits = paced_client(latency=2.0)
    for rid in range(5):
        client.release(rid)
    assert waits == [discogs_mod.MIN_INTERVAL] * 4  # slow responses never shorten the pause
    gaps = [b - a for a, b in zip(transport.starts, transport.starts[1:], strict=False)]
    assert gaps == [pytest.approx(2.0 + discogs_mod.MIN_INTERVAL)] * 4


def test_cached_responses_make_no_request_and_no_wait():
    client, _clock, transport, waits = paced_client()
    client.release(7)
    client.release(7)
    assert len(transport.starts) == 1 and waits == []


def test_429_waits_for_retry_after_then_retries():
    client, clock, transport, waits = paced_client(
        [Resp(429, {"Retry-After": "7"}), Resp(429, {}), Resp(200, body={"id": 3})])
    assert client.release(3) == {"id": 3}
    assert len(transport.starts) == 3
    assert 7.0 in waits and 10.0 in waits  # header value, then the default when it is missing
    assert transport.starts[1] - transport.starts[0] >= 7.0
    assert transport.starts[2] - transport.starts[1] >= 10.0
    assert clock.slept == waits


def test_retry_after_as_http_date_and_garbage():
    now = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc).timestamp()
    later = format_datetime(datetime(2026, 10, 17, 12, 0, 30, tzinfo=timezone.utc), usegmt=True)
    assert retry_after_seconds(later, now=now) == pytest.approx(30.0)
    assert retry_after_seconds("2.5") == 2.5
    assert retry_after_seconds("soon") == discogs_mod.RETRY_AFTER_DEFAULT
    assert retry_after_seconds(None) == discogs_mod.RETRY_AFTER_DEFAULT
    assert retry_after_seconds("0") == 1.0  # never hammer
    assert retry_after_seconds("86400") == discogs_mod.RETRY_AFTER_MAX


def test_429_every_time_gives_up_with_retry_after():
    client, _clock, transport, _waits = paced_client([Resp(429, {"Retry-After": "4"}) for _ in range(5)])
    with pytest.raises(DiscogsError) as err:
        client.release(9)
    assert len(transport.starts) == 5 and err.value.retry_after == 4.0 and "429" in str(err.value)


def test_429_retry_after_reaches_provider_error():
    from music_manager.service import ProviderError
    from music_manager.service._releases import provider_call

    client, *_ = paced_client([Resp(429, {"Retry-After": "12"}) for _ in range(5)])
    with pytest.raises(ProviderError) as err:
        provider_call(client.release, 11)
    assert err.value.retry_after == 12.0


def test_low_remaining_quota_backs_off():
    client, _clock, transport, waits = paced_client([Resp(200, {"X-Discogs-Ratelimit-Remaining": "1"})])
    client.release(1)
    client.release(2)
    assert 8 in waits and transport.starts[1] - transport.starts[0] >= 8


def test_requests_from_two_threads_are_serialised():
    clock = FakeClock()
    lock = threading.Lock()
    client = Discogs("token", None, None)
    client.sleeper = clock.sleep
    client.clock = clock
    active = []
    overlap = []

    class Session:
        starts: list[float] = []

        def get(self, url, params=None, timeout=None):
            active.append(url)
            if len(active) > 1:
                overlap.append(tuple(active))
            with lock:
                self.starts.append(clock.now)
                clock.now += 0.01
            active.remove(url)
            return Resp(body={"u": url})

    client.session = Session()
    threads = [threading.Thread(target=lambda i=i: [client.release(i * 100 + k) for k in range(10)])
               for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    starts = sorted(Session.starts)
    assert overlap == [] and len(starts) == 30
    assert min(b - a for a, b in zip(starts, starts[1:], strict=False)) >= discogs_mod.MIN_INTERVAL
