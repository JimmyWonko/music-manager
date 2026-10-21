"""Discogs provider: API client (rate limit, 6-hour response cache), token storage, and the
release/tracklist helpers that turn Discogs JSON into numbered tracks.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, TypedDict, cast

import requests

from .. import __version__
from ..naming import ARTIST_NUM_SUFFIX, POS_VINYL, side_label, year4

# --- data shapes (review #13) ---------------------------------------------------
# Plain dicts at runtime; these only describe the keys the code reads and writes.
# Discogs JSON is described loosely (total=False): the API omits keys freely.


class DiscogsArtist(TypedDict, total=False):
    id: int
    name: str
    anv: str
    join: str


class DiscogsLabel(TypedDict, total=False):
    id: int
    name: str
    catno: str


class DiscogsFormat(TypedDict, total=False):
    name: str
    qty: str
    descriptions: list[str]


class DiscogsImage(TypedDict, total=False):
    type: str  # "primary" or "secondary"
    uri: str
    uri150: str


class DiscogsTrackJSON(TypedDict, total=False):
    position: str
    type_: str
    title: str
    duration: str
    artists: list[DiscogsArtist]
    sub_tracks: list["DiscogsTrackJSON"]


class DiscogsRelease(TypedDict, total=False):
    """A /releases/{id} response (also used for /masters/{id})."""
    id: int
    title: str
    year: int | str
    released: str
    master_id: int
    artists: list[DiscogsArtist]
    artists_sort: str
    labels: list[DiscogsLabel]
    formats: list[DiscogsFormat]
    genres: list[str]
    styles: list[str]
    tracklist: list[DiscogsTrackJSON]
    country: str
    images: list[DiscogsImage]


class SearchResult(TypedDict, total=False):
    """One item of a /database/search response."""
    id: int
    title: str
    year: str
    catno: str
    label: list[str]
    format: list[str]
    country: str
    master_id: int


# None: the defaults below, resolved when needed (never at import). service.configure sets them.
TOKEN_PATH: Path | None = None  # default ~/.config/discogs-tag/token
CACHE_DIR: Path | None = None  # default ~/.cache/discogs-tag


def token_path() -> Path:
    return Path(TOKEN_PATH) if TOKEN_PATH is not None else Path.home() / ".config" / "discogs-tag" / "token"


def cache_dir() -> Path:
    return Path(CACHE_DIR) if CACHE_DIR is not None else Path.home() / ".cache" / "discogs-tag"
CACHE_MAX_AGE = 6 * 60 * 60  # Discogs API terms cap cached data at 6 hours
USER_AGENT = (
    f"music-manager/{__version__} +https://github.com/JimmyWonko/music-manager"
)
API = "https://api.discogs.com"
POS_CD = re.compile(r"^(?:(?:cd|dvd|disc|disk)\s*)?(\d+)\s*[-–.]\s*(\d+)[a-z]?$", re.I)
POS_NUM = re.compile(r"^(\d+)([a-z]|\.\d+)?$", re.I)
RELEASE_URL = re.compile(r"discogs\.com/(?:.+/)?release/(\d+)", re.I)


# ---------------------------------------------------------------------------
# Discogs API
# ---------------------------------------------------------------------------

def _write_cache_atomic(cache: Path, text: str) -> None:
    """Write a cache file so readers only ever see the old or the complete new file."""
    fd, tmp_name = tempfile.mkstemp(dir=cache.parent, prefix=f".{cache.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, cache)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class DiscogsError(RuntimeError):
    """A Discogs request failed. retry_after: seconds Discogs asked us to wait (429), when known."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


# Discogs allows 60 authenticated requests a minute (25 unauthenticated), counted over a
# moving 60-second window. Requests are serial (one lock per client) and each starts at
# least MIN_INTERVAL after the previous one *finished*, so any 60 s holds at most
# ceil(60 / 1.1) = 55 requests.
MIN_INTERVAL = 1.1
RETRY_AFTER_DEFAULT = 10.0
RETRY_AFTER_MAX = 300.0


def retry_after_seconds(value: str | None, now: float | None = None) -> float:
    """Seconds to wait for a Retry-After header: delta-seconds or an HTTP date; 10 s when absent or unreadable."""
    if value is None or not str(value).strip():
        return RETRY_AFTER_DEFAULT
    text = str(value).strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return RETRY_AFTER_DEFAULT
        if when is None:
            return RETRY_AFTER_DEFAULT
        seconds = when.timestamp() - (time.time() if now is None else now)
    if seconds != seconds:  # NaN
        return RETRY_AFTER_DEFAULT
    return min(max(seconds, 1.0), RETRY_AFTER_MAX)


class Discogs:
    def __init__(self, token: str | None, key: str | None, secret: str | None):
        if not token and not (key and secret):
            raise DiscogsError(
                "No Discogs credentials. Generate a token at\n"
                "  https://www.discogs.com/settings/developers\n"
                "then run:  discogs-tag --save-token"
            )
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/vnd.discogs.v2.discogs+json",
            }
        )
        if token:
            self.session.headers["Authorization"] = f"Discogs token={token}"
        else:
            self.session.headers["Authorization"] = f"Discogs key={key}, secret={secret}"
        self._last: float | None = None
        self.min_interval = MIN_INTERVAL
        # Called with the number of seconds before every rate-limit pause (the UI shows it).
        self.on_wait: Callable[[float], None] | None = None
        # Stand-ins for tests (a fake clock); None means time.monotonic / time.sleep.
        self.clock: Callable[[], float] | None = None
        self.sleeper: Callable[[float], None] | None = None
        # False: read fresh cache files but never create, replace or delete one (read-only checks).
        self.cache_write = True
        self._lock = threading.RLock()

    def _now(self) -> float:
        clock = getattr(self, "clock", None)
        return clock() if clock is not None else time.monotonic()

    def _sleep(self, seconds: float) -> None:
        if getattr(self, "on_wait", None) is not None:
            self.on_wait(seconds)
        sleeper = getattr(self, "sleeper", None)
        (sleeper or time.sleep)(seconds)

    def _wait(self) -> None:
        last = getattr(self, "_last", None)
        if last is None:
            return
        delay = self.min_interval - (self._now() - last)
        if delay > 0:
            self._sleep(delay)

    def _request(self, url: str, params: dict | None = None):
        """One paced GET (call with self._lock held)."""
        self._wait()
        try:
            return self.session.get(url, params=params, timeout=30)
        finally:
            self._last = self._now()

    def _cache_path(self, kind: str, key: str) -> Path:
        digest = hashlib.sha1(key.encode()).hexdigest()
        path = cache_dir() / kind / f"{digest}.json"
        if getattr(self, "cache_write", True):
            path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _get(self, url: str, params: dict | None, cache_kind: str, cache_key: str) -> dict:
        cache = self._cache_path(cache_kind, cache_key)
        # Discogs' API terms: don't show data more than 6 hours stale.
        if cache.exists() and time.time() - cache.stat().st_mtime < CACHE_MAX_AGE:
            try:
                return json.loads(cache.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, ValueError):
                # Truncated or corrupt (e.g. an old non-atomic write): drop it and refetch.
                if getattr(self, "cache_write", True):
                    try:
                        cache.unlink()
                    except OSError:
                        pass

        with self._lock:
            retry_after: float | None = None
            for attempt in range(5):
                response = self._request(url, params)
                remaining = response.headers.get("X-Discogs-Ratelimit-Remaining")
                if remaining is not None and remaining.isdigit() and int(remaining) <= 2:
                    self._sleep(8)
                if response.status_code == 429:
                    retry_after = retry_after_seconds(response.headers.get("Retry-After"))
                    if attempt < 4:
                        self._sleep(retry_after)
                    continue
                if response.status_code >= 500:
                    if attempt < 4:
                        self._sleep(2 * (attempt + 1))
                    continue
                if response.status_code >= 400:
                    raise DiscogsError(f"Discogs HTTP {response.status_code}: {response.text[:300]}")
                data = response.json()
                if getattr(self, "cache_write", True):
                    _write_cache_atomic(cache, json.dumps(data))
                return data
            if retry_after is not None:
                raise DiscogsError(f"Discogs HTTP 429: rate limited after retries: {url}", retry_after=retry_after)
            raise DiscogsError(f"Discogs request failed after retries: {url}")

    def search_releases(self, artist: str, album: str, catno: str = "", year: str = "") -> list[SearchResult]:
        """Search releases. year only re-orders results; it never filters them."""
        params: dict[str, str] = {"type": "release", "per_page": "15"}
        if artist:
            params["artist"] = artist
        if album:
            params["release_title"] = album
        if catno:
            params["catno"] = catno
        query = json.dumps(params, sort_keys=True)
        data = self._get(f"{API}/database/search", params, "search", query)
        results = list(data.get("results") or [])
        if results or not (artist or album):
            return rank_by_year(results, year)
        fallback = {"type": "release", "per_page": "15", "q": " ".join(p for p in (artist, album) if p)}
        data = self._get(f"{API}/database/search", fallback, "search", json.dumps(fallback, sort_keys=True))
        return rank_by_year(list(data.get("results") or []), year)

    def search_barcode(self, barcode: str) -> list[SearchResult]:
        """Releases with this UPC/EAN barcode (digits). Cached like every search."""
        params = {"type": "release", "per_page": "15", "barcode": barcode}
        data = self._get(f"{API}/database/search", params, "search", json.dumps(params, sort_keys=True))
        return list(data.get("results") or [])

    def search_text(self, query: str, year: str = "") -> list[SearchResult]:
        """Free-text release search (Discogs "q"); year only re-orders results."""
        params = {"type": "release", "per_page": "15", "q": query}
        data = self._get(f"{API}/database/search", params, "search", json.dumps(params, sort_keys=True))
        return rank_by_year(list(data.get("results") or []), year)

    def release(self, release_id: int) -> DiscogsRelease:
        return cast(DiscogsRelease, self._get(f"{API}/releases/{release_id}", None, "release", str(release_id)))

    def master(self, master_id: int) -> DiscogsRelease:
        return cast(DiscogsRelease, self._get(f"{API}/masters/{master_id}", None, "master", str(master_id)))

    def image(self, url: str) -> bytes:
        """Download one release image (cover art). Not cached."""
        with self._lock:
            response = self._request(url)
        if response.status_code >= 400:
            raise DiscogsError(f"Discogs HTTP {response.status_code} for image {url}")
        return response.content


def rank_by_year(results: list[SearchResult], year: str) -> list[SearchResult]:
    """Stable sort: exact year first, then nearest years, then unknown years.

    Nothing is dropped, so a wrong or missing year in the folder name can
    never hide the right release. score_result() also rewards a year match.
    """
    wanted = year4(year)
    if not wanted:
        return results

    def distance(item: SearchResult) -> int:
        got = year4(item.get("year"))
        return abs(int(got) - int(wanted)) if got else 10_000

    return sorted(results, key=distance)


def load_token() -> tuple[str | None, str | None, str | None]:
    env_token = os.environ.get("DISCOGS_TOKEN", "").strip() or None
    key = os.environ.get("DISCOGS_KEY", "").strip() or None
    secret = os.environ.get("DISCOGS_SECRET", "").strip() or None
    if env_token:
        return env_token, key, secret
    path = token_path()
    if path.exists():
        token = path.read_text(encoding="utf-8").strip() or None
        return token, key, secret
    return None, key, secret


def save_token(token: str, path: Path | None = None) -> None:
    """Write the token so it is never readable by other users, not even briefly.

    The file is created under a temporary name with O_CREAT|O_EXCL and mode
    0600, written, fsynced, then renamed over the old token file.
    """
    path = path or token_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)  # exactly 0600 whatever the umask is
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token.strip() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def split_result_title(title: str) -> tuple[str, str]:
    if " - " in (title or ""):
        artist, album = title.split(" - ", 1)
        return artist.strip(), album.strip()
    return "", title or ""


def parse_position(position: str) -> tuple[int, int]:
    """(disc, track number) from a Discogs position.

    Track number 0 means "no usable number": vinyl/cassette sides (A1, B2),
    letter-only positions, or anything unrecognised. Those tracks are
    numbered by their order in the tracklist (see flatten_tracklist).
    """
    pos = (position or "").strip()
    if not pos:
        return (1, 0)
    match = POS_CD.match(pos)
    if match:
        return int(match.group(1)), int(match.group(2))
    match = POS_NUM.match(pos)
    if match:
        return 1, int(match.group(1))
    if is_side_position(pos):
        return 1, 0
    digits = re.findall(r"\d+", pos)
    if len(digits) >= 2:
        return int(digits[0]), int(digits[1])
    return (1, 0)


def is_side_position(position: str) -> bool:
    """A1, B12, AA1, C, A3a ... (vinyl and cassette sides)."""
    return bool(POS_VINYL.match((position or "").strip()))


def legacy_vinyl_number(position: str) -> int | None:
    """The number older versions of this script gave a side position.

    They used (side - 1) * 20 + n, so B1 became 21. Only used to recognise
    files that were already renamed that way.
    """
    match = re.fullmatch(r"([A-Z]+)\s*(\d+)", (position or "").strip(), re.I)
    if not match:
        return None
    side = 0
    for ch in match.group(1).upper():
        side = side * 26 + (ord(ch) - 64)
    return (side - 1) * 20 + int(match.group(2))


@dataclass
class DiscogsTrack:
    disc: int
    number: int
    title: str
    artist: str
    position: str = ""  # Discogs position as printed on the release (A1, B2, 1-03, ...)
    legacy_number: int | None = None  # number the old (side-1)*20+n scheme gave
    side: str | None = None  # normalised side position (A1) when its whole disc uses sides
    duration_s: float | None = None  # Discogs duration ("3:45"), None when not listed
    subtracks: tuple[str, ...] = ()  # merged subtrack positions ("2-9a", "2-9b"); see flatten_tracklist(merge=)


def parse_duration(text: str | None) -> float | None:
    """Discogs duration ("3:45", "1:02:03") in seconds, or None when empty or unreadable."""
    parts = (text or "").strip().split(":")
    if not parts or not all(p.strip().isdigit() for p in parts) or len(parts) > 3:
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return float(seconds)


def _track_artist(item: DiscogsTrackJSON, fallback: str) -> str:
    artists = item.get("artists") or []
    if not artists:
        return fallback
    return " & ".join(
        ARTIST_NUM_SUFFIX.sub("", a.get("anv") or a.get("name") or "").strip()
        for a in artists
    )


SUBPOS = re.compile(r"^((?:\d+-)?\d+)(?:([a-z])|\.(\d+))$", re.IGNORECASE)  # 9a / 2-9a / 9.1 / 2-9.1


@dataclass(frozen=True)
class SubtrackGroup:
    """Consecutive release tracks sharing one base position (2-9a, 2-9b; 9.1, 9.2; an index track's
    sub_tracks): often ripped as ONE file. See match.collapse_subtracks."""
    disc: int
    base: str  # "2-9"
    positions: tuple[str, ...]  # ("2-9a", "2-9b")
    titles: tuple[str, ...]
    durations: tuple[float | None, ...]


def subtrack_groups(release: DiscogsRelease) -> list[SubtrackGroup]:
    """Every run of 2+ consecutive playable tracks sharing a numeric base position, in tracklist order.

    Letter suffixes (9a, 2-9b) always count; dotted ones (9.1) only when the release also has plain
    numeric positions (else "1.1" may be disc.track). Vinyl side positions (A1a) never count. An index
    track's sub_tracks form a group when their positions share one base.
    """
    entries = _tracklist_entries(release, "")
    plain = any(re.fullmatch(r"(?:\d+-)?\d+", e[0]) for e in entries)
    groups: list[SubtrackGroup] = []
    run: list[tuple[str, str, str, str]] = []
    run_base = ""

    def close():
        if len(run) > 1:
            groups.append(SubtrackGroup(max(parse_position(run_base)[0], 1), run_base, tuple(e[0] for e in run),
                                        tuple(e[1] for e in run), tuple(parse_duration(e[3]) for e in run)))

    for entry in entries:
        m = SUBPOS.match(entry[0])
        base = m.group(1) if m and (m.group(2) or plain) else ""
        if base and base == run_base:
            run.append(entry)
            continue
        close()
        run, run_base = ([entry], base) if base else ([], "")
    close()
    return groups


def _merge_entries(entries: list[tuple[str, str, str, str]], merge) -> tuple[list, dict[int, tuple[str, ...]]]:
    """Replace each run of entries whose positions are one of merge by one "A / B" entry at the base."""
    wanted = {tuple(p.casefold() for p in g): tuple(g) for g in merge or () if len(g) > 1}
    out: list[tuple[str, str, str, str]] = []
    subs: dict[int, tuple[str, ...]] = {}
    i = 0
    while i < len(entries):
        hit = None
        for key, group in wanted.items():
            span = entries[i:i + len(key)]
            if tuple(e[0].casefold() for e in span) == key:
                hit = (group, span)
                break
        if hit is None:
            out.append(entries[i])
            i += 1
            continue
        group, span = hit
        m = SUBPOS.match(span[0][0])
        base = m.group(1) if m else span[0][0]
        titles = [e[1] for e in span]
        prefix = titles[0].split(": ", 1)[0] + ": " if ": " in titles[0] else ""
        if prefix and all(t.startswith(prefix) for t in titles):
            title = prefix + " / ".join(t[len(prefix):] for t in titles)
        else:
            title = " / ".join(titles)
        secs = [parse_duration(e[3]) for e in span]
        total = sum(secs) if all(x is not None for x in secs) else None  # type: ignore[misc]
        duration = f"{int(total) // 60}:{int(total) % 60:02d}" if total is not None else ""
        subs[len(out)] = group
        out.append((base, title, span[0][2], duration))
        i += len(span)
    return out, subs


def _tracklist_entries(release: DiscogsRelease, album_artist: str) -> list[tuple[str, str, str, str]]:
    """(position, title, artist, duration) for every playable track, in tracklist order.

    Headings are dropped. Index tracks (a work split into movements) are
    replaced by their sub-tracks, titled "Work: Movement" so the work name
    is not lost. A normal track that lists sub_tracks stays one track.
    """
    entries: list[tuple[str, str, str, str]] = []
    for item in release.get("tracklist") or []:
        kind = (item.get("type_") or "track").lower()
        if kind == "heading":
            continue
        if kind == "index":
            parent_title = (item.get("title") or "").strip()
            parent_artist = _track_artist(item, album_artist)
            for sub in item.get("sub_tracks") or []:
                if (sub.get("type_") or "track").lower() != "track":
                    continue
                sub_title = (sub.get("title") or "").strip()
                if not sub_title:
                    continue
                title = f"{parent_title}: {sub_title}" if parent_title else sub_title
                entries.append(((sub.get("position") or "").strip(), title, _track_artist(sub, parent_artist),
                                sub.get("duration") or ""))
            continue
        title = (item.get("title") or "").strip()
        if not title:
            continue
        entries.append(((item.get("position") or "").strip(), title, _track_artist(item, album_artist),
                        item.get("duration") or ""))
    return entries


def flatten_tracklist(release: DiscogsRelease, album_artist: str, merge=()) -> list[DiscogsTrack]:
    """Discogs tracklist -> numbered tracks.

    Each disc keeps Discogs' own numbers (1, 2, ... or 1-1, 1-2, ...) when
    every track on it has a distinct number. Otherwise, and always for
    vinyl/cassette sides, the disc's tracks are numbered 1..N in tracklist
    order, so A1 A2 B1 B2 become 1 2 3 4 in the track-number tag.

    When every track on a disc has a distinct side position, each track also
    gets `side` (A1, A2, B1 ...), which file names use by default.

    merge: subtrack groups (position tuples, see subtrack_groups) to make one track each: position
    the base ("2-9"), title "A / B" ("Work: A / B" for an index track's movements), duration the sum
    when every part has one, subtracks the merged positions. Later tracks keep their numbers.
    """
    parsed = []
    durations = []
    entries, merged = _merge_entries(_tracklist_entries(release, album_artist), merge)
    for position, title, artist, duration in entries:
        disc, number = parse_position(position)
        parsed.append((max(disc, 1), number, position, title, artist))
        durations.append(parse_duration(duration))

    by_disc: dict[int, list[int]] = {}
    for i, (disc, *_rest) in enumerate(parsed):
        by_disc.setdefault(disc, []).append(i)

    numbers: dict[int, int] = {}
    sides_of: dict[int, str] = {}
    for indexes in by_disc.values():
        explicit = [parsed[i][1] for i in indexes]
        sides = any(is_side_position(parsed[i][2]) for i in indexes)
        labels = [side_label(parsed[i][2]) if is_side_position(parsed[i][2]) else None for i in indexes]
        if all(labels) and len({lab.casefold() for lab in labels}) == len(labels):
            for i, lab in zip(indexes, labels):
                sides_of[i] = lab
        if not sides and all(n > 0 for n in explicit) and len(set(explicit)) == len(explicit):
            for i in indexes:
                numbers[i] = parsed[i][1]
        else:
            for seq, i in enumerate(indexes, 1):
                numbers[i] = seq

    out: list[DiscogsTrack] = []
    for i, (disc, _number, position, title, artist) in enumerate(parsed):
        out.append(
            DiscogsTrack(
                disc=disc,
                number=numbers[i],
                title=title,
                artist=artist,
                position=position,
                legacy_number=legacy_vinyl_number(position),
                side=sides_of.get(i),
                duration_s=durations[i],
                subtracks=merged.get(i, ()),
            )
        )
    return out


def release_artists(release: DiscogsRelease) -> str:
    names = []
    for artist in release.get("artists") or []:
        name = ARTIST_NUM_SUFFIX.sub("", artist.get("name") or "").strip()
        if name:
            names.append(name)
    joined = " & ".join(names) if names else (release.get("artists_sort") or "")
    return ARTIST_NUM_SUFFIX.sub("", joined).strip()


def release_catno(release: DiscogsRelease, preferred: str = "") -> str:
    cats = []
    for label in release.get("labels") or []:
        cat = (label.get("catno") or "").strip()
        if cat and cat != "none":
            cats.append(cat)
    if preferred:
        pref = preferred.replace(" ", "").upper()
        for cat in cats:
            if cat.replace(" ", "").upper() == pref:
                return cat
    return "; ".join(cats)


def release_discs(release: DiscogsRelease, tracks: list[DiscogsTrack]) -> int:
    from_tracks = max((t.disc for t in tracks), default=1)
    qty = 1
    for fmt in release.get("formats") or []:
        try:
            qty = max(qty, int(fmt.get("qty") or 1))
        except ValueError:
            pass
    return max(from_tracks, qty, 1)
