#!/usr/bin/env python3
"""Tag and rename album folders using Discogs, with this library's Picard rules.

Get a token (not an OAuth app):
  https://www.discogs.com/settings/developers  →  Generate token

  discogs-tag --save-token
  discogs-tag --dry-run "/path/to/album"
  discogs-tag "/path/to/Featuring"
"""

from __future__ import annotations

import argparse
import base64
import errno
import hashlib
import json
import os
import pickle
import re
import shutil
import stat
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

import requests
from mutagen import File as MutagenFile
from mutagen import MutagenError
from mutagen.aiff import AIFF
from mutagen.apev2 import APEv2File
from mutagen.asf import ASF
from mutagen.easyid3 import EasyID3
from mutagen.flac import FLAC
from mutagen.id3 import (
    ID3,
    ID3NoHeaderError,
    TALB,
    TCON,
    TDRC,
    TDOR,
    TIT2,
    TPE1,
    TPE2,
    TPOS,
    TPUB,
    TRCK,
    TXXX,
)
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4FreeForm
from mutagen.oggflac import OggFLAC
from mutagen.oggopus import OggOpus
from mutagen.oggspeex import OggSpeex
from mutagen.oggvorbis import OggVorbis
from mutagen.wave import WAVE

# The script is usually run through ~/.local/bin/discogs-tag, a symlink.
# Resolve it so the shared helper next to the real file can be imported.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mm_common  # noqa: E402

# There is deliberately no built-in library path: a missing --dest must never
# send albums into whatever library happens to be mounted. $DEFAULT_MUSIC is
# used only when the user has set it.
_env_music = os.getenv("DEFAULT_MUSIC", "").strip()
DEFAULT_MUSIC: Path | None = Path(_env_music).expanduser() if _env_music else None

TOKEN_PATH = Path.home() / ".config" / "discogs-tag" / "token"
CACHE_DIR = Path.home() / ".cache" / "discogs-tag"
CACHE_MAX_AGE = 6 * 60 * 60  # Discogs API terms cap cached data at 6 hours
USER_AGENT = (
    f"music-manager/{mm_common.__version__} +https://github.com/JimmyWonko/music-manager"
)
API = "https://api.discogs.com"

AUDIO_EXTS = {
    ".flac",
    ".mp3",
    ".m4a",
    ".aac",
    ".ogg",
    ".opus",
    ".wav",
    ".wma",
    ".ape",
    ".wv",
    ".aiff",
    ".aif",
    ".alac",
}

SIDECAR_EXTS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".tiff",
    ".tif",
    ".webp",
    ".log",
    ".cue",
    ".txt",
    ".nfo",
    ".accurip",
    ".md5",
    ".sfv",
    ".m3u",
    ".m3u8",
}

JUNK_DIR_NAMES = {".git", ".trash", ".trashes", "@eadir"}
# Only treated as artwork/extras when they sit inside an album folder (see
# is_artwork_dir). A real artist or album called "Art" or "Covers" is kept.
ARTWORK_DIR_NAMES = {"scans", "covers", "artwork", "booklet", "pics", "art"}

SWAP_PREFIXES = ("The", "Die", "Le")

ARTIST_REPLACEMENTS = (
    ('"', ""),
    (" meets ", " & "),
    (" X ", " & "),
    (" × ", " & "),
    ("; ", " & "),
    (" x ", " & "),
    (" | ", " & "),
    (" • ", " & "),
    (" ⋅ ", " & "),
    (" and ", " & "),
    ("+", "&"),
    (" with ", " & "),
    (" _ ", " & "),
    (" vs ", " & "),
    (" vs. ", " & "),
)

CATNO_STRIP = re.compile(r"""[_⋅/: *?"<>| ]""")
ARTIST_NUM_SUFFIX = re.compile(r"\s*\(\d+\)$")
DISC_DIR = mm_common.DISC_DIR
YEAR_RE = re.compile(r"(?:19|20)\d{2}")
JUNK_BITS = re.compile(
    r"""(?ix)
    (?:
        \[(?:web|cd|vinyl|flac|mp3|v0|v2|320|16-44\.1|24-96|24-192)[^\]]*\]
        | \{[^}]*\}
        | \((?:web|cd|flac|mp3|v0)[^)]*\)
        | \b(?:web|flac|mp3|v0|eac|log|cue)\b
    )
    """
)
NAMED_ALBUM_DIR = re.compile(
    r"^\[(?P<y1>\d{4}|)\]\s+(?P<title>.+?)(?:\s+\[(?P<y2>\d{4})\])?(?:\s+\[(?P<cat>[^\]]*)\])?\s*$"
)
TRACK_PREFIX = re.compile(r"^(\d{1,3})\s*[\.\-_]\s*")
# "06 Title": a leading number separated only by spaces (used by match_tracks
# after TRACK_PREFIX; matched only against track numbers the release has)
SPACED_TRACK_PREFIX = re.compile(r"^(\d{1,3})\s+(?=\S)")
# "2-05 Title" / "2.05 Title": disc 2, track 5 (only trusted on multi-disc releases)
DISC_TRACK_PREFIX = re.compile(r"^(\d{1,2})[-.](\d{1,3})(?=[\s._-])")
# "B2. Title" / "A1 - Title": vinyl/cassette side + track (only trusted when
# the release really has that position)
SIDE_PREFIX = re.compile(r"^([A-Za-z]{1,2}\d{1,2})(?=[\s._-])")
POS_CD = re.compile(r"^(?:(?:cd|dvd|disc|disk)\s*)?(\d+)\s*[-–.]\s*(\d+)[a-z]?$", re.I)
POS_NUM = re.compile(r"^(\d+)([a-z]|\.\d+)?$", re.I)
POS_VINYL = re.compile(r"^([A-Z]{1,2})\s*(\d*)([a-z]|\.\d+)?$", re.I)
RELEASE_URL = re.compile(r"discogs\.com/(?:.+/)?release/(\d+)", re.I)


def natural_key(name: str) -> list:
    return [int(p) if p.isdigit() else p.casefold() for p in re.split(r"(\d+)", name)]


def is_junk_dir(path: Path) -> bool:
    return path.name.startswith(".") or path.name.lower() in JUNK_DIR_NAMES


def is_artwork_dir(path: Path) -> bool:
    """Scans/Covers/Art/... only count as artwork inside an album folder.

    The parent must be an album folder (it holds audio, or CD1/Disc 2/...
    subfolders with audio) or a disc folder. A top-level artist or album
    folder that happens to be called "Art" or "Covers" is a real folder.
    """
    if path.name.lower() not in ARTWORK_DIR_NAMES:
        return False
    parent = path.parent
    if audio_in(parent):
        return True
    return bool(mm_common.disc_dirs_with_audio(parent))


def should_skip_dir(path: Path) -> bool:
    return is_junk_dir(path) or is_artwork_dir(path) or is_source_checkout(path)


def is_source_checkout(path: Path) -> bool:
    """A git checkout (e.g. a copy of this tool next to the music) holds
    test samples, not albums; it is never scanned or moved."""
    return os.path.lexists(path / ".git")


def stays_behind(path: Path) -> bool:
    """Hidden folders that never travel with an album (trash, git data).

    Every other hidden or system folder inside an album (.AppleDouble,
    @eaDir, …) moves with it, so the old folder can be emptied and removed.
    """
    name = path.name.lower()
    return name in {".git", ".trash", ".trashes"} or name.startswith(".trash-")


def is_audio(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in AUDIO_EXTS


def audio_in(folder: Path) -> list[Path]:
    tracks: list[Path] = []
    try:
        for entry in folder.iterdir():
            if is_audio(entry):
                tracks.append(entry)
    except OSError:
        return []
    tracks.sort(key=lambda p: natural_key(p.name))
    return tracks


# ---------------------------------------------------------------------------
# Naming (port of the restored Picard script)
#
# Every naming decision (folder and file names, when the original year is
# shown) lives in this section, so it can later be swapped for a
# user-configurable template without touching the tagging or move code.
# ---------------------------------------------------------------------------

def swapprefix(name: str, prefixes: tuple[str, ...] = SWAP_PREFIXES) -> str:
    stripped = name.strip()
    for prefix in prefixes:
        token = prefix + " "
        if stripped.startswith(token) or stripped.casefold().startswith(token.casefold()):
            rest = stripped[len(prefix) :].lstrip()
            if rest:
                return f"{rest}, {prefix}"
    return stripped


def format_artist(name: str) -> str:
    name = ARTIST_NUM_SUFFIX.sub("", name or "")
    if name.strip().casefold() == "various":
        name = "Various Artists"
    name = swapprefix(name)
    for old, new in ARTIST_REPLACEMENTS:
        name = name.replace(old, new)
    return name.strip()


def format_album_title(title: str) -> str:
    title = (title or "").replace('"', "").replace(":", "-")
    return title[:100].strip()


def format_catalog(catno: str | None) -> str | None:
    if not catno:
        return None
    first = re.split(r";\s*", catno.strip())[0].strip()
    if first.casefold() in {"", "none", "[none]"}:
        return None
    cleaned = CATNO_STRIP.sub("", first).upper()
    return cleaned or None


def sanitize_filename(name: str) -> str:
    name = name.replace("/", "_").replace("\\", "_")
    for ch in ':*?"<>|':
        name = name.replace(ch, "_")
    return name.strip(" .") or "unknown"


def year4(value: str | int | None) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text[:4] if len(text) >= 4 and text[:4].isdigit() else ""


def reissue_original_year(master_year: str | int | None, release_year: str | int | None) -> str:
    """The original year for the ORIGINALDATE tags, or "" when this release is the original.

    A release is a reissue when its master's year is earlier than its own
    year. Same year, no master, or an unknown year on either side: not a
    reissue, so no original-date tag is written. (The folder name always
    has a leading year; see folder_lead_year.)
    """
    master, release = year4(master_year), year4(release_year)
    if master and release and int(master) < int(release):
        return master
    return ""


def folder_lead_year(master_year: str | int | None, release_year: str | int | None) -> str:
    """Leading year of the album folder, so an artist's albums sort chronologically.

    The master's year when this is a reissue, otherwise the release's own
    year (or the master's year when the release year is unknown).
    """
    return reissue_original_year(master_year, release_year) or year4(release_year) or year4(master_year)


def album_folder_name(album: str, original_year: str, release_year: str, catno: str | None, discs: int) -> str:
    """[LEADYEAR]  Album [2CD] [RELEASEYEAR] [CATNO]

    original_year is the master's year.
    - LEADYEAR: the master's year for a reissue, else the release year
      (see folder_lead_year), so each artist's albums sort chronologically.
    - RELEASEYEAR: only for a reissue (master year earlier than release year).
    - Unknown parts are left out with their brackets and spaces.

    Reissue:      [1979]  Nervous Breakdown [1988] [SSTCD001]
    Not reissue:  [1984]  My War [SST023]
    No year:      My War [SST023]
    """
    lead = folder_lead_year(original_year, release_year)
    title = format_album_title(album)
    parts = [f"[{lead}]  {title}" if lead else title]
    if discs > 1:
        parts.append(f"{discs}CD")
    if reissue_original_year(original_year, release_year):
        parts.append(f"[{year4(release_year)}]")
    catalog = format_catalog(catno)
    if catalog:
        parts.append(f"[{catalog}]")
    return sanitize_filename(" ".join(parts))


def track_filename(number: int | str, title: str, suffix: str) -> str:
    """"N. Title.ext", or "A1. Title.ext" when given a vinyl/cassette side position."""
    return sanitize_filename(f"{number}. {title}") + suffix.lower()


# Vinyl and cassette tracks are named after their side position (A1, A2, B1 ...)
# by default, as collectors expect. --sequential-sides switches back to 1..N.
SIDE_NAMES = True


def side_label(position: str) -> str | None:
    """Normalised side position for a file name ("a1" -> "A1", "B 2" -> "B2"), or None."""
    pos = (position or "").strip()
    match = POS_VINYL.match(pos)
    if not match:
        return None
    return match.group(1).upper() + match.group(2) + (match.group(3) or "")


def track_prefix(dt: "DiscogsTrack", side_names: bool | None = None) -> str:
    """What goes before ". Title" in a track's file name."""
    use_sides = SIDE_NAMES if side_names is None else side_names
    if use_sides and dt.side:
        return dt.side
    return str(dt.number)


# ---------------------------------------------------------------------------
# Guess artist/album from folder + tags
# ---------------------------------------------------------------------------

def _read_tag_file(path: Path, easy: bool = False):
    """Return (audio, tags). audio is None when only an ID3 tag was read.

    mutagen's MP3 opener also scans for an MPEG frame and raises
    HeaderNotFoundError when the file is tag-only or corrupt. ID3 and
    EasyID3 read the tag without that scan.
    """
    try:
        if path.suffix.lower() == ".mp3":
            return None, (EasyID3(path) if easy else ID3(path))
        audio = MutagenFile(path, easy=easy)
    except ID3NoHeaderError:
        return None, None
    except MutagenError as exc:
        print(f"  warning: cannot read tags: {path}: {exc}")
        return None, None
    if audio is None or not audio.tags:
        return audio, None
    return audio, audio.tags


def read_easy_tags(path: Path) -> dict[str, str]:
    _audio, tags = _read_tag_file(path, easy=True)
    if not tags:
        return {}
    out: dict[str, str] = {}
    for key in ("artist", "albumartist", "album", "title", "date", "originaldate", "tracknumber"):
        value = tags.get(key)
        if value:
            out[key] = str(value[0])
    return out


def strip_junk(text: str) -> str:
    text = JUNK_BITS.sub(" ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def parse_album_dirname(name: str) -> dict[str, str]:
    info: dict[str, str] = {}
    match = NAMED_ALBUM_DIR.match(name)
    if match:
        title = match.group("title") or ""
        title = re.sub(r"\s+\d+CD$", "", title).strip()
        info["album"] = title
        info["originalyear"] = match.group("y1") or ""
        info["year"] = match.group("y2") or match.group("y1") or ""
        cat = (match.group("cat") or "").strip()
        if cat and not YEAR_RE.fullmatch(cat):
            info["catno"] = cat
        elif cat and YEAR_RE.fullmatch(cat) and not match.group("y2"):
            info["year"] = cat
        return info

    cleaned = strip_junk(name)
    years = YEAR_RE.findall(cleaned)
    if years:
        info["year"] = years[0]
        cleaned = YEAR_RE.sub(" ", cleaned, count=1)
        cleaned = re.sub(r"\(\s*\)", "", cleaned)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" -_")

    if " - " in cleaned:
        left, right = cleaned.split(" - ", 1)
        # Artist - 2013 - Title  (year already stripped, may leave extra dash)
        right = right.strip(" -_")
        if len(left.split()) <= 6:
            info["artist"] = left.strip()
            info["album"] = right.strip()
            return info

    info["album"] = cleaned
    return info


def guess_from_album_dir(album_dir: Path, tracks: list[Path]) -> dict[str, str]:
    tags = read_easy_tags(tracks[0]) if tracks else {}
    parsed = parse_album_dirname(album_dir.name)
    parent = album_dir.parent.name

    artist = (
        tags.get("albumartist")
        or tags.get("artist")
        or parsed.get("artist")
        or (parent if not NAMED_ALBUM_DIR.match(parent) else "")
    )
    album = tags.get("album") or parsed.get("album") or album_dir.name
    guess = {
        "artist": ARTIST_NUM_SUFFIX.sub("", artist or "").strip(),
        "album": strip_junk(album),
        "year": year4(tags.get("date")) or parsed.get("year", ""),
        "originalyear": year4(tags.get("originaldate")) or parsed.get("originalyear", ""),
        "catno": parsed.get("catno", ""),
    }
    return guess


def default_dest(start: Path, library: Path | None = None) -> Path | None:
    """Destination when --dest is not given, or None when there isn't a safe one.

    The Featuring directory the path sits under, else $DEFAULT_MUSIC when the
    user set it (and it exists). Never a guessed or built-in library.
    """
    library = DEFAULT_MUSIC if library is None else library
    resolved = start.resolve()
    parts = list(resolved.parts)
    if "Featuring" in parts:
        return Path(*parts[: parts.index("Featuring") + 1])
    if library is not None and library.is_dir():
        return library.resolve()
    return None


# ---------------------------------------------------------------------------
# Walk albums
# ---------------------------------------------------------------------------

@dataclass
class AlbumFolder:
    path: Path
    tracks: list[Path]
    sidecars: list[Path] = field(default_factory=list)
    extra_dirs: list[Path] = field(default_factory=list)


def _dir_holds_track(folder: Path, tracks: list[Path]) -> bool:
    root = folder.resolve()
    for track in tracks:
        resolved = track.resolve()
        if resolved == root or root in resolved.parents:
            return True
    return False


def collect_extras(folder: Path, tracks: list[Path]) -> tuple[list[Path], list[Path]]:
    """Non-audio files and extra folders that should travel with the album.

    Top-level dirs that contain none of the album tracks (Scans, Covers,
    Artwork, Booklet, …, and hidden/system folders such as .AppleDouble)
    are moved as directories. Leftover files inside disc folders are kept
    with their relative path. Only trash and .git folders stay behind.
    """
    track_set = {path.resolve() for path in tracks}
    extra_files: list[Path] = []
    extra_dirs: list[Path] = []
    try:
        entries = list(folder.iterdir())
    except OSError:
        return [], []

    for entry in entries:
        if entry.is_dir() and stays_behind(entry):
            continue
        if entry.is_dir() and is_junk_dir(entry) and not entry.is_symlink():
            extra_dirs.append(entry)
            continue
        if entry.is_dir():
            if _dir_holds_track(entry, tracks):
                for dirpath, dirnames, filenames in os.walk(entry, followlinks=False):
                    dirnames[:] = [d for d in dirnames if not stays_behind(Path(dirpath) / d)]
                    for name in filenames:
                        path = Path(dirpath) / name
                        if path.resolve() in track_set or is_audio(path):
                            continue
                        extra_files.append(path)
            else:
                extra_dirs.append(entry)
            continue
        if entry.is_file() and entry.resolve() not in track_set:
            extra_files.append(entry)

    extra_files.sort(key=lambda p: natural_key(str(p.relative_to(folder))))
    extra_dirs.sort(key=lambda p: natural_key(p.name))
    return extra_files, extra_dirs


def collect_albums(root: Path) -> list[AlbumFolder]:
    albums: list[AlbumFolder] = []
    for dirpath, dirnames, _filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        dirnames[:] = [d for d in dirnames if not should_skip_dir(current / d)]
        if should_skip_dir(current) and current != root:
            continue

        here = audio_in(current)
        disc_dirs = [d for d in list(dirnames) if DISC_DIR.match(d) and audio_in(current / d)]

        if disc_dirs and not here:
            tracks: list[Path] = []
            for name in sorted(disc_dirs, key=natural_key):
                tracks.extend(audio_in(current / name))
            if tracks:
                files, dirs = collect_extras(current, tracks)
                albums.append(AlbumFolder(current, tracks, files, dirs))
            dirnames[:] = [d for d in dirnames if d not in disc_dirs]
            continue

        if here:
            files, dirs = collect_extras(current, here)
            albums.append(AlbumFolder(current, here, files, dirs))
            dirnames[:] = []
    albums.sort(key=lambda a: str(a.path).casefold())
    return albums


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
    pass


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
        self._last = 0.0
        self.min_interval = 1.1

    def _wait(self) -> None:
        delay = self.min_interval - (time.monotonic() - self._last)
        if delay > 0:
            time.sleep(delay)

    def _cache_path(self, kind: str, key: str) -> Path:
        digest = hashlib.sha1(key.encode()).hexdigest()
        path = CACHE_DIR / kind / f"{digest}.json"
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
                try:
                    cache.unlink()
                except OSError:
                    pass

        self._wait()
        for attempt in range(5):
            response = self.session.get(url, params=params, timeout=30)
            self._last = time.monotonic()
            remaining = response.headers.get("X-Discogs-Ratelimit-Remaining")
            if remaining is not None and remaining.isdigit() and int(remaining) <= 2:
                time.sleep(8)
            if response.status_code == 429:
                time.sleep(int(response.headers.get("Retry-After", "10")))
                continue
            if response.status_code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            if response.status_code >= 400:
                raise DiscogsError(f"Discogs HTTP {response.status_code}: {response.text[:300]}")
            data = response.json()
            _write_cache_atomic(cache, json.dumps(data))
            return data
        raise DiscogsError(f"Discogs request failed after retries: {url}")

    def search_releases(self, artist: str, album: str, catno: str = "", year: str = "") -> list[dict]:
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

    def release(self, release_id: int) -> dict:
        return self._get(f"{API}/releases/{release_id}", None, "release", str(release_id))

    def master(self, master_id: int) -> dict:
        return self._get(f"{API}/masters/{master_id}", None, "master", str(master_id))


def rank_by_year(results: list[dict], year: str) -> list[dict]:
    """Stable sort: exact year first, then nearest years, then unknown years.

    Nothing is dropped, so a wrong or missing year in the folder name can
    never hide the right release. score_result() also rewards a year match.
    """
    wanted = year4(year)
    if not wanted:
        return results

    def distance(item: dict) -> int:
        got = year4(item.get("year"))
        return abs(int(got) - int(wanted)) if got else 10_000

    return sorted(results, key=distance)


def load_token() -> tuple[str | None, str | None, str | None]:
    env_token = os.environ.get("DISCOGS_TOKEN", "").strip() or None
    key = os.environ.get("DISCOGS_KEY", "").strip() or None
    secret = os.environ.get("DISCOGS_SECRET", "").strip() or None
    if env_token:
        return env_token, key, secret
    if TOKEN_PATH.exists():
        token = TOKEN_PATH.read_text(encoding="utf-8").strip() or None
        return token, key, secret
    return None, key, secret


def save_token(token: str, path: Path | None = None) -> None:
    """Write the token so it is never readable by other users, not even briefly.

    The file is created under a temporary name with O_CREAT|O_EXCL and mode
    0600, written, fsynced, then renamed over the old token file.
    """
    path = path or TOKEN_PATH
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


def _keep_char(ch: str) -> bool:
    # Letters, marks (needed by Devanagari, Thai, ...) and digits of any script.
    return unicodedata.category(ch)[0] in "LMN"


def norm(text: str) -> str:
    """Unicode-aware comparison form: NFKC, casefold, letters/digits only.

    Japanese, Cyrillic, Greek, ... names keep their letters, so they can
    match. Punctuation and symbols become spaces.
    """
    text = unicodedata.normalize("NFKC", text or "").casefold()
    text = ARTIST_NUM_SUFFIX.sub("", text)
    text = "".join(ch if _keep_char(ch) else " " for ch in text)
    return " ".join(text.split())


def ascii_fold(normed: str) -> str | None:
    """'björk' -> 'bjork'. None unless the result is plain ASCII.

    Only accented Latin text gets an extra folded form; stripping marks from
    kana or Devanagari would merge unrelated words.
    """
    decomposed = unicodedata.normalize("NFKD", normed)
    folded = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    folded = folded.replace("ß", "ss").replace("æ", "ae").replace("ø", "o").replace("œ", "oe").replace("đ", "d").replace("ł", "l")
    return folded if folded.isascii() else None


def _ratio(a: str, b: str) -> float:
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def similar(a: str, b: str) -> float:
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    best = _ratio(na, nb)
    fa, fb = ascii_fold(na), ascii_fold(nb)
    if fa and fb:
        best = max(best, _ratio(fa, fb))
    return best


def split_result_title(title: str) -> tuple[str, str]:
    if " - " in (title or ""):
        artist, album = title.split(" - ", 1)
        return artist.strip(), album.strip()
    return "", title or ""


# --auto only applies a match when the best search hit scores at least
# AUTO_MIN_SCORE and beats the runner-up by at least AUTO_MIN_MARGIN.
# The margin used to be 0.08; 0.10 means two hits that differ only by the
# year bonus (0.07) or a near-identical title can never be auto-picked.
AUTO_MIN_SCORE = 0.90
AUTO_MIN_MARGIN = 0.10


def score_components(result: dict, guess: dict[str, str]) -> dict[str, float]:
    """Weighted parts of score_result, so --auto can show why it picked a hit."""
    r_artist, r_album = split_result_title(result.get("title") or "")
    parts = {
        "album": 0.40 * similar(guess.get("album", ""), r_album),
        "artist": 0.35 * similar(guess.get("artist", ""), r_artist),
        "catno": 0.0,
        "year": 0.0,
    }
    cat = (guess.get("catno") or "").replace(" ", "").upper()
    rcat = (result.get("catno") or "").replace(" ", "").upper()
    if cat and rcat and (cat == rcat or cat in rcat or rcat in cat):
        parts["catno"] = 0.18
    gy, ry = guess.get("year", ""), str(result.get("year") or "")
    if gy and ry and gy == ry:
        parts["year"] = 0.07
    return parts


def format_score_components(parts: dict[str, float]) -> str:
    return "  ".join(f"{k} {v:.2f}" for k, v in parts.items())


def score_result(result: dict, guess: dict[str, str]) -> float:
    return min(sum(score_components(result, guess).values()), 1.0)


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


def _track_artist(item: dict, fallback: str) -> str:
    artists = item.get("artists") or []
    if not artists:
        return fallback
    return " & ".join(
        ARTIST_NUM_SUFFIX.sub("", a.get("anv") or a.get("name") or "").strip()
        for a in artists
    )


def _tracklist_entries(release: dict, album_artist: str) -> list[tuple[str, str, str]]:
    """(position, title, artist) for every playable track, in tracklist order.

    Headings are dropped. Index tracks (a work split into movements) are
    replaced by their sub-tracks, titled "Work: Movement" so the work name
    is not lost. A normal track that lists sub_tracks stays one track.
    """
    entries: list[tuple[str, str, str]] = []
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
                entries.append(((sub.get("position") or "").strip(), title, _track_artist(sub, parent_artist)))
            continue
        title = (item.get("title") or "").strip()
        if not title:
            continue
        entries.append(((item.get("position") or "").strip(), title, _track_artist(item, album_artist)))
    return entries


def flatten_tracklist(release: dict, album_artist: str) -> list[DiscogsTrack]:
    """Discogs tracklist -> numbered tracks.

    Each disc keeps Discogs' own numbers (1, 2, ... or 1-1, 1-2, ...) when
    every track on it has a distinct number. Otherwise, and always for
    vinyl/cassette sides, the disc's tracks are numbered 1..N in tracklist
    order, so A1 A2 B1 B2 become 1 2 3 4 in the track-number tag.

    When every track on a disc has a distinct side position, each track also
    gets `side` (A1, A2, B1 ...), which file names use by default.
    """
    parsed = []
    for position, title, artist in _tracklist_entries(release, album_artist):
        disc, number = parse_position(position)
        parsed.append((max(disc, 1), number, position, title, artist))

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
            )
        )
    return out


def release_artists(release: dict) -> str:
    names = []
    for artist in release.get("artists") or []:
        name = ARTIST_NUM_SUFFIX.sub("", artist.get("name") or "").strip()
        if name:
            names.append(name)
    joined = " & ".join(names) if names else (release.get("artists_sort") or "")
    return ARTIST_NUM_SUFFIX.sub("", joined).strip()


def release_catno(release: dict, preferred: str = "") -> str:
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


def release_discs(release: dict, tracks: list[DiscogsTrack]) -> int:
    from_tracks = max((t.disc for t in tracks), default=1)
    qty = 1
    for fmt in release.get("formats") or []:
        try:
            qty = max(qty, int(fmt.get("qty") or 1))
        except ValueError:
            pass
    return max(from_tracks, qty, 1)


def already_tagged(path: Path) -> str | None:
    audio, tags = _read_tag_file(path)
    if not tags:
        return None
    for key in ("DISCOGS_RELEASE_ID", "discogs_release_id"):
        if key in tags:
            value = tags[key]
            return str(value[0] if isinstance(value, list) else value)
    if isinstance(tags, ID3):
        for frame in tags.getall("TXXX"):
            if (frame.desc or "").casefold() == "discogs_release_id":
                return str(frame.text[0]) if frame.text else None
    if isinstance(audio, MP4) and "----:com.apple.iTunes:DISCOGS_RELEASE_ID" in tags:
        raw = tags["----:com.apple.iTunes:DISCOGS_RELEASE_ID"][0]
        return raw.decode() if isinstance(raw, bytes) else str(raw)
    return None


def album_release_ids(tracks: list[Path]) -> dict[Path, str | None]:
    """Discogs release id of every track (None when a track has none or can't be read)."""
    out: dict[Path, str | None] = {}
    for track in tracks:
        try:
            out[track] = already_tagged(track)
        except Exception:  # noqa: BLE001 - unreadable counts as untagged
            out[track] = None
    return out


# ---------------------------------------------------------------------------
# Tag writing
#
# Every format gets its own conventional field names (Picard/MusicBrainz
# style). Only the fields this tool owns are replaced: before one of them is
# written, every spelling of that field (aliases such as TOTALTRACKS next to
# TRACKTOTAL) is removed so no stale or contradictory copy survives. A field
# with no new value is left alone, except the DISCOGS_* ids, which describe
# the chosen release and are always replaced as a group.
#
# Everything else (ReplayGain, AccurateRip, encoder and gapless info,
# embedded art, lyrics, comments, MusicBrainz ids, ...) is kept. With
# --clear-other-tags the remaining text metadata is removed too, but
# PROTECTED fields (loudness, rip verification, encoder, art, lyrics, ISRC,
# ratings, embedded cue/log) are still kept.
# ---------------------------------------------------------------------------

ALWAYS_REPLACED = {"discogs_release_id", "discogs_master_id", "discogs_position"}
# When a file already carries a different DISCOGS_RELEASE_ID, these values
# came from that other release, so they are replaced even by "no value".
RELEASE_FIELDS = {"date", "year", "originaldate", "originalyear", "label", "catalognumber", "genre"}
ORIGINAL_FIELDS = {"originaldate", "originalyear"}

PROTECTED_SUBSTRINGS = (
    "replaygain",
    "r128",
    "accuraterip",
    "ctdb",
    "itunsmpb",
    "itunnorm",
    "itunpgap",
    "encod",  # ENCODER, ENCODED-BY, ENCODERSETTINGS, WM/EncodingSettings, ...
    "lyrics",
    "isrc",
    "cuesheet",
    "cdtoc",
    "picture",
    "coverart",
    "cover art",
    "rating",
    "playcount",
    "peakvalue",
    "averagelevel",
    "mediaclass",
    "toolname",
    "toolversion",
    "isvbr",
    "deviceconformance",
)
PROTECTED_EXACT = {"log", "eaclog", "crc", "crc32", "©too", "©lyr", "covr", "pgap"}


def is_protected_key(name: str) -> bool:
    key = name.casefold()
    if key.startswith("----:"):
        key = key.rsplit(":", 1)[-1]
    return key in PROTECTED_EXACT or any(part in key for part in PROTECTED_SUBSTRINGS)


def clean_date(value: str | int | None) -> str:
    """Discogs writes unknown parts as 00 ("1999-00-00"); drop them."""
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?", text)
    if not match:
        return year4(text)
    year, month, day = match.groups()
    if not month or month == "00":
        return year
    if not day or day == "00":
        return f"{year}-{month}"
    return f"{year}-{month}-{day}"


def tag_values(meta: dict) -> dict[str, str | None]:
    """meta -> canonical field values as text; None means 'no value'."""

    def text(value) -> str | None:
        if value is None:
            return None
        value = str(value).strip()
        return value if value and value != "0" else None

    date = clean_date(meta.get("date"))
    original = clean_date(meta.get("originaldate"))
    return {
        "artist": text(meta.get("artist")),
        "albumartist": text(meta.get("albumartist")),
        "album": text(meta.get("album")),
        "title": text(meta.get("title")),
        "tracknumber": text(meta.get("track")),
        "tracktotal": text(meta.get("tracktotal")),
        "discnumber": text(meta.get("disc")),
        "disctotal": text(meta.get("disctotal")),
        "date": text(date),
        "year": text(year4(date)),
        "originaldate": text(original),
        "originalyear": text(year4(original)),
        "label": text(meta.get("label")),
        "catalognumber": text(meta.get("catalognumber")),
        "genre": text(meta.get("genre")),
        "discogs_release_id": text(meta.get("release_id")),
        "discogs_master_id": text(meta.get("master_id")),
        "discogs_position": text(meta.get("position")),
    }


# canonical field -> (key written, other spellings of the same field)
VORBIS_FIELDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "artist": ("ARTIST", ()),
    "albumartist": ("ALBUMARTIST", ("ALBUM ARTIST", "ALBUM_ARTIST")),
    "album": ("ALBUM", ()),
    "title": ("TITLE", ()),
    "tracknumber": ("TRACKNUMBER", ()),
    "tracktotal": ("TRACKTOTAL", ("TOTALTRACKS",)),
    "discnumber": ("DISCNUMBER", ()),
    "disctotal": ("DISCTOTAL", ("TOTALDISCS",)),
    "date": ("DATE", ()),
    "year": ("YEAR", ()),
    "originaldate": ("ORIGINALDATE", ()),
    "originalyear": ("ORIGINALYEAR", ()),
    "label": ("LABEL", ("ORGANIZATION", "PUBLISHER")),
    "catalognumber": ("CATALOGNUMBER", ("CATALOG",)),
    "genre": ("GENRE", ()),
    "discogs_release_id": ("DISCOGS_RELEASE_ID", ()),
    "discogs_master_id": ("DISCOGS_MASTER_ID", ()),
    "discogs_position": ("DISCOGS_POSITION", ()),
}

# APEv2 (Monkey's Audio, WavPack, Musepack). Keys are case-insensitive.
APE_FIELDS: dict[str, tuple[str, tuple[str, ...]]] = {
    **VORBIS_FIELDS,
    "artist": ("Artist", ()),
    "albumartist": ("Album Artist", ("ALBUMARTIST", "ALBUM_ARTIST")),
    "album": ("Album", ()),
    "title": ("Title", ()),
    "tracknumber": ("Track", ("TRACKNUMBER", "TRACKTOTAL", "TOTALTRACKS")),  # "3/12"
    "tracktotal": ("Track", ()),
    "discnumber": ("Disc", ("DISCNUMBER", "DISCTOTAL", "TOTALDISCS")),  # "1/2"
    "disctotal": ("Disc", ()),
    "date": ("Year", ("DATE",)),
    "year": ("Year", ()),
    "label": ("Label", ("ORGANIZATION", "PUBLISHER")),
    "catalognumber": ("CatalogNumber", ("CATALOG",)),
    "genre": ("Genre", ()),
}

# Windows Media (ASF) attribute names, as Picard writes them.
ASF_FIELDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "artist": ("Author", ()),
    "albumartist": ("WM/AlbumArtist", ()),
    "album": ("WM/AlbumTitle", ()),
    "title": ("Title", ()),
    "tracknumber": ("WM/TrackNumber", ("WM/Track",)),
    "tracktotal": ("TotalTracks", ("WM/TrackTotal",)),
    "discnumber": ("WM/PartOfSet", ()),
    "disctotal": ("TotalDiscs", ("WM/DiscTotal",)),
    "date": ("WM/Year", ()),
    "originaldate": ("WM/OriginalReleaseTime", ()),
    "originalyear": ("WM/OriginalReleaseYear", ()),
    "label": ("WM/Publisher", ()),
    "catalognumber": ("WM/CatalogNo", ()),
    "genre": ("WM/Genre", ()),
    "discogs_release_id": ("DISCOGS_RELEASE_ID", ()),
    "discogs_master_id": ("DISCOGS_MASTER_ID", ()),
    "discogs_position": ("DISCOGS_POSITION", ()),
}

FF = "----:com.apple.iTunes:"
MP4_FIELDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "artist": ("\xa9ART", ()),
    "albumartist": ("aART", ()),
    "album": ("\xa9alb", ()),
    "title": ("\xa9nam", ()),
    "tracknumber": ("trkn", ()),
    "discnumber": ("disk", ()),
    "date": ("\xa9day", ()),
    "originaldate": (FF + "ORIGINALDATE", ()),
    "originalyear": (FF + "ORIGINALYEAR", ()),
    "label": (FF + "LABEL", (FF + "publisher",)),
    "catalognumber": (FF + "CATALOGNUMBER", ()),
    "genre": ("\xa9gen", ("gnre",)),
    "discogs_release_id": (FF + "DISCOGS_RELEASE_ID", ()),
    "discogs_master_id": (FF + "DISCOGS_MASTER_ID", ()),
    "discogs_position": (FF + "DISCOGS_POSITION", ()),
}
MP4_TEXT_ATOMS = {"aART", "soal", "soar", "soaa", "sonm", "soco", "desc", "ldes", "cprt", "gnre", "cpil", "tmpo", "purl", "egid"}


def _apply_fields(tags, fields, values: dict, clear_other: bool, clearable, replace: set[str]) -> list[str]:
    """Replace owned fields in a dict-like tag container.

    values maps canonical field -> value in the container's own format, or
    None for 'no new value'. Fields in replace are cleared even without a
    new value. Returns keys removed and not written again.
    """
    removed: list[str] = []
    owned: set[str] = set()
    written: set[str] = set()
    for name, (key, aliases) in fields.items():
        spellings = {key.casefold(), *(a.casefold() for a in aliases)}
        owned |= spellings
        value = values.get(name)
        if value is None and name not in replace:
            continue
        for existing in list(tags.keys()):
            if existing.casefold() in spellings and existing.casefold() not in written:
                try:
                    del tags[existing]
                except KeyError:
                    continue
                if value is None or existing.casefold() != key.casefold():
                    removed.append(existing)
        if value is not None:
            tags[key] = value
            written.add(key.casefold())
    if clear_other:
        for existing in list(tags.keys()):
            if existing.casefold() in owned or is_protected_key(existing) or not clearable(tags, existing):
                continue
            try:
                del tags[existing]
            except KeyError:
                continue
            removed.append(existing)
    return removed


def _always_clearable(_tags, _key) -> bool:
    return True


def _write_vorbis(audio, meta: dict, clear_other: bool, replace: set[str]) -> list[str]:
    if audio.tags is None:
        audio.add_tags()
    values = {name: ([v] if v is not None else None) for name, v in tag_values(meta).items()}
    return _apply_fields(audio.tags, VORBIS_FIELDS, values, clear_other, _always_clearable, replace)


def _write_ape(audio, meta: dict, clear_other: bool, replace: set[str]) -> list[str]:
    if audio.tags is None:
        audio.add_tags()
    tv = tag_values(meta)
    values = dict(tv)
    values["tracknumber"] = f"{tv['tracknumber']}/{tv['tracktotal']}" if tv["tracktotal"] else tv["tracknumber"]
    values["discnumber"] = f"{tv['discnumber']}/{tv['disctotal']}" if tv["disctotal"] else tv["discnumber"]
    values["tracktotal"] = values["disctotal"] = values["year"] = None

    def clearable(tags, key):
        return getattr(tags[key], "kind", 0) == 0  # text items only, never binary (art)

    return _apply_fields(audio.tags, APE_FIELDS, values, clear_other, clearable, replace)


def _write_asf(audio, meta: dict, clear_other: bool, replace: set[str]) -> list[str]:
    from mutagen.asf import ASFUnicodeAttribute

    if audio.tags is None:
        audio.add_tags()
    tv = tag_values(meta)
    values: dict = {name: tv.get(name) for name in ASF_FIELDS}
    if tv["disctotal"] and tv["discnumber"]:
        values["discnumber"] = f"{tv['discnumber']}/{tv['disctotal']}"

    def clearable(tags, key):
        return all(isinstance(v, ASFUnicodeAttribute) for v in tags[key])

    return _apply_fields(audio.tags, ASF_FIELDS, values, clear_other, clearable, replace)


def _write_mp4_tags(audio, meta: dict, clear_other: bool, replace: set[str]) -> list[str]:
    if audio.tags is None:
        audio.add_tags()
    tv = tag_values(meta)
    values: dict = {}
    for name, value in tv.items():
        if name not in MP4_FIELDS:
            continue
        key = MP4_FIELDS[name][0]
        if value is None:
            values[name] = None
        elif key.startswith("----:"):
            values[name] = [MP4FreeForm(value.encode("utf-8"))]
        else:
            values[name] = [value]
    values["tracknumber"] = [(int(meta["track"]), int(meta.get("tracktotal") or 0))]
    values["discnumber"] = [(int(meta["disc"]), int(meta.get("disctotal") or 0))]

    def clearable(_tags, key):
        return key.startswith("\xa9") or key.startswith("----:") or key in MP4_TEXT_ATOMS

    return _apply_fields(audio.tags, MP4_FIELDS, values, clear_other, clearable, replace)


# ID3v2.4 (MP3, and ID3 chunks inside WAV and AIFF)
ID3_TEXT_FRAMES = {
    "artist": ("TPE1", TPE1),
    "albumartist": ("TPE2", TPE2),
    "album": ("TALB", TALB),
    "title": ("TIT2", TIT2),
    "date": ("TDRC", TDRC),
    "originaldate": ("TDOR", TDOR),
    "label": ("TPUB", TPUB),
    "genre": ("TCON", TCON),
}
# TXXX descriptions that are other spellings of a field written as a frame
ID3_TXXX_ALIASES = {
    "albumartist": ("ALBUM ARTIST", "ALBUMARTIST"),
    "date": ("DATE", "YEAR"),
    "originaldate": ("ORIGINALDATE", "ORIGINALYEAR", "ORIGINAL YEAR"),
    "label": ("LABEL", "PUBLISHER", "ORGANIZATION"),
    "tracknumber": ("TRACKTOTAL", "TOTALTRACKS"),
    "discnumber": ("DISCTOTAL", "TOTALDISCS"),
}
ID3_TXXX_FIELDS = {
    "catalognumber": ("CATALOGNUMBER", ("CATALOG",)),
    "discogs_release_id": ("DISCOGS_RELEASE_ID", ()),
    "discogs_master_id": ("DISCOGS_MASTER_ID", ()),
    "discogs_position": ("DISCOGS_POSITION", ()),
}
ID3_PROTECTED_FRAMES = {"TSSE", "TENC", "TLEN", "TFLT", "TSRC", "TDEN", "TOWN"}


def _del_txxx(tags: ID3, descs) -> list[str]:
    wanted = {d.casefold() for d in descs}
    removed = []
    for frame in list(tags.getall("TXXX")):
        if (frame.desc or "").casefold() in wanted:
            tags.delall(frame.HashKey)
            removed.append(frame.HashKey)
    return removed


def _apply_id3(tags: ID3, meta: dict, clear_other: bool, replace: set[str]) -> list[str]:
    tv = tag_values(meta)
    removed: list[str] = []

    def pair(n, total):
        return f"{n}/{total}" if total else n

    for name, (frame_id, cls) in ID3_TEXT_FRAMES.items():
        value = tv[name]
        if value is None:
            if name in replace:
                if tags.getall(frame_id):
                    tags.delall(frame_id)
                    removed.append(frame_id)
                removed += _del_txxx(tags, ID3_TXXX_ALIASES.get(name, ()))
            continue
        tags.delall(frame_id)
        removed += _del_txxx(tags, ID3_TXXX_ALIASES.get(name, ()))
        tags.add(cls(encoding=3, text=value))
    if tv["date"]:
        for old in ("TYER", "TDAT", "TIME"):  # v2.3 date frames; TDRC replaces them
            if tags.getall(old):
                tags.delall(old)
                removed.append(old)
    if (tv["originaldate"] or "originaldate" in replace) and tags.getall("TORY"):
        tags.delall("TORY")
        removed.append("TORY")
    for name, frame_id, cls, total in (
        ("tracknumber", "TRCK", TRCK, "tracktotal"),
        ("discnumber", "TPOS", TPOS, "disctotal"),
    ):
        if tv[name] is None:
            continue
        tags.delall(frame_id)
        removed += _del_txxx(tags, ID3_TXXX_ALIASES.get(name, ()))
        tags.add(cls(encoding=3, text=pair(tv[name], tv[total])))
    for name, (desc, aliases) in ID3_TXXX_FIELDS.items():
        value = tv[name]
        if value is None and name not in replace:
            continue
        gone = _del_txxx(tags, (desc, *aliases))
        if value is None:
            removed += gone
        else:
            removed += [k for k in gone if k.casefold() != f"txxx:{desc}".casefold()]
            tags.add(TXXX(encoding=3, desc=desc, text=value))

    if clear_other:
        owned_txxx = {d.casefold() for d, _ in ID3_TXXX_FIELDS.values()}
        for key in list(tags.keys()):
            frame = tags[key]
            fid = frame.FrameID
            if fid == "TXXX":
                desc = frame.desc or ""
                if desc.casefold() in owned_txxx or is_protected_key(desc):
                    continue
            elif fid == "COMM":
                if is_protected_key(frame.desc or ""):
                    continue
            elif fid.startswith("T"):
                if fid in ID3_PROTECTED_FRAMES or fid in {"TRCK", "TPOS"} or fid in {f for f, _ in ID3_TEXT_FRAMES.values()}:
                    continue
            elif not (fid.startswith("W") or fid == "UFID"):
                continue  # art, lyrics, ratings, private and binary frames stay
            tags.delall(key)
            removed.append(key)
    return removed


def _load_id3_for_mp3(path: Path) -> ID3:
    try:
        return ID3(path)
    except ID3NoHeaderError:
        return ID3()


def _atomic_tag_update(path: Path, edit):
    """Run edit(tmp) on a copy of path, then put the copy in place atomically.

    The copy is a hidden sibling (same folder, so os.replace is atomic) made
    with mkstemp; it gets the original's permission bits and, where allowed,
    its owner and group. After edit() saves into it, it is fsynced and
    renamed over the original. A crash or error at any point leaves the
    original file untouched (the temporary copy is removed on errors). A
    symlinked track is updated at its real location, keeping the link.
    The new file has a new inode, so journals must identify it by SHA-256.
    """
    real = Path(os.path.realpath(path))
    st = os.stat(real)
    fd, tmp_name = tempfile.mkstemp(dir=real.parent, prefix=f".{real.name}.discogs-tag-", suffix=real.suffix)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out, open(real, "rb") as src:
            shutil.copyfileobj(src, out, COPY_CHUNK)
        os.chmod(tmp, stat.S_IMODE(st.st_mode))
        try:
            os.chown(tmp, st.st_uid, st.st_gid)
        except OSError:
            pass  # not allowed for other users' files; the data is what matters
        result = edit(tmp)
        with open(tmp, "rb+") as fh:
            os.fsync(fh.fileno())
        os.replace(tmp, real)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dir_fd = os.open(real.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass
    return result


def write_tags(path: Path, meta: dict, clear_other: bool = False, dry_run: bool = False) -> list[str]:
    """Write meta into path with the format's own tag names, atomically.

    Returns the tag keys that were removed and not rewritten. With dry_run
    the file is read and the changes are computed, but nothing is saved.
    A real write goes to a temporary copy that replaces the file only after
    it was saved and fsynced (see _atomic_tag_update).
    """
    if dry_run:
        return _write_tags_file(path, meta, clear_other, dry_run=True)
    previous = already_tagged(path)
    return _atomic_tag_update(path, lambda tmp: _write_tags_file(tmp, meta, clear_other, previous=previous))


def _write_tags_file(path: Path, meta: dict, clear_other: bool = False, dry_run: bool = False,
                     previous: str | None = None) -> list[str]:
    """Write meta into path with the format's own tag names.

    Returns the tag keys that were removed and not rewritten (stale aliases,
    and with clear_other also unowned text fields). With dry_run the file is
    read and the changes are computed, but nothing is saved.
    """
    replace = set(ALWAYS_REPLACED)
    if previous is None:
        previous = already_tagged(path)
    if previous and meta.get("release_id") and previous.strip() != str(meta["release_id"]):
        replace |= RELEASE_FIELDS
    if previous and not tag_values(meta)["originaldate"]:
        # Not a reissue: drop an original date an earlier run of this tool
        # wrote. Files never tagged by this tool keep theirs.
        replace |= ORIGINAL_FIELDS

    suffix = path.suffix.lower()
    if suffix == ".mp3":
        # ID3 only: works for tag-only or damaged files the MP3 parser rejects.
        tags = _load_id3_for_mp3(path)
        removed = _apply_id3(tags, meta, clear_other, replace)
        if not dry_run:
            tags.save(path, v2_version=4)
        return removed

    audio = MutagenFile(path)
    if audio is None:
        raise RuntimeError(f"unrecognised audio file: {path}")

    if isinstance(audio, (WAVE, AIFF, MP3)):
        if audio.tags is None:
            audio.add_tags()
        removed = _apply_id3(audio.tags, meta, clear_other, replace)
        if not dry_run:
            audio.save(v2_version=4)
        return removed
    if isinstance(audio, MP4):
        removed = _write_mp4_tags(audio, meta, clear_other, replace)
    elif isinstance(audio, ASF):
        removed = _write_asf(audio, meta, clear_other, replace)
    elif isinstance(audio, (FLAC, OggVorbis, OggOpus, OggFLAC, OggSpeex)):
        removed = _write_vorbis(audio, meta, clear_other, replace)
    elif isinstance(audio, APEv2File):
        removed = _write_ape(audio, meta, clear_other, replace)
    else:
        raise RuntimeError(f"tag writing not supported for {type(audio).__name__}: {path}")
    if not dry_run:
        audio.save()
    return removed


# ---------------------------------------------------------------------------
# Matching local files to Discogs tracks
# ---------------------------------------------------------------------------

def _disc_from_folder(local: Path, album_path: Path) -> int | None:
    parent = local.parent
    if parent == album_path or not DISC_DIR.match(parent.name):
        return None
    match = re.search(r"\d+", parent.name)
    return int(match.group(0)) if match else None


# Minimum similar() score for pairing an unnumbered file with a track by title.
TITLE_MATCH_MIN = 0.80


def _title_from_stem(stem: str) -> str:
    """The title part of a file name: track/side/disc prefixes stripped."""
    for pattern in (SIDE_PREFIX, DISC_TRACK_PREFIX, TRACK_PREFIX, SPACED_TRACK_PREFIX):
        m = pattern.match(stem)
        if m:
            stem = stem[m.end():]
            break
    return stem.strip(" .-_")


def match_tracks(album: AlbumFolder, dtracks: list[DiscogsTrack]) -> tuple[list[tuple[Path, DiscogsTrack | None]], bool]:
    """Pair local files with Discogs tracks.

    1. A file named after a side position the release has ("B2. Title")
       gets that track.
    2. A leading number ("06 Title", "6. Title", "2-06 Title") is matched
       against the track numbers; a CD1/CD2 folder limits it to that disc.
       Files renamed by older versions (B1 = 21) are recognised and matched
       by that old number when that fits the folder better.
    3. Anything left is matched by title similarity (TITLE_MATCH_MIN).
    4. If the folder has exactly as many files as the release has tracks,
       whatever is still left takes the remaining tracks in order. Otherwise
       it stays unmatched (None): reported and not tagged or moved.

    Returns the pairs and whether the old vinyl numbering was detected.
    """
    discs = {dt.disc for dt in dtracks}
    max_disc = max(discs, default=1)
    side_positions = {
        dt.position.casefold(): i for i, dt in enumerate(dtracks) if dt.position and is_side_position(dt.position)
    }

    info: list[tuple[Path, int | None, int | None, int | None]] = []
    for local in album.tracks:
        disc_hint = _disc_from_folder(local, album.path)
        number = side_index = None
        stem = local.stem
        side = SIDE_PREFIX.match(stem)
        disc_track = DISC_TRACK_PREFIX.match(stem)
        plain = TRACK_PREFIX.match(stem) or SPACED_TRACK_PREFIX.match(stem)
        if side and side.group(1).casefold() in side_positions:
            side_index = side_positions[side.group(1).casefold()]
        elif disc_track and len(discs) > 1 and disc_hint is None and 1 <= int(disc_track.group(1)) <= max_disc:
            disc_hint, number = int(disc_track.group(1)), int(disc_track.group(2))
        elif plain:
            number = int(plain.group(1))
        info.append((local, disc_hint, number, side_index))

    use_legacy = False
    if any(dt.legacy_number is not None and dt.legacy_number != dt.number for dt in dtracks):
        numbers = [n for _l, _d, n, _s in info if n is not None]
        new_hits = sum(1 for n in numbers if any(dt.number == n for dt in dtracks))
        old_hits = sum(1 for n in numbers if any(dt.legacy_number == n for dt in dtracks))
        use_legacy = old_hits > new_hits

    def number_of(dt: DiscogsTrack) -> int:
        if use_legacy and dt.legacy_number is not None:
            return dt.legacy_number
        return dt.number

    used: set[int] = set()
    chosen: list[int | None] = [None] * len(info)
    for k, (_local, disc_hint, number, side_index) in enumerate(info):
        if side_index is not None and side_index not in used:
            chosen[k] = side_index
            used.add(side_index)
            continue
        if number is None:
            continue
        for i, dt in enumerate(dtracks):
            if i in used or number_of(dt) != number:
                continue
            if disc_hint is not None and dt.disc != disc_hint:
                continue
            chosen[k] = i
            used.add(i)
            break

    # 3. Leftovers: match by normalized title, best pairs first.
    candidates: list[tuple[float, int, int]] = []
    for k, (local, disc_hint, _number, _side) in enumerate(info):
        if chosen[k] is not None:
            continue
        local_title = _title_from_stem(local.stem)
        if not local_title:
            continue
        for i, dt in enumerate(dtracks):
            if i in used or (disc_hint is not None and dt.disc != disc_hint):
                continue
            score = similar(local_title, dt.title)
            if score >= TITLE_MATCH_MIN:
                candidates.append((score, k, i))
    for _score, k, i in sorted(candidates, key=lambda c: (-c[0], c[1], c[2])):
        if chosen[k] is None and i not in used:
            chosen[k] = i
            used.add(i)

    # 4. Position fallback, only when the folder has exactly as many files as
    #    the release has tracks (so every leftover file has a leftover track).
    #    Otherwise unmatched files stay None: reported and left untagged.
    if len(info) == len(dtracks):
        for k, (_local, disc_hint, _number, _side) in enumerate(info):
            if chosen[k] is not None:
                continue
            free = [i for i in range(len(dtracks)) if i not in used]
            if disc_hint is not None:
                free = [i for i in free if dtracks[i].disc == disc_hint] or free
            if free:
                chosen[k] = free[0]
                used.add(free[0])

    pairs = [(info[k][0], dtracks[i] if i is not None else None) for k, i in enumerate(chosen)]
    return pairs, use_legacy


# ---------------------------------------------------------------------------
# Applying a release: tag everything, then move everything
# ---------------------------------------------------------------------------

class AlbumApplyError(RuntimeError):
    """An album could not be fully applied. The message says what happened."""


@dataclass
class PlannedFile:
    source: Path
    dest: Path
    meta: dict | None = None  # None for sidecars and extra folders


def unique_dest(path: Path) -> Path:
    if not os.path.lexists(path):
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(2, 50):
        candidate = path.with_name(f"{stem} ({i}){suffix}")
        if not os.path.lexists(candidate):
            return candidate
    raise RuntimeError(f"cannot find free name for {path}")


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


COPY_CHUNK = 4 * 1024 * 1024


def _copy_verified(src: Path, dest: Path) -> None:
    """Copy one file to dest (which must not exist), verifying every byte.

    The data goes to a hidden partial file next to dest, is flushed to disk,
    and is read back: size and SHA-256 must match the source. Only then is
    it given its real name. On any problem the partial file is removed and
    the source is untouched.
    """
    fd, partial_name = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.discogs-tag-partial-")
    partial = Path(partial_name)
    src_hash = hashlib.sha256()
    try:
        with open(src, "rb") as fin, os.fdopen(fd, "wb") as fout:
            while chunk := fin.read(COPY_CHUNK):
                src_hash.update(chunk)
                fout.write(chunk)
            fout.flush()
            os.fsync(fout.fileno())
        try:
            shutil.copystat(src, partial)
        except OSError:
            pass  # timestamps/permissions are nice to have; the data is what matters
        dest_hash = hashlib.sha256()
        with open(partial, "rb") as fcheck:
            while chunk := fcheck.read(COPY_CHUNK):
                dest_hash.update(chunk)
        if os.path.getsize(partial) != os.path.getsize(src) or dest_hash.digest() != src_hash.digest():
            raise OSError(errno.EIO, f"copy verification failed for {src} -> {dest}")
        if os.path.lexists(dest):
            raise FileExistsError(errno.EEXIST, f"{dest} appeared during the copy")
        os.rename(partial, dest)
    except BaseException:
        try:
            os.unlink(partial)
        except OSError:
            pass
        raise


def _move_across_devices(src: Path, dest: Path, journal: "Journal | None" = None) -> None:
    """True move between drives: verified copy first, then delete the source.

    Directories are moved file by file the same way; their emptied source
    folders are then removed with rmdir (only when truly empty). With a
    journal, every child step (mkdir, file/symlink move, rmdir) is recorded
    before it happens, so an interrupted directory move can be undone file
    by file.
    """
    if src.is_symlink():
        os.symlink(os.readlink(src), dest)
        os.unlink(src)
        return
    if not src.is_dir():
        _copy_verified(src, dest)
        os.unlink(src)
        return
    if journal is not None:
        journal.write(action="mkdir", path=str(dest))
    dest.mkdir()
    shutil.copystat(src, dest, follow_symlinks=False)
    for entry in sorted(src.iterdir()):
        child = dest / entry.name
        if journal is not None and (entry.is_symlink() or not entry.is_dir()):
            _journal_rename(journal, "file", entry, child)
        _move_across_devices(entry, child, journal)
    _rmdir_logged(journal, src)  # fails (and stops) if anything is still inside


def _move(src: Path, dest: Path, journal: Journal | None = None) -> Path:
    """Move src to dest without ever overwriting a different file.

    Same drive: a rename. Different drive: copy, verify size and SHA-256,
    then delete the source, so a move never leaves a second copy behind.
    """
    _mkdirs_logged(journal, dest.parent)
    kind = "dir" if src.is_dir() and not src.is_symlink() else "file"
    if dest.exists() or dest.is_symlink():
        if _same_file(src, dest):
            if str(src) != str(dest):
                _journal_rename(journal, kind, src, dest)
                os.rename(src, dest)  # case-only rename on a case-insensitive drive
            return dest
        dest = unique_dest(dest)
    _journal_rename(journal, kind, src, dest)
    try:
        os.rename(src, dest)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        _move_across_devices(src, dest, journal)
    return dest


def file_sha256(path: Path) -> str | None:
    """SHA-256 of a regular file, or None if it can't be read."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def _tag_container(path: Path):
    """(audio or None, tags or None) opened the same way write_tags opens them."""
    if path.suffix.lower() == ".mp3":
        try:
            return None, ID3(path)
        except ID3NoHeaderError:
            return None, None
    audio = MutagenFile(path)
    if audio is None:
        raise OSError(f"unrecognised audio file: {path}")
    return audio, audio.tags


def capture_tags(path: Path) -> dict:
    """Snapshot of every tag in path, enough for restore_tags to put them all back.

    ID3 keeps its frames and version; other formats keep their key/value
    pairs. Values are pickled so binary fields (cover art, MP4 free-form
    atoms) survive. Audio data is never copied.
    """
    _audio, tags = _tag_container(path)
    if tags is None:
        return {"kind": "none"}
    if isinstance(tags, ID3):
        payload = list(tags.values())
        return {"kind": "id3", "version": list(tags.version),
                "data": base64.b64encode(pickle.dumps(payload)).decode("ascii")}
    if isinstance(tags, list):
        # Vorbis comments and ASF are ordered lists of pairs; keep order and case.
        snap = {"kind": "list", "data": base64.b64encode(pickle.dumps(list(list.__iter__(tags)))).decode("ascii")}
        if hasattr(tags, "vendor"):
            snap["vendor"] = tags.vendor
        return snap
    payload = [(k, v) for k, v in tags.items()]
    return {"kind": "map", "data": base64.b64encode(pickle.dumps(payload)).decode("ascii")}


def restore_tags(path: Path, snapshot: dict) -> None:
    """Replace every tag in path with a capture_tags() snapshot, atomically like write_tags."""
    _atomic_tag_update(path, lambda tmp: _restore_tags_file(tmp, snapshot))


def _restore_tags_file(path: Path, snapshot: dict) -> None:
    kind = snapshot.get("kind")
    payload = pickle.loads(base64.b64decode(snapshot["data"])) if "data" in snapshot else []
    if path.suffix.lower() == ".mp3":
        if kind == "none":
            try:
                ID3(path).delete(path)
            except ID3NoHeaderError:
                pass
            return
        tags = ID3()
        for frame in payload:
            tags.add(frame)
        version = (snapshot.get("version") or [2, 4, 0])[1]
        tags.save(path, v2_version=3 if version == 3 else 4)
        return
    audio = MutagenFile(path)
    if audio is None:
        raise OSError(f"unrecognised audio file: {path}")
    if kind == "none":
        if audio.tags is not None:
            audio.delete()
        return
    if audio.tags is None:
        audio.add_tags()
    if kind == "id3":
        audio.tags.clear()
        for frame in payload:
            audio.tags.add(frame)
        version = (snapshot.get("version") or [2, 4, 0])[1]
        audio.save(v2_version=3 if version == 3 else 4)
        return
    if kind == "list" and isinstance(audio.tags, list):
        list.__delitem__(audio.tags, slice(None))
        list.extend(audio.tags, payload)
        if "vendor" in snapshot and hasattr(audio.tags, "vendor"):
            audio.tags.vendor = snapshot["vendor"]
        audio.save()
        return
    for key in list(audio.tags.keys()):
        del audio.tags[key]
    for key, value in payload:
        audio.tags[key] = value
    audio.save()


def _journal_rename(journal: "Journal | None", kind: str, src: Path, dest: Path) -> None:
    """Record a rename before it happens, including the file's current hash."""
    if journal is None:
        return
    st = os.lstat(src)
    entry = {"action": "rename", "kind": kind, "old": str(src), "new": str(dest), "dev": st.st_dev, "ino": st.st_ino}
    if stat.S_ISREG(st.st_mode):
        entry["sha256"] = file_sha256(src)
    elif stat.S_ISLNK(st.st_mode):
        entry["link"] = os.readlink(src)  # a cross-device move recreates the link (new inode)
    journal.write(**entry)


def _mkdirs_logged(journal: "Journal | None", folder: Path) -> None:
    """mkdir -p, recording each folder it creates (outermost first) before creating it."""
    missing = []
    current = folder
    while not os.path.lexists(current):
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    for path in reversed(missing):
        if journal is not None:
            journal.write(action="mkdir", path=str(path))
        path.mkdir(exist_ok=True)


def _trash_logged(journal: "Journal | None", path: Path) -> Path:
    """Move path to the trash, recording it first. Returns the trash location."""
    sha = file_sha256(path) if path.is_file() else None
    if journal is not None:
        journal.write(action="trash", path=str(path.resolve()), sha256=sha)
    trashed = mm_common.send_to_trash(path)
    if journal is not None:
        journal.write(action="trashed", path=str(path.resolve()), trashed=str(trashed), sha256=sha)
    return trashed


def move_planned(items: list[PlannedFile], done: list[tuple[Path, Path]], journal: Journal | None = None) -> None:
    """Move every item, appending (source, final destination) to done.

    When a destination is currently occupied by another file of the same
    album that is about to be renamed (re-tagging in place), that file is
    first moved to a temporary name so nothing ends up as "Title (2)".
    done is filled as moves happen, so after a failure it lists exactly
    what was moved. It is the natural hook for a future undo journal.
    """
    sources = [item.source for item in items]
    for item in items:
        if not (item.dest.exists() or item.dest.is_symlink()):
            continue
        blocker = next(
            (s for s in sources if s is not item.source and _same_file(s, item.dest) and not _same_file(s, item.source)),
            None,
        )
        if blocker is None:
            continue
        staged = _reserve_staging_name(blocker)
        try:
            _journal_rename(journal, "stage", blocker, staged)
            os.rename(blocker, staged)
        except BaseException:
            _release_staging_name(staged)
            raise
        done.append((blocker, staged))
        for other in items:
            if other.source is blocker:
                other.source = staged
        sources = [i.source for i in items]
    for item in items:
        final = _move(item.source, item.dest, journal)
        done.append((item.source, final))


def _reserve_staging_name(blocker: Path) -> Path:
    """Atomically reserve a unique hidden name next to blocker for staging it.

    An empty placeholder of the same kind (file or directory) is created, so
    no other process (or a stale leftover) can take the name; the rename
    then replaces the placeholder.
    """
    prefix = f".discogs-tag-stage-{blocker.name}."
    if blocker.is_dir() and not blocker.is_symlink():
        return Path(tempfile.mkdtemp(dir=blocker.parent, prefix=prefix))
    fd, name = tempfile.mkstemp(dir=blocker.parent, prefix=prefix)
    os.close(fd)
    return Path(name)


def _release_staging_name(staged: Path) -> None:
    """Remove an unused staging placeholder (only if it is still empty)."""
    try:
        if staged.is_dir() and not staged.is_symlink():
            staged.rmdir()
        elif staged.is_file() and staged.stat().st_size == 0:
            staged.unlink()
    except OSError:
        pass


def describe_moves(done: list[tuple[Path, Path]], pending: list[PlannedFile]) -> str:
    lines = []
    if done:
        lines.append(f"  moved {len(done)} item(s):")
        lines += [f"    {src}  ->  {dest}" for src, dest in done]
    else:
        lines.append("  nothing was moved")
    if pending:
        lines.append(f"  not moved ({len(pending)}):")
        lines += [f"    {item.source}" for item in pending]
    return "\n".join(lines)


def validate_layout(album: AlbumFolder, dest_root: Path, target_dir: Path) -> None:
    """Refuse layouts that would make an apply write into or through its own source.

    Raises AlbumApplyError (before anything is written) when the destination
    library or the album's target folder lies inside the source album, when the
    target folder contains the source album (the album would be emptied into
    one of its parents), or when a symlink in the album points outside it
    (tagging would modify a file elsewhere). Re-tagging in place (target is
    the album folder itself) is allowed.
    """
    src = album.path.resolve()
    target = target_dir.resolve()
    root = dest_root.resolve()
    if target != src:
        if root == src or root.is_relative_to(src):
            raise AlbumApplyError(f"refusing: destination {dest_root} is inside the source album {album.path}\n  nothing was changed")
        if target.is_relative_to(src):
            raise AlbumApplyError(f"refusing: target folder {target_dir} is inside the source album {album.path}\n  nothing was changed")
        if src.is_relative_to(target):
            raise AlbumApplyError(
                f"refusing: target folder {target_dir} contains the source album {album.path}\n  nothing was changed")
    escaping: list[str] = []
    for dirpath, dirnames, filenames in os.walk(album.path, followlinks=False):
        for name in dirnames + filenames:
            entry = Path(dirpath) / name
            if not entry.is_symlink():
                continue
            real = entry.resolve()
            if real != src and not real.is_relative_to(src):
                escaping.append(f"    {entry}  ->  {real}")
    if escaping:
        raise AlbumApplyError(
            "refusing: symlinks in the album point outside it (their targets would be re-tagged or moved):\n"
            + "\n".join(escaping) + "\n  nothing was changed")


def apply_album(
    album: AlbumFolder,
    dest_root: Path,
    release: dict,
    dtracks: list[DiscogsTrack],
    original_year: str,
    dry_run: bool,
    clear_other: bool = False,
    scan_root: Path | None = None,
    journal_dir: Path | None = None,
) -> list[tuple[Path, Path]]:
    """Tag the album's files, then move them into place.

    Order: plan everything, write tags to every file in place (nothing is
    moved if any file fails), then move tracks, extra folders and sidecars.
    original_year is the master's year ("" when unknown); it is only used
    for a reissue. Afterwards the old folder, and parents that became empty,
    are removed (see prune_limit). Returns the (source, destination)
    list of moves made.
    """
    album_artist = release_artists(release)
    folder_artist = format_artist(album_artist)
    title = release.get("title") or ""
    release_year = year4(release.get("released") or release.get("year"))
    catno = release_catno(release)
    label = ""
    if release.get("labels"):
        label = release["labels"][0].get("name") or ""
    genres = release.get("genres") or []
    styles = release.get("styles") or []
    genre = ", ".join(styles[:2] or genres[:2])
    discs = release_discs(release, dtracks)
    master_id = release.get("master_id") or 0
    # Original date only for reissues (master year earlier than this release).
    orig = reissue_original_year(original_year, release_year)

    folder = album_folder_name(title, original_year, release_year, catno, discs)
    target_dir = dest_root / folder_artist / folder

    print(f"  dest  {target_dir}")
    validate_layout(album, dest_root, target_dir)

    pairs, legacy = match_tracks(album, dtracks)
    if legacy:
        print("  note  these files use the old vinyl numbering (B1 = 21); they are renumbered 1..N")

    by_disc: dict[int, list[DiscogsTrack]] = {}
    for dt in dtracks:
        by_disc.setdefault(dt.disc, []).append(dt)

    plan: list[PlannedFile] = []
    for local, dt in pairs:
        if dt is None:
            print(f"  skip  extra file  {local.name}  (left in place)")
            continue
        disc_tracks = by_disc.get(dt.disc, dtracks)
        meta = {
            "artist": dt.artist or album_artist,
            "albumartist": album_artist,
            "album": title,
            "title": dt.title,
            "track": dt.number,
            "tracktotal": len(disc_tracks),
            "disc": dt.disc,
            "disctotal": discs,
            "date": str(release.get("released") or release_year or ""),
            "originaldate": orig,
            "label": label,
            "catalognumber": format_catalog(catno) or "",
            "genre": genre,
            "release_id": release.get("id"),
            "master_id": master_id,
            "position": dt.position,
        }
        dest_file = target_dir / track_filename(track_prefix(dt), dt.title, local.suffix)
        where = f"{dt.disc}-{dt.number}"
        if dt.position and dt.position != str(dt.number):
            where += f", {dt.position}"
        print(f"  {local.name}")
        print(f"      -> {dest_file.name}   [{where}]")
        plan.append(PlannedFile(local, dest_file, meta))

    clash: dict[str, list[Path]] = {}
    for item in plan:
        clash.setdefault(str(item.dest).casefold(), []).append(item.source)
    clashes = {k: v for k, v in clash.items() if len(v) > 1}
    if clashes:
        detail = "; ".join(", ".join(p.name for p in v) for v in clashes.values())
        print(f"  warning: several files map to the same name and will get (2) suffixes: {detail}")

    # Generated album playlists list the old file names. They are not moved;
    # they are deleted and a fresh one is written once the move succeeds.
    old_playlists: list[Path] = []
    sidecars: list[Path] = []
    for sidecar in album.sidecars:
        if sidecar.suffix.lower() in mm_common.PLAYLIST_EXTS and mm_common.is_generated_playlist(sidecar):
            old_playlists.append(sidecar)
        else:
            sidecars.append(sidecar)
            if sidecar.suffix.lower() in mm_common.PLAYLIST_EXTS:
                print(f"  note  hand-made playlist {sidecar.name} is moved as-is; it may list old file names")

    # Phase 1: tags. Nothing moves unless every file was tagged.
    # A real run journals every change before doing it, so the whole apply can be undone.
    journal: Journal | None = None
    if not dry_run:
        journal = Journal("apply", journal_dir)
        journal.write(action="start", command="apply", album=str(album.path), dest=str(target_dir))
    tagged: list[Path] = []
    try:
        for item in plan:
            try:
                if journal is not None:
                    journal.write(action="tags", phase="before", path=str(item.source), snapshot=capture_tags(item.source))
                removed = write_tags(item.source, item.meta, clear_other=clear_other, dry_run=dry_run)
            except Exception as exc:  # noqa: BLE001 - any failure must stop this album only
                msg = f"tagging failed for {item.source}: {type(exc).__name__}: {exc}\n  nothing was moved"
                if tagged and not dry_run:
                    msg += f"; {len(tagged)} file(s) were already re-tagged in place:\n"
                    msg += "\n".join(f"    {p}" for p in tagged)
                raise AlbumApplyError(msg) from exc
            if journal is not None:
                journal.write(action="tags", phase="after", path=str(item.source), sha256=file_sha256(item.source))
            if removed:
                print(f"      {'would clear' if dry_run else 'cleared'}  {', '.join(removed)}  ({item.source.name})")
            tagged.append(item.source)

        moves: list[PlannedFile] = list(plan)
        for extra_dir in album.extra_dirs:
            print(f"  move  {extra_dir.name}/")
            moves.append(PlannedFile(extra_dir, target_dir / extra_dir.name))
        for sidecar in sidecars:
            rel = sidecar.relative_to(album.path)
            print(f"  move  {rel}")
            moves.append(PlannedFile(sidecar, target_dir / rel))
        for playlist in old_playlists:
            print(f"  drop  {playlist.relative_to(album.path)}  (generated playlist; regenerated after the move)")

        if dry_run:
            return []

        # Phase 2: moves.
        done: list[tuple[Path, Path]] = []
        try:
            _mkdirs_logged(journal, target_dir)
            move_planned(moves, done, journal)
        except Exception as exc:  # noqa: BLE001
            moved_sources = {src for src, _ in done}
            pending = [m for m in moves if m.source not in moved_sources]
            raise AlbumApplyError(
                f"move failed: {type(exc).__name__}: {exc}\n"
                f"  all files were tagged; the album is split between\n"
                f"    {album.path}\n    {target_dir}\n" + describe_moves(done, pending)
            ) from exc

    # Phase 3: playlists and cleanup.
        if old_playlists:
            regenerate_playlist(target_dir, old_playlists, journal)
        if album.path.resolve() != target_dir.resolve():
            limit = prune_limit(album.path, dest_root, scan_root)
            for gone in _prune_empty(album.path, limit, protect=[dest_root, target_dir], journal=journal):
                print(f"  removed empty folder  {gone}")
        return done
    finally:
        if journal is not None:
            journal.close()
            print(f"journal: {journal.path}")
            print(f"undo:    discogs-tag --undo-journal '{journal.path}'   (preview; add --apply to undo)")


def regenerate_playlist(target_dir: Path, old_playlists: list[Path], journal: Journal | None = None) -> None:
    """Generated playlists go to the trash (never unlink), then a fresh one is written."""
    for playlist in old_playlists:
        try:
            if playlist.exists():
                _trash_logged(journal, playlist)
        except OSError as exc:
            print(f"  warning: could not trash old playlist {playlist}: {exc}")
    dest = mm_common.playlist_path(target_dir)
    if dest.exists() and not mm_common.is_generated_playlist(dest):
        print(f"  warning: not replacing hand-made playlist {dest}")
        return
    tracks = mm_common.album_playlist_tracks(target_dir)
    if not tracks:
        return
    try:
        if journal is not None:
            journal.write(action="create-intent", path=str(dest))
        mm_common.write_playlist_file(dest, mm_common.render_playlist(target_dir, tracks))
        if journal is not None:
            journal.write(action="create", path=str(dest), sha256=file_sha256(dest))
        print(f"  playlist  {dest.name}  ({len(tracks)} tracks)")
    except OSError as exc:
        print(f"  warning: could not write playlist {dest}: {exc}")


def prune_limit(album_path: Path, dest_root: Path, scan_root: Path | None) -> Path:
    """Highest folder that empty-folder cleanup may remove after a move.

    - The folder passed on the command line (scan_root) when the album was
      found under it: that folder itself goes too if it ends up empty.
    - Otherwise, inside the library: the artist-level folder (never the
      library root).
    - Otherwise only the album folder itself.
    The library root and its parents, the album's new folder and its
    parents, the home folder and mount points are never removed (see
    _prune_empty).
    """
    album = album_path.resolve()
    if scan_root is not None:
        scan = scan_root.resolve()
        if album == scan or scan in album.parents:
            return scan
    root = dest_root.resolve()
    if root in album.parents:
        return root / album.relative_to(root).parts[0]
    return album


def _rmdir_logged(journal: Journal | None, current: Path) -> None:
    if journal is not None:
        journal.write(action="rmdir", path=str(current))
    os.rmdir(current)


def _remove_empty_tree(folder: Path, protected: set[Path], journal: Journal | None = None) -> list[Path]:
    """rmdir every truly empty folder inside folder, deepest first."""
    removed: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(folder, topdown=False, followlinks=False):
        current = Path(dirpath)
        if current == folder or filenames:
            continue
        if current.resolve() in protected or current.is_symlink() or os.path.ismount(current):
            continue
        try:
            _rmdir_logged(journal, current)
            removed.append(current)
        except OSError:
            pass
    return removed


def _prune_empty(start: Path, limit: Path, protect: list[Path] = (), journal: Journal | None = None) -> list[Path]:
    """Remove folders left empty under start, then start and its parents.

    Uses os.rmdir only, which refuses to remove a folder holding anything at
    all (hidden files included). Goes no higher than limit (limit itself may
    go). Never removes a protected folder or any parent of one, the home
    folder, or a mount point. Returns the removed folders.
    """
    limit = limit.resolve()
    protected: set[Path] = set()
    for path in [*protect, Path.home()]:
        try:
            path = path.resolve()
        except OSError:
            continue
        protected |= {path, *path.parents}
    removed: list[Path] = []
    if start.is_dir() and not start.is_symlink():
        removed += _remove_empty_tree(start, protected, journal)
    current = start
    while True:
        try:
            resolved = current.resolve()
        except OSError:
            return removed
        if resolved != limit and limit not in resolved.parents:
            return removed
        if resolved in protected or current.is_symlink() or not current.is_dir() or os.path.ismount(current):
            return removed
        try:
            _rmdir_logged(journal, current)
        except OSError:
            return removed  # not empty: stop here
        removed.append(current)
        if resolved == limit:
            return removed
        current = current.parent


# ---------------------------------------------------------------------------
# Library cleanup: --normalize-folders / --undo-journal
#
# Renames album folders made by older versions ("[1984]  My War [1984]
# [SST023]") to the current rule ("[1984]  My War [SST023]") without
# calling Discogs and without touching any file's tags. Preview by default;
# --apply renames and records every rename in a JSON-lines journal that
# --undo-journal can reverse.
# ---------------------------------------------------------------------------

STATE_DIR = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "discogs-tag"

# Exactly the shape this tool writes: "[Y1]  Title[ NCD] [Y2][ [CAT]]",
# two spaces after the leading bracket and single spaces elsewhere.
OLD_FOLDER = re.compile(
    r"^\[(?P<y1>\d{4}|)\]  (?P<title>[^\[\]]+?)(?P<discs> \d+CD)?"
    r"(?P<y2seg> \[(?P<y2>\d{4}|)\])(?P<cat> \[[^\[\]]+\])?$"
)
# Already in the current shape: "[Y]  Title[ NCD][ [CAT]]".
NEW_FOLDER = re.compile(r"^\[\d{4}\]  [^\[\]]+?(?: \d+CD)?(?: \[[^\[\]]+\])?$")


@dataclass
class FolderPlan:
    """Result of plan_folder_rename: action is "rename", "ok" or "skip"."""
    action: str
    new_name: str = ""
    reason: str = ""


def plan_folder_rename(name: str, tag_years: tuple[str, str] | None = None) -> FolderPlan:
    """Decide the new name of one album folder from its name alone.

    tag_years is (DATE year, ORIGINALDATE/ORIGINALYEAR year) read from the
    album's files, or None when not read. Pure function: no file access.
    """
    match = OLD_FOLDER.match(name)
    if not match:
        if NEW_FOLDER.match(name):
            return FolderPlan("ok")
        return FolderPlan("skip", reason="not in the '[YEAR]  Album [YEAR] [CATNO]' shape")
    y1, y2 = match.group("y1"), match.group("y2")
    cat = match.group("cat") or ""
    if YEAR_RE.fullmatch(cat.strip(" []")):
        return FolderPlan("skip", reason="catalog number looks like a year (ambiguous)")
    if y1 and y2 and y1 < y2:
        return FolderPlan("ok")  # a reissue: both years stay
    if y1 and y2 and y1 > y2:
        return FolderPlan("skip", reason=f"leading year {y1} is later than release year {y2}")
    if bool(y1) != bool(y2):
        return FolderPlan("skip", reason="only one of the two years is filled in")
    if y1 and tag_years:
        date, original = (year4(v) for v in tag_years)
        if date and original and original < date:
            return FolderPlan(
                "skip",
                reason=f"tags say reissue (original {original}, release {date}) but folder years are equal",
            )
    if y1:
        new = name[: match.start("y2seg")] + name[match.end("y2seg"):]
    else:
        # "[]  Album []" from an old version with no year at all.
        new = match.group("title") + (match.group("discs") or "") + cat
    if not new.strip() or new != sanitize_filename(new):
        return FolderPlan("skip", reason="new name would not be a clean folder name")
    return FolderPlan("rename", new_name=new)


DATE_KEYS = ("date", "year", "tdrc", "tyer", "©day", "wm/year")
ORIGINAL_KEYS = (
    "originaldate", "originalyear", "tdor", "tory",
    "txxx:originaldate", "txxx:originalyear",
    "----:com.apple.itunes:originaldate", "----:com.apple.itunes:originalyear",
    "wm/originalreleasetime", "wm/originalreleaseyear",
)


def _tag_text(value) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    if hasattr(value, "text"):  # ID3 frame
        value = value.text[0] if value.text else ""
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8", "replace")
    return str(value)


def read_tag_years(path: Path) -> tuple[str, str]:
    """(DATE year, original year) from one file, any format; "" when absent."""
    _audio, tags = _read_tag_file(path)
    if not tags:
        return "", ""
    values: dict[str, str] = {}
    try:
        for key in tags.keys():
            values.setdefault(str(key).casefold(), _tag_text(tags[key]))
    except Exception:  # noqa: BLE001 - odd tag types: treat as unknown
        return "", ""

    def first(keys: tuple[str, ...]) -> str:
        for key in keys:
            year = year4(values.get(key, ""))
            if year:
                return year
        return ""

    return first(DATE_KEYS), first(ORIGINAL_KEYS)


def album_tag_years(album: AlbumFolder) -> tuple[str, str] | None:
    """Years from the album's first track that has any, or None."""
    for track in album.tracks:
        date, original = read_tag_years(track)
        if date or original:
            return date, original
    return None


@dataclass
class FolderRename:
    old: Path
    new: Path
    playlists: list[tuple[Path, Path]] = field(default_factory=list)


def plan_library_normalization(root: Path) -> tuple[list[FolderRename], list[tuple[Path, str]], int]:
    """(renames, skipped with reasons, number already fine) for a library."""
    renames: list[FolderRename] = []
    skipped: list[tuple[Path, str]] = []
    fine = 0
    for album in collect_albums(root):
        name = album.path.name
        plan = plan_folder_rename(name)
        if plan.action == "rename" and OLD_FOLDER.match(name).group("y1"):
            plan = plan_folder_rename(name, album_tag_years(album))
        if plan.action == "ok":
            fine += 1
            continue
        if plan.action == "skip":
            skipped.append((album.path, plan.reason))
            continue
        new_dir = album.path.with_name(plan.new_name)
        playlists = []
        for playlist in mm_common.playlists_in(album.path):
            if playlist.stem == name and mm_common.is_generated_playlist(playlist):
                playlists.append((new_dir / playlist.name, new_dir / f"{plan.new_name}{playlist.suffix}"))
        renames.append(FolderRename(album.path, new_dir, playlists))

    # Never merge or overwrite: an existing target, or two folders that
    # would get the same name, are conflicts and are left alone. (The
    # existence check follows the drive's own case rules.)
    targets: dict[str, int] = {}
    for item in renames:
        targets[str(item.new)] = targets.get(str(item.new), 0) + 1
    ok: list[FolderRename] = []
    for item in renames:
        if os.path.lexists(item.new):
            skipped.append((item.old, f"conflict: {item.new.name!r} already exists"))
        elif targets[str(item.new)] > 1:
            skipped.append((item.old, f"conflict: another folder would also become {item.new.name!r}"))
        else:
            ok.append(item)
    return ok, skipped, fine


class Journal:
    """Append-only JSON-lines record of renames, flushed and fsynced per line."""

    def __init__(self, kind: str, directory: Path | None = None):
        directory = directory or STATE_DIR / "journal"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for n in range(1000):
            suffix = f"-{n}" if n else ""
            path = directory / f"{kind}-{stamp}{suffix}.jsonl"
            try:
                self._fh = open(path, "x", encoding="utf-8")  # noqa: SIM115
            except FileExistsError:
                continue
            self.path = path
            break
        else:
            raise OSError(f"cannot create a journal in {directory}")

    def write(self, **entry) -> None:
        entry = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **entry}
        self._fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def close(self) -> None:
        self._fh.close()


def _rename_logged(journal: Journal, kind: str, old: Path, new: Path) -> None:
    if os.path.lexists(new):
        raise FileExistsError(f"{new} already exists")
    os.rename(old, new)
    st = os.lstat(new)
    journal.write(action="rename", kind=kind, old=str(old), new=str(new), dev=st.st_dev, ino=st.st_ino)


def normalize_folders(root: Path, apply: bool, journal_dir: Path | None = None) -> int:
    root = root.resolve()
    if not root.is_dir():
        print(f"not a folder: {root}", file=sys.stderr)
        return 2
    renames, skipped, fine = plan_library_normalization(root)
    mode = "APPLY" if apply else "PREVIEW (nothing is renamed; add --apply to rename)"
    print(f"normalize album folders under {root}\n{mode}\n")
    for item in renames:
        print(f"  {item.old}\n    -> {item.new.name}")
        for old_pl, new_pl in item.playlists:
            print(f"       playlist {old_pl.name} -> {new_pl.name}")
    if skipped:
        print("\nskipped (left as they are):")
        for path, reason in skipped:
            print(f"  {path}\n    {reason}")
    conflicts = sum(1 for _, reason in skipped if reason.startswith("conflict"))
    print(
        f"\n{len(renames)} to rename, {fine} already fine, "
        f"{len(skipped) - conflicts} skipped, {conflicts} conflicts"
    )
    if not apply or not renames:
        return 0

    journal = Journal("normalize-folders", journal_dir)
    journal.write(action="start", command="normalize-folders", root=str(root))
    done = failed = 0
    try:
        for item in renames:
            try:
                _rename_logged(journal, "folder", item.old, item.new)
            except OSError as exc:
                failed += 1
                print(f"  FAILED {item.old}: {exc}")
                continue
            done += 1
            for old_pl, new_pl in item.playlists:
                try:
                    _rename_logged(journal, "playlist", old_pl, new_pl)
                except OSError as exc:
                    print(f"  warning: playlist not renamed: {old_pl}: {exc}")
    finally:
        journal.close()
    print(f"\nrenamed {done} folders, {failed} failed")
    print(f"journal: {journal.path}")
    print(f"undo:    discogs-tag --undo-journal '{journal.path}' --apply")
    return 1 if failed else 0


class _UndoView:
    """The file system as undo sees it.

    Live mode looks at the disk. Preview mode layers the effect of every
    step already listed on top, so later steps are checked as they would be
    when they run (a file moved back frees its new name, a recreated folder
    exists, and so on). Nothing on disk is changed in either mode.
    """

    _DIR = "<dir>"

    def __init__(self, simulate: bool):
        self.simulate = simulate
        self.over: dict[str, str | None] = {}
        self._hashes: dict[str, str | None] = {}

    def exists(self, path: Path) -> bool:
        key = str(path)
        if key in self.over:
            return self.over[key] is not None
        return os.path.lexists(path)

    def is_dir(self, path: Path) -> bool:
        key = str(path)
        if key in self.over:
            real = self.over[key]
            return real == self._DIR or (real is not None and os.path.isdir(real))
        return path.is_dir() and not path.is_symlink()

    def is_empty_dir(self, path: Path) -> bool:
        if not self.is_dir(path):
            return False
        names: set[str] = set()
        real = self.over.get(str(path), str(path))
        if real not in (None, self._DIR):
            try:
                names = set(os.listdir(real))
            except OSError:
                names = set()
        for key, value in self.over.items():
            parent, _, name = key.rpartition("/")
            if parent == str(path):
                if value is None:
                    names.discard(name)
                else:
                    names.add(name)
        return not names

    def sha256(self, path: Path) -> str | None:
        real = self.over.get(str(path), str(path))
        if real in (None, self._DIR):
            return None
        if not self.simulate:
            return file_sha256(Path(real))
        if real not in self._hashes:
            self._hashes[real] = file_sha256(Path(real))
        return self._hashes[real]

    def moved(self, src: Path, dest: Path) -> None:
        if self.simulate:
            self.over[str(dest)] = self.over.get(str(src), str(src))
            self.over[str(src)] = None

    def made_dir(self, path: Path) -> None:
        if self.simulate:
            self.over[str(path)] = self._DIR

    def removed(self, path: Path) -> None:
        if self.simulate:
            self.over[str(path)] = None


@dataclass
class UndoStep:
    """One reversing action. check() says why it can't run ("" when it can)."""

    label: str
    check: object
    run: object
    simulate: object


def _move_back(journal: Journal, current: Path, original: Path) -> None:
    original.parent.mkdir(parents=True, exist_ok=True)
    if current.is_dir() and not current.is_symlink() and original.is_dir() and not original.is_symlink():
        _merge_back(journal, current, original)
        return
    _journal_rename(journal, "undo", current, original)
    try:
        os.rename(current, original)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        _move_across_devices(current, original, journal)


def _merge_back(journal: Journal, current: Path, original: Path) -> None:
    """Move a folder's contents back into a source folder that still exists.

    Never overwrites: a name already taken in original is left in current and
    reported (current is then kept).
    """
    blocked: list[str] = []
    for entry in sorted(current.iterdir()):
        target = original / entry.name
        if os.path.lexists(target):
            if entry.is_dir() and not entry.is_symlink() and target.is_dir() and not target.is_symlink():
                try:
                    _merge_back(journal, entry, target)
                    continue
                except OSError:
                    pass
            blocked.append(entry.name)
            continue
        _move_back(journal, entry, target)
    if blocked:
        raise OSError(errno.EEXIST, f"already exist in {original}, left in {current}: {', '.join(blocked)}")
    _rmdir_logged(journal, current)


def undo_plan(entries: list[dict]) -> list[UndoStep]:
    """Reversing steps for a journal's entries, newest first.

    Every step re-checks the disk before it runs: something that changed
    since the journal was written (a file edited, moved, or replaced, or a
    name that is taken again) is skipped and reported, never overwritten.
    """
    after_hash: dict[int, str | None] = {}
    pending_before: dict[str, int] = {}
    for i, entry in enumerate(entries):
        if entry.get("action") != "tags":
            continue
        if entry.get("phase") == "before":
            pending_before[entry.get("path", "")] = i
        elif entry.get("phase") == "after" and entry.get("path") in pending_before:
            after_hash[pending_before.pop(entry["path"])] = entry.get("sha256")

    steps: list[UndoStep] = []
    for i in range(len(entries) - 1, -1, -1):
        entry = entries[i]
        action = entry.get("action")

        if action == "rename":
            current, original = Path(entry["new"]), Path(entry["old"])

            is_dir = entry.get("kind") == "dir"

            def check(view, e=entry, current=current, original=original, is_dir=is_dir):
                if not view.exists(current):
                    if is_dir and view.is_dir(original):
                        return ""  # moved back child by child (cross-device move)
                    return "no longer exists"
                if e.get("sha256"):
                    if view.sha256(current) != e["sha256"]:
                        return "changed since (contents differ)"
                elif "link" in e and str(current) not in view.over:
                    try:
                        if os.readlink(current) != e["link"]:
                            return "is not the link that was moved (changed since)"
                    except OSError:
                        return "is not the link that was moved (changed since)"
                elif is_dir and str(current) not in view.over:
                    # A cross-device folder move recreates it (new inode); its contents
                    # are journaled one by one, and merging back never overwrites.
                    if not view.is_dir(current):
                        return "is no longer a folder"
                elif str(current) not in view.over:
                    try:
                        st = os.lstat(current)
                    except OSError:
                        return "no longer exists"
                    if st.st_dev == e.get("dev") and st.st_ino != e.get("ino"):
                        return "is not the item that was renamed (changed since)"
                if view.exists(original):
                    if is_dir and view.is_dir(original) and view.is_dir(current):
                        return ""  # partial cross-device move: merge back into it
                    return f"{original.name!r} exists again"
                return ""

            def run(j, c=current, o=original):
                if os.path.lexists(c):
                    _move_back(j, c, o)

            def simulate(view, c=current, o=original):
                if view.exists(c) and not view.exists(o):
                    view.moved(c, o)
                elif view.exists(c):
                    view.removed(c)

            steps.append(UndoStep(f"move back  {current}\n    -> {original}", check, run, simulate))

        elif action == "mkdir":
            folder = Path(entry["path"])
            steps.append(UndoStep(
                f"remove the folder it created  {folder}",
                lambda view, f=folder: "" if view.is_empty_dir(f) else ("no longer exists" if not view.exists(f) else "is not empty"),
                lambda j, f=folder: _rmdir_logged(j, f),
                lambda view, f=folder: view.removed(f),
            ))

        elif action == "rmdir":
            folder = Path(entry["path"])
            steps.append(UndoStep(
                f"recreate empty folder  {folder}",
                lambda view, f=folder: "" if not view.exists(f) else ("" if view.is_dir(f) else "a file now has that name"),
                lambda j, f=folder: _mkdirs_logged(j, f),
                lambda view, f=folder: view.made_dir(f),
            ))

        elif action == "trashed":
            original, trashed = Path(entry["path"]), Path(entry["trashed"])

            def check(view, e=entry, original=original, trashed=trashed):
                if not view.exists(trashed):
                    return "is no longer in the trash"
                if e.get("sha256") and view.sha256(trashed) != e["sha256"]:
                    return "the trashed copy changed"
                if view.exists(original):
                    return f"{original.name!r} exists again"
                return ""

            def run(j, original=original, trashed=trashed):
                _move_back(j, trashed, original)
                info = trashed.parent.parent / "info" / f"{trashed.name}.trashinfo"
                if info.is_file():
                    info.unlink()

            steps.append(UndoStep(
                f"restore from trash  {original}", check, run,
                lambda view, o=original, t=trashed: view.moved(t, o),
            ))

        elif action == "create":
            created = Path(entry["path"])
            steps.append(UndoStep(
                f"trash the playlist it wrote  {created}",
                lambda view, f=created, h=entry.get("sha256"): (
                    "no longer exists" if not view.exists(f)
                    else "changed since (contents differ)" if view.sha256(f) != h else ""),
                lambda j, f=created: _trash_logged(j, f),
                lambda view, f=created: view.removed(f),
            ))

        elif action == "tags" and entry.get("phase") == "before":
            path = Path(entry["path"])
            expected = after_hash.get(i)
            finished = i in after_hash

            def check(view, path=path, expected=expected, finished=finished):
                if not view.exists(path):
                    return "is not back in its old place"
                if finished and view.sha256(path) != expected:
                    return "changed since it was tagged"
                return ""

            def run(j, path=path, snapshot=entry.get("snapshot") or {"kind": "none"}):
                j.write(action="tags", phase="before", path=str(path), snapshot=capture_tags(path))
                restore_tags(path, snapshot)
                j.write(action="tags", phase="after", path=str(path), sha256=file_sha256(path))

            label = f"restore the original tags  {path}"
            if not finished:
                label += "  (the tag write never finished)"
            steps.append(UndoStep(label, check, run, lambda view: None))

    return steps


def undo_journal(journal_path: Path, apply: bool, journal_dir: Path | None = None) -> int:
    """Reverse everything a journal recorded, newest first.

    Without apply this is the undo plan: every step that would run and every
    step that would be skipped, with nothing changed on disk.
    """
    entries = []
    with open(journal_path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"  warning: line {n} of the journal is not valid JSON; ignored")
    steps = undo_plan(entries)
    mode = "APPLY" if apply else "PREVIEW (nothing is changed; add --apply to undo)"
    print(f"undo {journal_path}\n{mode}\n")

    skipped: list[tuple[str, str]] = []
    if not apply:
        view = _UndoView(simulate=True)
        ready = 0
        for step in steps:
            reason = step.check(view)
            if reason:
                skipped.append((step.label, reason))
                continue
            step.simulate(view)
            ready += 1
            print(f"  {step.label}")
        for label, reason in skipped:
            print(f"  would skip  {label}: {reason}")
        print(f"\n{ready} changes would be reversed, {len(skipped)} skipped")
        return 0

    view = _UndoView(simulate=False)
    journal = Journal("undo", journal_dir)
    journal.write(action="start", command="undo-journal", journal=str(journal_path))
    done = 0
    try:
        for step in steps:
            reason = step.check(view)
            if not reason:
                try:
                    step.run(journal)
                    print(f"  {step.label}")
                    done += 1
                    continue
                except OSError as exc:
                    reason = str(exc)
            skipped.append((step.label, reason))
    finally:
        journal.close()
    for label, reason in skipped:
        print(f"  skipped  {label}: {reason}")
    print(f"\nreversed {done} changes, {len(skipped)} skipped")
    print(f"journal of this undo: {journal.path}")
    return 1 if skipped else 0


# ---------------------------------------------------------------------------
# Interactive flow
# ---------------------------------------------------------------------------

def print_candidates(results: list[tuple[float, dict]]) -> None:
    for i, (score, item) in enumerate(results, 1):
        artist, album = split_result_title(item.get("title") or "")
        fmt = ", ".join(item.get("format") or [])
        print(
            f"  {i:2}. {int(score * 100):3}%  {artist} – {album}  "
            f"[{item.get('year') or '?'}]  {item.get('catno') or '-'}  "
            f"{item.get('country') or ''}  {fmt}"
        )
        print(f"       https://www.discogs.com/release/{item.get('id')}")


def prompt(text: str) -> str:
    try:
        return input(text).strip()
    except EOFError:
        return "q"


def choose_release(
    api: Discogs,
    guess: dict[str, str],
    results: list[dict],
    auto: bool,
    release_id: int | None,
) -> dict | None:
    if release_id:
        return api.release(release_id)

    ranked = sorted(((score_result(r, guess), r) for r in results), key=lambda x: -x[0])
    if auto and not ranked:
        print("  no search results; nothing to auto-match")
        return None
    if auto and ranked:
        top = ranked[0][0]
        second = ranked[1][0] if len(ranked) > 1 else 0.0
        print(f"  best  {top:.2f}  [{format_score_components(score_components(ranked[0][1], guess))}]  "
              f"{ranked[0][1].get('title')}")
        if len(ranked) > 1:
            print(f"  next  {second:.2f}  [{format_score_components(score_components(ranked[1][1], guess))}]  "
                  f"{ranked[1][1].get('title')}")
        if top >= AUTO_MIN_SCORE and top - second >= AUTO_MIN_MARGIN:
            print(f"  auto  {int(top * 100)}%  margin {top - second:.2f}  {ranked[0][1].get('title')}")
            return api.release(int(ranked[0][1]["id"]))
        print(f"  no confident auto-match (need score >= {AUTO_MIN_SCORE:.2f} and margin >= {AUTO_MIN_MARGIN:.2f})")
        return None

    if not sys.stdin.isatty():
        raise DiscogsError("No TTY. Pass --release-id or --auto.")

    while True:
        if ranked:
            print_candidates(ranked[:12])
        else:
            print("  (no results)")
        print("  [1-N] choose   s skip   /query   i <id or url>   q quit")
        choice = prompt("> ")
        if not choice or choice.lower() == "s":
            return None
        if choice.lower() in {"q", "quit"}:
            raise KeyboardInterrupt
        if choice.startswith("/"):
            query = choice[1:].strip()
            if " - " in query:
                artist, album = query.split(" - ", 1)
            else:
                artist, album = guess.get("artist", ""), query
            results = api.search_releases(artist.strip(), album.strip(), guess.get("catno", ""), guess.get("year", ""))
            ranked = sorted(((score_result(r, guess), r) for r in results), key=lambda x: -x[0])
            continue
        if choice.lower().startswith("i"):
            raw = choice[1:].strip()
            match = RELEASE_URL.search(raw) or re.fullmatch(r"\d+", raw)
            if not match:
                print("  need a release id or discogs.com/release/… URL")
                continue
            return api.release(int(match.group(1) if match.lastindex else match.group(0)))
        if choice.isdigit() and 1 <= int(choice) <= len(ranked):
            return api.release(int(ranked[int(choice) - 1][1]["id"]))
        print("  not understood")


def process_album(
    api: Discogs,
    album: AlbumFolder,
    dest_root: Path,
    dry_run: bool,
    auto: bool,
    yes: bool,
    skip_tagged: bool,
    release_id: int | None,
    clear_other: bool = False,
    scan_root: Path | None = None,
    allow_track_mismatch: bool = False,
) -> str:
    """Returns "applied", "skipped", or "skipped-tagged" (not counted by --limit).

    In --auto mode a release whose track count differs from the local files
    is not applied unless allow_track_mismatch is set.
    """
    if skip_tagged:
        tagged = album_release_ids(album.tracks)
        ids = set(tagged.values())
        if len(ids) == 1 and None not in ids:
            print(f"skip tagged (Discogs {ids.pop()}): {album.path}")
            return "skipped-tagged"
        if ids - {None}:
            untagged = sum(1 for v in tagged.values() if v is None)
            found = ", ".join(sorted(i for i in ids if i))
            print(f"mixed tags: {album.path}: release id(s) {found}; {untagged} of {len(tagged)} file(s) untagged"
                  " - not skipped")

    guess = guess_from_album_dir(album.path, album.tracks)
    print()
    print("=" * 72)
    print(f"folder  {album.path}")
    print(f"guess   {guess['artist']}  –  {guess['album']}  [{guess.get('year') or '?'}]  {guess.get('catno') or ''}")
    print(f"tracks  {len(album.tracks)}")

    results = []
    if not release_id:
        results = api.search_releases(guess["artist"], guess["album"], guess.get("catno", ""), guess.get("year", ""))
        if not results and guess["album"]:
            results = api.search_releases("", guess["album"], guess.get("catno", ""), guess.get("year", ""))

    try:
        release = choose_release(api, guess, results, auto=auto, release_id=release_id)
    except KeyboardInterrupt:
        raise
    if release is None:
        print("  skipped")
        return "skipped"

    album_artist = release_artists(release)
    dtracks = flatten_tracklist(release, album_artist)
    release_year = year4(release.get("released") or release.get("year"))
    original_year = ""  # the master's year; only used when it is earlier (reissue)
    master_id = release.get("master_id")
    if master_id:
        try:
            master = api.master(int(master_id))
            original_year = year4(master.get("year"))
        except DiscogsError as exc:
            print(f"  warning: master fetch failed: {exc}")

    reissue_of = reissue_original_year(original_year, release_year)
    print(f"  match  {album_artist} – {release.get('title')}  [{release.get('year')}]")
    print(f"         orig {reissue_of + ' (reissue)' if reissue_of else '- (original release)'}  "
          f"cat {release_catno(release) or '-'}  "
          f"{len(dtracks)} tracks / {release_discs(release, dtracks)} disc(s)")
    print(f"         https://www.discogs.com/release/{release.get('id')}")

    if len(dtracks) != len(album.tracks):
        print(f"  warning: {len(album.tracks)} local files vs {len(dtracks)} Discogs tracks")
        if auto and not release_id and not allow_track_mismatch:
            print("  skipped: track count mismatch in --auto mode (use --allow-track-mismatch to apply anyway)")
            return "skipped"

    if not yes and sys.stdin.isatty() and not dry_run:
        ans = prompt("  apply tags + rename? [Y/n] ").lower()
        if ans in {"n", "no"}:
            return "skipped"

    apply_album(
        album, dest_root, release, dtracks, original_year,
        dry_run=dry_run, clear_other=clear_other, scan_root=scan_root,
    )
    return "applied"


def self_test() -> int:
    assert swapprefix("The Beatles") == "Beatles, The"
    assert swapprefix("Die Ärzte") == "Ärzte, Die"
    assert format_artist("The Grouch") == "Grouch, The"
    assert format_artist("Aesop Rock (2)") == "Aesop Rock"
    assert format_artist("Various") == "Various Artists"
    assert format_artist("X meets Y") == "X & Y"
    assert format_album_title('Foo: "Bar"') == "Foo- Bar"
    assert format_catalog("BTR149; other") == "BTR149"
    assert format_catalog("none") is None
    name = album_folder_name("No Cause for Concern", "1981", "1993", "DOJO CD 167", 1)
    assert name == "[1981]  No Cause for Concern [1993] [DOJOCD167]", name
    # Release-year bracket only for reissues; unknown parts left out entirely.
    for args, want in (
        (("Come On Die Young", "1999", "1999", "OLE365-2", 1), "[1999]  Come On Die Young [OLE365-2]"),
        (("Mermaid Avenue", "1998", "1998", "7559-62204-1", 2), "[1998]  Mermaid Avenue 2CD [7559-62204-1]"),
        (("Mermaid Avenue", "1990", "1998", "7559-62204-1", 2), "[1990]  Mermaid Avenue 2CD [1998] [7559-62204-1]"),
        (("My War", "", "1984", "SST 023", 1), "[1984]  My War [SST023]"),
        (("Nervous Breakdown", "1979", "1988", "SST CD 001", 1), "[1979]  Nervous Breakdown [1988] [SSTCD001]"),
        (("The First Four Years", "1983", "1990", None, 1), "[1983]  The First Four Years [1990]"),
        (("What The…", "2013", "2013", "", 1), "[2013]  What The…"),
        (("My War", "", "", "SST 023", 1), "My War [SST023]"),
        (("My War", "", "", None, 1), "My War"),
    ):
        assert album_folder_name(*args) == want, (args, album_folder_name(*args))
    # Library cleanup (--normalize-folders): pure planning on names.
    for name, want in (
        ("[1984]  My War [1984] [SST023]", "[1984]  My War [SST023]"),
        ("[2013]  What The… [2013]", "[2013]  What The…"),
        ("[1998]  Mermaid Avenue 2CD [1998] [7559-62204-1]", "[1998]  Mermaid Avenue 2CD [7559-62204-1]"),
    ):
        plan = plan_folder_rename(name)
        assert (plan.action, plan.new_name) == ("rename", want), (name, plan)
    assert plan_folder_rename("[1979]  Nervous Breakdown [1988] [SSTCD001]").action == "ok"
    assert plan_folder_rename("[1984]  My War [SST023]").action == "ok"
    assert plan_folder_rename("[1991]  Black Sabbath [TECP-23892] [1970]").action == "skip"
    assert plan_folder_rename("[1985]  X [1985] [C1]", ("2016", "1985")).action == "skip"
    # Tags: original date only for reissues.
    assert reissue_original_year("1981", "1993") == "1981"
    assert reissue_original_year("1999", "1999") == "" and reissue_original_year("", "1999") == ""
    assert reissue_original_year("2001", "1999") == "" and reissue_original_year("1981", "") == ""
    assert folder_lead_year("1984", "1984") == "1984" and folder_lead_year("", "1984") == "1984"
    assert folder_lead_year("2001", "1999") == "1999" and folder_lead_year("1981", "") == "1981"
    parsed = parse_album_dirname("[1981]  No Cause for Concern [1993] [DOJOCD167]")
    assert parsed["album"] == "No Cause for Concern"
    assert parsed["originalyear"] == "1981"
    assert parsed["year"] == "1993"
    assert parsed["catno"] == "DOJOCD167"
    messy = parse_album_dirname("Doze - Iron & Rhymes (Aesop Rock + Iron & Wine) (2012) FLAC WEB")
    assert messy.get("artist") == "Doze"
    assert "Iron & Rhymes" in messy.get("album", "")
    assert parse_position("A1")[0] == 1
    assert parse_position("1-3") == (1, 3)
    assert parse_position("CD2-4") == (2, 4)

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tagged = Path(tmp) / "tagged.mp3"
        tags = ID3()
        tags.add(TXXX(encoding=3, desc="DISCOGS_RELEASE_ID", text="12345"))
        tags["TIT2"] = TIT2(encoding=3, text="Song")
        tags["TPE1"] = TPE1(encoding=3, text="Artist")
        tags.save(tagged)
        assert already_tagged(tagged) == "12345"
        assert read_easy_tags(tagged).get("title") == "Song"
        assert read_easy_tags(tagged).get("artist") == "Artist"

        bare = Path(tmp) / "bare.mp3"
        bare.write_bytes(b"not an mpeg frame")
        assert already_tagged(bare) is None
        assert read_easy_tags(bare) == {}
        write_tags(
            bare,
            {
                "title": "Song",
                "artist": "Artist",
                "albumartist": "Artist",
                "album": "Album",
                "track": 1,
                "tracktotal": 1,
                "disc": 1,
                "disctotal": 1,
                "date": "1999",
                "originaldate": "1999",
                "label": "",
                "catalognumber": "",
                "genre": "",
                "release_id": 99,
                "master_id": 0,
            },
        )
        assert already_tagged(bare) == "99"

        album_dir = Path(tmp) / "Artist - Album"
        scans = album_dir / "Scans"
        scans.mkdir(parents=True)
        track = album_dir / "1. Song.flac"
        track.write_bytes(b"flac")
        (album_dir / "cover.jpg").write_bytes(b"jpg")
        (album_dir / "notes.txt").write_bytes(b"nfo")
        (scans / "front.jpg").write_bytes(b"scan")
        leftover = album_dir / "CD1"
        leftover.mkdir()
        (leftover / "2. Other.flac").write_bytes(b"flac")
        (leftover / "rip.log").write_bytes(b"log")
        files, dirs = collect_extras(album_dir, [track, leftover / "2. Other.flac"])
        assert {p.name for p in dirs} == {"Scans"}
        names = {str(p.relative_to(album_dir)) for p in files}
        assert names == {"cover.jpg", "notes.txt", "CD1/rip.log"}

    _self_test_fixes()

    with tempfile.TemporaryDirectory() as tmp:
        lib = Path(tmp) / "Library"
        lib.mkdir()
        assert default_dest(Path(tmp) / "Artist - Album", library=lib) == lib.resolve()
        featuring = lib / "Aesop Rock" / "Featuring" / "Some Album"
        assert default_dest(featuring, library=lib) == (lib / "Aesop Rock" / "Featuring").resolve()

    print("self-test ok")
    return 0


def _tiny_wav(path: Path) -> None:
    import wave

    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * 8)


def _tiny_aiff(path: Path) -> None:
    import struct

    frames = 8
    rate_8000 = b"\x40\x0b\xfa\x00\x00\x00\x00\x00\x00\x00"  # 80-bit float 8000.0
    comm = struct.pack(">hLh", 1, frames, 16) + rate_8000
    ssnd = struct.pack(">LL", 0, 0) + b"\x00\x00" * frames
    body = b"AIFF" + b"COMM" + struct.pack(">L", len(comm)) + comm + b"SSND" + struct.pack(">L", len(ssnd)) + ssnd
    path.write_bytes(b"FORM" + struct.pack(">L", len(body)) + body)


def _self_test_fixes() -> None:
    """Checks for the tagging/matching/playlist fixes. No network, no samples."""
    import stat
    import tempfile

    def track(pos, title, kind="track", **extra):
        return {"position": pos, "type_": kind, "title": title, **extra}

    # Vinyl: sequential across sides, original position kept.
    lp = {"tracklist": [track("A1", "a"), track("A2", "b"), track("", "Side B", "heading"), track("B1", "c"), track("B2", "d")]}
    tracks = flatten_tracklist(lp, "X")
    assert [(t.number, t.position) for t in tracks] == [(1, "A1"), (2, "A2"), (3, "B1"), (4, "B2")], tracks
    assert tracks[2].legacy_number == 21
    assert tracks[2].side == "B1"
    assert track_filename(track_prefix(tracks[2], side_names=True), "c", ".flac") == "B1. c.flac"
    assert track_filename(track_prefix(tracks[2], side_names=False), "c", ".flac") == "3. c.flac"

    # Index tracks: sub-tracks included, numbered in order.
    work = {"tracklist": [
        track("1", "Intro"),
        track("", "Suite", "index", sub_tracks=[track("2a", "Part 1"), track("2b", "Part 2")]),
        track("3", "Outro"),
    ]}
    assert [(t.number, t.title) for t in flatten_tracklist(work, "X")] == [
        (1, "Intro"), (2, "Suite: Part 1"), (3, "Suite: Part 2"), (4, "Outro")]

    # Unicode similarity.
    assert similar("荒井由実", "荒井由実") == 1.0
    assert similar("Кино", "кино") == 1.0
    assert similar("Björk", "Bjork") == 1.0
    assert similar("ひこうき雲", "ミスリム") < 0.5
    assert rank_by_year([{"year": "2000"}, {"year": "1990"}], "1990")[0]["year"] == "1990"
    assert clean_date("1999-00-00") == "1999" and clean_date("1999-05-00") == "1999-05"

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        # Folder skip: "Art"/"Covers" are artwork only inside an album folder.
        for rel in ("Art/Album/1. a.flac", "Artist/Covers/1. b.flac", "Artist/Album/1. c.flac",
                    "Artist/Album/Scans/1. not-a-track.flac"):
            (root / "walk" / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / "walk" / rel).write_bytes(b"x")
        found = sorted(str(a.path.relative_to(root / "walk")) for a in collect_albums(root / "walk"))
        assert found == ["Art/Album", "Artist/Album", "Artist/Covers"], found

        # Playlists: generated vs hand-made.
        album = root / "pl" / "Album"
        album.mkdir(parents=True)
        (album / "1. a.flac").write_bytes(b"x")
        (album / "2. b.flac").write_bytes(b"x")
        legacy = album / "Album.m3u"
        legacy.write_text("1. a.flac\n2. b.flac\n", encoding="utf-8")
        assert mm_common.is_generated_playlist(legacy)
        mine = album / "mine.m3u"
        mine.write_text("1. a.flac\n2. b.flac\n", encoding="utf-8")
        assert not mm_common.is_generated_playlist(mine)
        legacy.write_text("2. b.flac\n1. a.flac\n", encoding="utf-8")
        assert not mm_common.is_generated_playlist(legacy)
        lines = mm_common.render_playlist(album, mm_common.album_playlist_tracks(album))
        assert lines[:2] == ["#EXTM3U", mm_common.GENERATED_MARKER]
        marked = album / "x.m3u8"
        marked.write_bytes(mm_common.encode_lines(lines))
        assert mm_common.is_generated_playlist(marked)

        # Tag writing: ID3v2.4 in WAV and AIFF, ID3 in MP3, nothing Vorbis-style.
        meta = {
            "title": "Ünïcode", "artist": "A", "albumartist": "A", "album": "B", "track": 2, "tracktotal": 9,
            "disc": 1, "disctotal": 1, "date": "2001-00-00", "originaldate": "1990", "label": "L",
            "catalognumber": "C1", "genre": "G", "release_id": 77, "master_id": 0, "position": "A2",
        }
        for name, make in (("t.wav", _tiny_wav), ("t.aiff", _tiny_aiff)):
            path = root / name
            make(path)
            write_tags(path, meta)
            tags = MutagenFile(path).tags
            assert isinstance(tags, ID3) and tags.version[:2] == (2, 4), name
            assert str(tags["TIT2"]) == "Ünïcode" and str(tags["TRCK"]) == "2/9" and str(tags["TDRC"]) == "2001"
            assert already_tagged(path) == "77"
        mp3 = root / "t.mp3"
        mp3.write_bytes(b"")
        old = ID3()
        old.add(TXXX(encoding=3, desc="DISCOGS_MASTER_ID", text="5"))
        old.add(TXXX(encoding=3, desc="REPLAYGAIN_TRACK_GAIN", text="-1 dB"))
        old.save(mp3)
        write_tags(mp3, meta)
        tags = ID3(mp3)
        assert "TXXX:DISCOGS_MASTER_ID" not in tags  # stale id from another release
        assert "TXXX:REPLAYGAIN_TRACK_GAIN" in tags  # never touched by default
        assert str(tags["TXXX:DISCOGS_POSITION"]) == "A2"

        # Token file is private from the start.
        token = root / "cfg" / "token"
        save_token("abc", token)
        assert stat.S_IMODE(token.stat().st_mode) == 0o600 and token.read_text() == "abc\n"

        # Empty-folder cleanup: only truly empty folders, never past the limit.
        arg = root / "bf-test" / "Black Flag"
        (arg / "Album" / "CD1").mkdir(parents=True)
        (arg / "Kept").mkdir()
        (arg / "Kept" / ".hidden").write_text("x")
        _prune_empty(arg / "Album", arg)
        assert not (arg / "Album").exists() and (arg / "Kept" / ".hidden").exists()
        (arg / "Kept" / ".hidden").unlink()
        _prune_empty(arg / "Kept", arg)
        assert not arg.exists() and (root / "bf-test").is_dir()

        # Cross-drive move: verified copy, then the source is deleted.
        src = root / "xdev" / "a.flac"
        src.parent.mkdir()
        src.write_bytes(b"audio" * 1000)
        dest = root / "xdev-out" / "a.flac"
        dest.parent.mkdir()
        _move_across_devices(src, dest)
        assert not src.exists() and dest.read_bytes() == b"audio" * 1000


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="discogs-tag",
        description="Tag and rename albums from Discogs using this library's folder rules.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  discogs-tag --save-token
  discogs-tag --dry-run "/path/to/album"
  discogs-tag --auto --yes "/path/to/Featuring"
  discogs-tag --release-id 249504 --dest "$DEFAULT_MUSIC" "/path/to/album"
  discogs-tag --normalize-folders "/path/to/Music"           (preview)
  discogs-tag --normalize-folders --apply "/path/to/Music"
  discogs-tag --undo-journal ~/.local/state/discogs-tag/journal/FILE.jsonl --apply

credentials:
  Create a personal token at
  https://www.discogs.com/settings/developers
  then run --save-token. The token is stored at
  ~/.config/discogs-tag/token (mode 600).
  DISCOGS_TOKEN overrides that file.
  DISCOGS_KEY and DISCOGS_SECRET are the consumer-key alternative.

where files go:
  DESTINATION/AlbumArtist/[LEADYEAR]  Album [RELEASEYEAR] [CATNO]/N. Title.ext
  LEADYEAR is the master's year for a reissue (master year earlier than
  the release year), otherwise the release's own year, so albums sort
  chronologically. [RELEASEYEAR] and the ORIGINALDATE tags only appear for
  a reissue. Unknown parts are left out with their brackets:
    [1984]  My War [SST023]                    not a reissue
    [1979]  Nervous Breakdown [1988] [SSTCD001] reissue
    [2013]  What The…                          no catalog number
  After a move, the old album folder and every parent that became empty
  are removed, up to and including the folder passed on the command line
  (never the library root, never a folder still holding any file).
  Moves to another drive copy, verify (size + SHA-256), then delete.
  DESTINATION is --dest, else the Featuring directory when the path is
  under one, else $DEFAULT_MUSIC if you set it. There is no built-in
  library. With none of these, discogs-tag stops before touching anything.
  Extra files and folders (Scans, Covers, logs, cues, …) move with the
  album. Vinyl/cassette sides are numbered
  by side (A1. Title, A2. Title, B1. Title); the track-number tag counts
  straight through (1 2 3) for players and the side is kept in
  DISCOGS_POSITION. --sequential-sides names them 1. 2. 3. instead.

safety:
  All files of an album are tagged before anything moves. Only the tags
  this tool writes are replaced. Comments, MusicBrainz ids and other text
  tags are kept unless --clear-other-tags; ReplayGain, AccurateRip,
  encoder info, art, lyrics and ISRC are always kept. One album failing
  does not stop the run.

library cleanup (no Discogs, no tag changes):
  --normalize-folders ROOT lists every album folder under ROOT whose name
  follows an older rule ("[1984]  My War [1984] [SST023]") and the new
  name ("[1984]  My War [SST023]"), plus everything skipped and why and
  every conflict. Nothing changes without --apply. Equal years are a
  non-reissue unless the album's tags have ORIGINALDATE/ORIGINALYEAR
  earlier than DATE (then the folder is skipped). Existing targets are
  never overwritten or merged. A generated album playlist named after the
  folder is renamed with it. Every rename is recorded in a JSON-lines
  journal in ~/.local/state/discogs-tag/journal/ ($XDG_STATE_HOME);
  --undo-journal FILE --apply reverses it newest first, skipping anything
  that changed since. Without --apply, --undo-journal only previews.

Undo:
  Every real apply also writes a journal (tags before and after, moves,
  folders, playlists). Its path is printed at the end of the run, and
  --undo-journal FILE undoes the whole apply the same way.
""".strip(),
    )
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=None,
        help="album folder or tree to process",
    )
    parser.add_argument("--dest", type=Path, help="library root to write into (AlbumArtist/Album/...)")
    parser.add_argument("--dry-run", action="store_true", help="search and show plan, write nothing")
    parser.add_argument(
        "--sequential-sides",
        action="store_true",
        help="name vinyl/cassette tracks 1. 2. 3. instead of A1. A2. B1. (tags are the same either way)",
    )
    parser.add_argument("--auto", action="store_true", help="apply only high-confidence matches")
    parser.add_argument("-y", "--yes", action="store_true", help="do not ask before writing")
    parser.add_argument(
        "--allow-track-mismatch",
        action="store_true",
        help="with --auto, apply a match even when its track count differs from the local files",
    )
    parser.add_argument("--skip-tagged", action="store_true", help="skip albums whose files all carry the same Discogs release id")
    parser.add_argument("--release-id", type=int, help="use this Discogs release id (single album)")
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="process at most N albums (0 = all); albums skipped by --skip-tagged do not count",
    )
    parser.add_argument(
        "--clear-other-tags",
        action="store_true",
        help="also remove text tags this tool does not write (comments, MusicBrainz ids, ...); "
        "ReplayGain, AccurateRip, encoder info, art, lyrics and ISRC are always kept",
    )
    parser.add_argument("--save-token", action="store_true", help="store a Discogs user token and exit")
    parser.add_argument("--self-test", action="store_true", help="run built-in naming tests and exit")
    parser.add_argument(
        "--normalize-folders",
        action="store_true",
        help="rename album folders under PATH to the current naming rule (preview unless --apply)",
    )
    parser.add_argument(
        "--undo-journal",
        type=Path,
        metavar="FILE",
        help="reverse everything recorded in a journal from an apply or --normalize-folders (preview unless --apply)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="really make the changes for --normalize-folders / --undo-journal (default is a preview)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.self_test:
        return self_test()
    global SIDE_NAMES
    SIDE_NAMES = not args.sequential_sides

    if args.save_token:
        token = prompt("Paste Discogs user token: ")
        if not token:
            print("no token entered", file=sys.stderr)
            return 1
        save_token(token)
        print(f"saved {TOKEN_PATH}")
        return 0

    if args.undo_journal is not None:
        apply = args.apply and not args.dry_run
        return undo_journal(args.undo_journal.expanduser(), apply=apply)

    if args.path is None:
        print("error: pass an album folder or tree", file=sys.stderr)
        return 2

    if args.normalize_folders:
        apply = args.apply and not args.dry_run
        return normalize_folders(args.path.expanduser(), apply=apply)
    if args.apply:
        print("error: --apply only goes with --normalize-folders or --undo-journal", file=sys.stderr)
        return 2

    root = args.path.expanduser()
    try:
        root = root.resolve()
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not root.exists():
        print(f"error: not found: {root}", file=sys.stderr)
        return 1

    dest = args.dest or default_dest(root)
    if dest is None:
        print(
            "error: no destination library. Pass --dest DIR, or set DEFAULT_MUSIC to your library root.",
            file=sys.stderr,
        )
        return 2
    dest = dest.expanduser().resolve()
    albums = collect_albums(root)
    if not albums:
        print(f"No album folders with audio under {root}")
        return 0
    if args.release_id and len(albums) > 1 and args.limit != 1:
        print("error: --release-id only works on a single album folder", file=sys.stderr)
        return 2

    token, key, secret = load_token()
    try:
        api = Discogs(token, key, secret)
    except DiscogsError as exc:
        print(exc, file=sys.stderr)
        return 1

    print(
        f"albums {len(albums)}   dest {dest}"
        + (f"   limit {args.limit}" if args.limit else "")
        + ("   DRY RUN" if args.dry_run else "")
    )
    applied = skipped = failed = 0
    processed = 0
    failures: list[tuple[Path, str]] = []
    try:
        for album in albums:
            if args.limit and processed >= args.limit:
                break
            try:
                result = process_album(
                    api,
                    album,
                    dest,
                    dry_run=args.dry_run,
                    auto=args.auto,
                    yes=args.yes,
                    skip_tagged=args.skip_tagged,
                    release_id=args.release_id,
                    clear_other=args.clear_other_tags,
                    scan_root=root,
                    allow_track_mismatch=args.allow_track_mismatch,
                )
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 - one album must not stop the batch
                processed += 1
                failed += 1
                detail = str(exc) if isinstance(exc, (DiscogsError, AlbumApplyError)) else f"{type(exc).__name__}: {exc}"
                print(f"  error: {album.path}: {detail}", file=sys.stderr)
                failures.append((album.path, detail.splitlines()[0] if detail else type(exc).__name__))
                continue
            if result == "skipped-tagged":
                skipped += 1
                continue
            processed += 1
            if result == "applied":
                applied += 1
            else:
                skipped += 1
    except KeyboardInterrupt:
        print("\nstopped")
    print(f"\ndone  applied={applied}  skipped={skipped}  failed={failed}")
    for path, reason in failures:
        print(f"  failed  {path}: {reason}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
