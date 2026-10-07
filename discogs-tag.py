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
import hashlib
import json
import os
import re
import shutil
import sys
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

_DRIVE_MUSIC = Path("/path/to/Music")
DEFAULT_MUSIC = Path(
    os.getenv("DEFAULT_MUSIC") or (_DRIVE_MUSIC if _DRIVE_MUSIC.exists() else Path.home() / "Music")
)

TOKEN_PATH = Path.home() / ".config" / "discogs-tag" / "token"
CACHE_DIR = Path.home() / ".cache" / "discogs-tag"
USER_AGENT = "xen-discogs-tag/1.0 +local-library-tool"
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
# "2-05 Title" / "2.05 Title": disc 2, track 5 (only trusted on multi-disc releases)
DISC_TRACK_PREFIX = re.compile(r"^(\d{1,2})[-.](\d{1,3})(?=[\s._-])")
# "B2. Title" / "A1 - Title": vinyl/cassette side + track (only trusted when
# the release really has that position)
SIDE_PREFIX = re.compile(r"^([A-Za-z]{1,2}\d{1,2})(?=[\s._-])")
POS_CD = re.compile(r"^(?:(?:cd|dvd|disc|disk)\s*)?(\d+)\s*[-–.]\s*(\d+)[a-z]?$", re.I)
POS_NUM = re.compile(r"^(\d+)([a-z]|\.\d+)?$", re.I)
POS_VINYL = re.compile(r"^([A-Z]{1,2})\s*(\d*)([a-z]|\.\d+)?$", re.I)
RELEASE_URL = re.compile(r"discogs\.com/(?:.+/)?release/(\d+)", re.I)
MASTER_URL = re.compile(r"discogs\.com/(?:.+/)?master/(\d+)", re.I)


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
    return is_junk_dir(path) or is_artwork_dir(path)


def is_audio(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in AUDIO_EXTS


def is_sidecar(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in SIDECAR_EXTS


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
    """[lead year]  Album [2CD] [release year] [CATNO]

    original_year is the master's year. The leading year is always present
    when any year is known (see folder_lead_year), so non-reissues read
    "[1984]  My War [1984] [SST023]".
    """
    lead = folder_lead_year(original_year, release_year)
    press = release_year or original_year
    parts = [f"[{lead}]  {format_album_title(album)}"]
    if discs > 1:
        parts.append(f"{discs}CD")
    parts.append(f"[{press}]")
    catalog = format_catalog(catno)
    if catalog:
        parts.append(f"[{catalog}]")
    return sanitize_filename(" ".join(parts))


def track_filename(number: int, title: str, suffix: str) -> str:
    return sanitize_filename(f"{number}. {title}") + suffix.lower()


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


def default_dest(start: Path) -> Path:
    resolved = start.resolve()
    parts = list(resolved.parts)
    if "Featuring" in parts:
        return Path(*parts[: parts.index("Featuring") + 1])
    if DEFAULT_MUSIC.exists():
        return DEFAULT_MUSIC.resolve()
    try:
        resolved.relative_to(DEFAULT_MUSIC)
        return DEFAULT_MUSIC
    except ValueError:
        return start if start.is_dir() else start.parent


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
    Artwork, Booklet, …) are moved as directories. Leftover files inside
    disc folders are kept with their relative path.
    """
    track_set = {path.resolve() for path in tracks}
    extra_files: list[Path] = []
    extra_dirs: list[Path] = []
    try:
        entries = list(folder.iterdir())
    except OSError:
        return [], []

    for entry in entries:
        if entry.is_dir() and is_junk_dir(entry):
            continue
        if entry.is_dir():
            if _dir_holds_track(entry, tracks):
                for dirpath, dirnames, filenames in os.walk(entry, followlinks=False):
                    dirnames[:] = [d for d in dirnames if not is_junk_dir(Path(dirpath) / d)]
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
        if cache.exists() and time.time() - cache.stat().st_mtime < 30 * 86400:
            return json.loads(cache.read_text(encoding="utf-8"))

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
            cache.write_text(json.dumps(data), encoding="utf-8")
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


def score_result(result: dict, guess: dict[str, str]) -> float:
    r_artist, r_album = split_result_title(result.get("title") or "")
    score = 0.40 * similar(guess.get("album", ""), r_album)
    score += 0.35 * similar(guess.get("artist", ""), r_artist)
    cat = (guess.get("catno") or "").replace(" ", "").upper()
    rcat = (result.get("catno") or "").replace(" ", "").upper()
    if cat and rcat and (cat == rcat or cat in rcat or rcat in cat):
        score += 0.18
    gy, ry = guess.get("year", ""), str(result.get("year") or "")
    if gy and ry and gy == ry:
        score += 0.07
    return min(score, 1.0)


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
    order, so A1 A2 B1 B2 become 1 2 3 4.
    """
    parsed = []
    for position, title, artist in _tracklist_entries(release, album_artist):
        disc, number = parse_position(position)
        parsed.append((max(disc, 1), number, position, title, artist))

    by_disc: dict[int, list[int]] = {}
    for i, (disc, *_rest) in enumerate(parsed):
        by_disc.setdefault(disc, []).append(i)

    numbers: dict[int, int] = {}
    for indexes in by_disc.values():
        explicit = [parsed[i][1] for i in indexes]
        sides = any(is_side_position(parsed[i][2]) for i in indexes)
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


def write_tags(path: Path, meta: dict, clear_other: bool = False, dry_run: bool = False) -> list[str]:
    """Write meta into path with the format's own tag names.

    Returns the tag keys that were removed and not rewritten (stale aliases,
    and with clear_other also unowned text fields). With dry_run the file is
    read and the changes are computed, but nothing is saved.
    """
    replace = set(ALWAYS_REPLACED)
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


def _write_mp3(path: Path, meta: dict) -> None:
    write_tags(path, meta)


def _write_mp4(path: Path, meta: dict) -> None:
    write_tags(path, meta)


# ---------------------------------------------------------------------------
# Matching local files to Discogs tracks
# ---------------------------------------------------------------------------

def _disc_from_folder(local: Path, album_path: Path) -> int | None:
    parent = local.parent
    if parent == album_path or not DISC_DIR.match(parent.name):
        return None
    match = re.search(r"\d+", parent.name)
    return int(match.group(0)) if match else None


def match_tracks(album: AlbumFolder, dtracks: list[DiscogsTrack]) -> tuple[list[tuple[Path, DiscogsTrack | None]], bool]:
    """Pair local files with Discogs tracks.

    1. A file named after a side position the release has ("B2. Title")
       gets that track.
    2. A leading number ("06 Title", "6. Title", "2-06 Title") is matched
       against the track numbers; a CD1/CD2 folder limits it to that disc.
       Files renamed by older versions (B1 = 21) are recognised and matched
       by that old number when that fits the folder better.
    3. Anything left takes the remaining tracks in tracklist order.

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
        plain = TRACK_PREFIX.match(stem)
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
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(2, 50):
        candidate = path.with_name(f"{stem} ({i}){suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"cannot find free name for {path}")


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _move(src: Path, dest: Path) -> Path:
    """Move src to dest without ever overwriting a different file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() or dest.is_symlink():
        if _same_file(src, dest):
            if str(src) != str(dest):
                os.rename(src, dest)  # case-only rename on a case-insensitive drive
            return dest
        dest = unique_dest(dest)
    shutil.move(str(src), str(dest))
    return dest


def move_planned(items: list[PlannedFile], done: list[tuple[Path, Path]]) -> None:
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
        staged = unique_dest(blocker.with_name(f".discogs-tag-{os.getpid()}-{blocker.name}"))
        os.rename(blocker, staged)
        done.append((blocker, staged))
        for other in items:
            if other.source is blocker:
                other.source = staged
        sources = [i.source for i in items]
    for item in items:
        final = _move(item.source, item.dest)
        done.append((item.source, final))


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


def apply_album(
    album: AlbumFolder,
    dest_root: Path,
    release: dict,
    dtracks: list[DiscogsTrack],
    original_year: str,
    dry_run: bool,
    clear_other: bool = False,
) -> list[tuple[Path, Path]]:
    """Tag the album's files, then move them into place.

    Order: plan everything, write tags to every file in place (nothing is
    moved if any file fails), then move tracks, extra folders and sidecars.
    original_year is the master's year ("" when unknown); it is only used
    for a reissue. Returns the (source, destination) list of moves made.
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
        dest_file = target_dir / track_filename(dt.number, dt.title, local.suffix)
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
    tagged: list[Path] = []
    for item in plan:
        try:
            removed = write_tags(item.source, item.meta, clear_other=clear_other, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001 - any failure must stop this album only
            msg = f"tagging failed for {item.source}: {type(exc).__name__}: {exc}\n  nothing was moved"
            if tagged and not dry_run:
                msg += f"; {len(tagged)} file(s) were already re-tagged in place:\n"
                msg += "\n".join(f"    {p}" for p in tagged)
            raise AlbumApplyError(msg) from exc
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
        target_dir.mkdir(parents=True, exist_ok=True)
        move_planned(moves, done)
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
        regenerate_playlist(target_dir, old_playlists)
    _prune_empty(album.path, dest_root)
    return done


def regenerate_playlist(target_dir: Path, old_playlists: list[Path]) -> None:
    for playlist in old_playlists:
        try:
            if playlist.exists():
                playlist.unlink()
        except OSError as exc:
            print(f"  warning: could not delete old playlist {playlist}: {exc}")
    dest = mm_common.playlist_path(target_dir)
    if dest.exists() and not mm_common.is_generated_playlist(dest):
        print(f"  warning: not replacing hand-made playlist {dest}")
        return
    tracks = mm_common.album_playlist_tracks(target_dir)
    if not tracks:
        return
    try:
        mm_common.write_playlist_file(dest, mm_common.render_playlist(target_dir, tracks))
        print(f"  playlist  {dest.name}  ({len(tracks)} tracks)")
    except OSError as exc:
        print(f"  warning: could not write playlist {dest}: {exc}")


def _prune_empty(start: Path, stop: Path) -> None:
    current = start
    stop = stop.resolve()
    while True:
        try:
            current.relative_to(stop)
        except ValueError:
            return
        if current.resolve() == stop:
            return
        try:
            next(current.iterdir())
            return
        except StopIteration:
            current.rmdir()
            current = current.parent
        except OSError:
            return


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
    if auto and ranked:
        top = ranked[0][0]
        second = ranked[1][0] if len(ranked) > 1 else 0.0
        if top >= 0.90 and top - second >= 0.08:
            print(f"  auto  {int(top * 100)}%  {ranked[0][1].get('title')}")
            return api.release(int(ranked[0][1]["id"]))
        print("  no confident auto-match")
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
) -> str:
    """Returns "applied", "skipped", or "skipped-tagged" (not counted by --limit)."""
    if skip_tagged:
        tagged = already_tagged(album.tracks[0])
        if tagged:
            print(f"skip tagged (Discogs {tagged}): {album.path}")
            return "skipped-tagged"

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

    if not yes and sys.stdin.isatty() and not dry_run:
        ans = prompt("  apply tags + rename? [Y/n] ").lower()
        if ans in {"n", "no"}:
            return "skipped"

    apply_album(album, dest_root, release, dtracks, original_year, dry_run=dry_run, clear_other=clear_other)
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
    name = album_folder_name("Come On Die Young", "1999", "1999", "OLE365-2", 1)
    assert name == "[1999]  Come On Die Young [1999] [OLE365-2]", name
    name = album_folder_name("Mermaid Avenue", "1998", "1998", "7559-62204-1", 2)
    assert name == "[1998]  Mermaid Avenue 2CD [1998] [7559-62204-1]", name
    # Leading year is always present: no master -> release year.
    name = album_folder_name("My War", "", "1984", "SST 023", 1)
    assert name == "[1984]  My War [1984] [SST023]", name
    name = album_folder_name("The First Four Years", "1983", "1990", None, 1)
    assert name == "[1983]  The First Four Years [1990]", name
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
        _write_mp3(
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

    if DEFAULT_MUSIC.exists():
        assert default_dest(Path("/tmp/Artist - Album")) == DEFAULT_MUSIC.resolve()
        featuring = DEFAULT_MUSIC / "Aesop Rock" / "Featuring" / "Some Album"
        assert default_dest(featuring) == DEFAULT_MUSIC / "Aesop Rock" / "Featuring"

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
    assert track_filename(tracks[2].number, "c", ".flac") == "3. c.flac"

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

credentials:
  Create a personal token at
  https://www.discogs.com/settings/developers
  then run --save-token. The token is stored at
  ~/.config/discogs-tag/token (mode 600).
  DISCOGS_TOKEN overrides that file.
  DISCOGS_KEY and DISCOGS_SECRET are the consumer-key alternative.

where files go:
  DESTINATION/AlbumArtist/[original year]  Album [press year] [CATNO]/N. Title.ext
  The leading year is the master's year for a reissue (master year earlier
  than the release year), otherwise the release's own year, so albums sort
  chronologically. The ORIGINALDATE tags are only written for a reissue.
  DESTINATION is --dest, else the Featuring directory when the path is
  under one, else $DEFAULT_MUSIC (default: /path/to/Music
  when that volume is mounted). Extra files and folders (Scans, Covers,
  logs, cues, …) move with the album. Vinyl/cassette sides are numbered
  straight through (A1 A2 B1 -> 1 2 3); the side is kept in DISCOGS_POSITION.

safety:
  All files of an album are tagged before anything moves. Only the tags
  this tool writes are replaced. Comments, MusicBrainz ids and other text
  tags are kept unless --clear-other-tags; ReplayGain, AccurateRip,
  encoder info, art, lyrics and ISRC are always kept. One album failing
  does not stop the run.
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
    parser.add_argument("--auto", action="store_true", help="apply only high-confidence matches")
    parser.add_argument("-y", "--yes", action="store_true", help="do not ask before writing")
    parser.add_argument("--skip-tagged", action="store_true", help="skip albums that already have a Discogs id")
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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.self_test:
        return self_test()

    if args.save_token:
        token = prompt("Paste Discogs user token: ")
        if not token:
            print("no token entered", file=sys.stderr)
            return 1
        save_token(token)
        print(f"saved {TOKEN_PATH}")
        return 0

    if args.path is None:
        print("error: pass an album folder or tree", file=sys.stderr)
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

    dest = (args.dest or default_dest(root)).expanduser().resolve()
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
