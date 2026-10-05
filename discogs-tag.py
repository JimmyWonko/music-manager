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
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

import requests
from mutagen import File as MutagenFile
from mutagen import MutagenError
from mutagen.easyid3 import EasyID3
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
from mutagen.mp4 import MP4

DEFAULT_MUSIC = Path("/path/to/Music")
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

SKIP_DIR_NAMES = {".git", ".trash", ".trashes", "@eadir", "scans", "covers", "artwork", "booklet", "pics"}

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
DISC_DIR = re.compile(r"^(?:cd|disc|disk)\s*[-_. ]?\s*\d+", re.I)
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
POS_CD = re.compile(r"^(?:cd\s*)?(\d+)\s*[-–.]\s*(\d+)$", re.I)
POS_VINYL = re.compile(r"^([A-Z]+)\s*(\d+)$", re.I)
RELEASE_URL = re.compile(r"discogs\.com/(?:.+/)?release/(\d+)", re.I)
MASTER_URL = re.compile(r"discogs\.com/(?:.+/)?master/(\d+)", re.I)


def natural_key(name: str) -> list:
    return [int(p) if p.isdigit() else p.casefold() for p in re.split(r"(\d+)", name)]


def should_skip_dir(path: Path) -> bool:
    return path.name.startswith(".") or path.name.lower() in SKIP_DIR_NAMES


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


def album_folder_name(album: str, original_year: str, release_year: str, catno: str | None, discs: int) -> str:
    orig = original_year or release_year
    press = release_year or original_year
    parts = [f"[{orig}]  {format_album_title(album)}"]
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


def collect_sidecars(folder: Path) -> list[Path]:
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        dirnames[:] = [d for d in dirnames if not should_skip_dir(Path(dirpath) / d)]
        for name in filenames:
            path = Path(dirpath) / name
            if is_sidecar(path):
                found.append(path)
    return found


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
                albums.append(AlbumFolder(current, tracks, collect_sidecars(current)))
            dirnames[:] = [d for d in dirnames if d not in disc_dirs]
            continue

        if here:
            albums.append(AlbumFolder(current, here, collect_sidecars(current)))
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
            return results
        fallback = {"type": "release", "per_page": "15", "q": " ".join(p for p in (artist, album) if p)}
        data = self._get(f"{API}/database/search", fallback, "search", json.dumps(fallback, sort_keys=True))
        return list(data.get("results") or [])

    def release(self, release_id: int) -> dict:
        return self._get(f"{API}/releases/{release_id}", None, "release", str(release_id))

    def master(self, master_id: int) -> dict:
        return self._get(f"{API}/masters/{master_id}", None, "master", str(master_id))


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


def save_token(token: str) -> None:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(token.strip() + "\n", encoding="utf-8")
    TOKEN_PATH.chmod(0o600)


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    text = ARTIST_NUM_SUFFIX.sub("", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return text.strip()


def similar(a: str, b: str) -> float:
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


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
    pos = (position or "").strip()
    if not pos:
        return (1, 0)
    match = POS_CD.match(pos)
    if match:
        return int(match.group(1)), int(match.group(2))
    match = POS_VINYL.match(pos)
    if match:
        letters = match.group(1).upper()
        side = 0
        for ch in letters:
            side = side * 26 + (ord(ch) - 64)
        return 1, (side - 1) * 20 + int(match.group(2))
    digits = re.findall(r"\d+", pos)
    if len(digits) >= 2:
        return int(digits[0]), int(digits[1])
    if digits:
        return 1, int(digits[0])
    return (1, 0)


@dataclass
class DiscogsTrack:
    disc: int
    number: int
    title: str
    artist: str


def flatten_tracklist(release: dict, album_artist: str) -> list[DiscogsTrack]:
    out: list[DiscogsTrack] = []
    seq = 0
    last_disc = 1
    for item in release.get("tracklist") or []:
        kind = (item.get("type_") or "track").lower()
        if kind in {"heading", "index"}:
            continue
        title = (item.get("title") or "").strip()
        if not title:
            continue
        disc, number = parse_position(item.get("position") or "")
        if disc != last_disc:
            seq = 0
            last_disc = disc
        seq += 1
        if number <= 0:
            number = seq
        artists = item.get("artists") or []
        if artists:
            artist = " & ".join(
                ARTIST_NUM_SUFFIX.sub("", a.get("anv") or a.get("name") or "").strip()
                for a in artists
            )
        else:
            artist = album_artist
        out.append(DiscogsTrack(disc=max(disc, 1), number=number, title=title, artist=artist))
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


def write_tags(path: Path, meta: dict) -> None:
    suffix = path.suffix.lower()
    if suffix == ".mp3":
        _write_mp3(path, meta)
        return
    if suffix in {".m4a", ".mp4", ".aac"}:
        _write_mp4(path, meta)
        return
    audio = MutagenFile(path)
    if audio is None:
        raise RuntimeError(f"unreadable: {path}")
    mapping = {
        "ARTIST": meta["artist"],
        "ALBUMARTIST": meta["albumartist"],
        "ALBUM": meta["album"],
        "TITLE": meta["title"],
        "TRACKNUMBER": str(meta["track"]),
        "TRACKTOTAL": str(meta["tracktotal"]),
        "DISCNUMBER": str(meta["disc"]),
        "DISCTOTAL": str(meta["disctotal"]),
        "DATE": meta["date"],
        "YEAR": year4(meta["date"]),
        "ORIGINALDATE": meta["originaldate"],
        "ORIGINALYEAR": year4(meta["originaldate"]),
        "LABEL": meta["label"],
        "CATALOGNUMBER": meta["catalognumber"],
        "GENRE": meta["genre"],
        "DISCOGS_RELEASE_ID": str(meta["release_id"]),
        "DISCOGS_MASTER_ID": str(meta.get("master_id") or ""),
    }
    for key, value in mapping.items():
        if not value:
            continue
        audio[key] = value
    audio.save()


def _ensure_id3(path: Path) -> ID3:
    try:
        return ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
        tags.save(path)
        return ID3(path)


def _write_mp3(path: Path, meta: dict) -> None:
    tags = _ensure_id3(path)
    tags.delall("TXXX:DISCOGS_RELEASE_ID")
    tags.delall("TXXX:DISCOGS_MASTER_ID")
    tags.delall("TXXX:CATALOGNUMBER")
    tags["TIT2"] = TIT2(encoding=3, text=meta["title"])
    tags["TPE1"] = TPE1(encoding=3, text=meta["artist"])
    tags["TPE2"] = TPE2(encoding=3, text=meta["albumartist"])
    tags["TALB"] = TALB(encoding=3, text=meta["album"])
    tags["TRCK"] = TRCK(encoding=3, text=f"{meta['track']}/{meta['tracktotal']}")
    tags["TPOS"] = TPOS(encoding=3, text=f"{meta['disc']}/{meta['disctotal']}")
    if meta["date"]:
        tags["TDRC"] = TDRC(encoding=3, text=meta["date"])
    if meta["originaldate"]:
        tags["TDOR"] = TDOR(encoding=3, text=meta["originaldate"])
    if meta["label"]:
        tags["TPUB"] = TPUB(encoding=3, text=meta["label"])
    if meta["genre"]:
        tags["TCON"] = TCON(encoding=3, text=meta["genre"])
    if meta["catalognumber"]:
        tags.add(TXXX(encoding=3, desc="CATALOGNUMBER", text=meta["catalognumber"]))
    tags.add(TXXX(encoding=3, desc="DISCOGS_RELEASE_ID", text=str(meta["release_id"])))
    if meta.get("master_id"):
        tags.add(TXXX(encoding=3, desc="DISCOGS_MASTER_ID", text=str(meta["master_id"])))
    tags.save(path)


def _write_mp4(path: Path, meta: dict) -> None:
    audio = MP4(path)
    audio["\xa9nam"] = [meta["title"]]
    audio["\xa9ART"] = [meta["artist"]]
    audio["aART"] = [meta["albumartist"]]
    audio["\xa9alb"] = [meta["album"]]
    audio["trkn"] = [(int(meta["track"]), int(meta["tracktotal"]))]
    audio["disk"] = [(int(meta["disc"]), int(meta["disctotal"]))]
    if meta["date"]:
        audio["\xa9day"] = [meta["date"]]
    if meta["genre"]:
        audio["\xa9gen"] = [meta["genre"]]
    audio["----:com.apple.iTunes:DISCOGS_RELEASE_ID"] = [str(meta["release_id"]).encode()]
    if meta.get("catalognumber"):
        audio["----:com.apple.iTunes:CATALOGNUMBER"] = [meta["catalognumber"].encode()]
    audio.save()


def unique_dest(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(2, 50):
        candidate = path.with_name(f"{stem} ({i}){suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"cannot find free name for {path}")


def apply_album(
    album: AlbumFolder,
    dest_root: Path,
    release: dict,
    dtracks: list[DiscogsTrack],
    original_year: str,
    dry_run: bool,
) -> None:
    album_artist = release_artists(release)
    folder_artist = format_artist(album_artist)
    title = release.get("title") or ""
    release_year = year4(release.get("released") or release.get("year"))
    orig = original_year or release_year
    catno = release_catno(release)
    label = ""
    if release.get("labels"):
        label = release["labels"][0].get("name") or ""
    genres = release.get("genres") or []
    styles = release.get("styles") or []
    genre = ", ".join(styles[:2] or genres[:2])
    discs = release_discs(release, dtracks)
    master_id = release.get("master_id") or 0

    folder = album_folder_name(title, orig, release_year, catno, discs)
    target_dir = dest_root / folder_artist / folder

    print(f"  dest  {target_dir}")
    if not dry_run:
        target_dir.mkdir(parents=True, exist_ok=True)

    remaining = list(dtracks)
    used: set[int] = set()

    def take_track(local: Path) -> DiscogsTrack | None:
        stem = local.stem
        match = TRACK_PREFIX.match(stem)
        if match:
            want = int(match.group(1))
            for i, dt in enumerate(remaining):
                if dt.number == want and i not in used:
                    used.add(i)
                    return remaining[i]
        for i, dt in enumerate(remaining):
            if i not in used:
                used.add(i)
                return dt
        return None

    by_disc: dict[int, list[DiscogsTrack]] = {}
    for dt in dtracks:
        by_disc.setdefault(dt.disc, []).append(dt)

    for local in album.tracks:
        dt = take_track(local)
        if dt is None:
            print(f"  skip  extra file  {local.name}")
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
        }
        dest_name = track_filename(dt.number, dt.title, local.suffix)
        dest_file = target_dir / dest_name
        print(f"  {local.name}")
        print(f"      -> {dest_file.name}   [{dt.disc}-{dt.number}]")
        if dry_run:
            continue
        write_tags(local, meta)
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        if dest_file.exists() and dest_file.resolve() != local.resolve():
            dest_file = unique_dest(dest_file)
        if local.resolve() != dest_file.resolve():
            local.rename(dest_file)

    for sidecar in album.sidecars:
        dest_file = target_dir / sidecar.name
        print(f"  move  {sidecar.name}")
        if dry_run:
            continue
        if dest_file.exists() and dest_file.resolve() == sidecar.resolve():
            continue
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        if dest_file.exists():
            dest_file = unique_dest(dest_file)
        sidecar.rename(dest_file)

    if not dry_run:
        _prune_empty(album.path, dest_root)


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
            results = api.search_releases(artist.strip(), album.strip(), guess.get("catno", ""))
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
) -> str:
    if skip_tagged:
        tagged = already_tagged(album.tracks[0])
        if tagged:
            print(f"skip tagged (Discogs {tagged}): {album.path}")
            return "skipped"

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
            results = api.search_releases("", guess["album"], guess.get("catno", ""))

    try:
        release = choose_release(api, guess, results, auto=auto, release_id=release_id)
    except KeyboardInterrupt:
        raise
    if release is None:
        print("  skipped")
        return "skipped"

    album_artist = release_artists(release)
    dtracks = flatten_tracklist(release, album_artist)
    original_year = year4(release.get("released") or release.get("year"))
    master_id = release.get("master_id")
    if master_id:
        try:
            master = api.master(int(master_id))
            original_year = year4(master.get("year")) or original_year
        except DiscogsError as exc:
            print(f"  warning: master fetch failed: {exc}")

    print(f"  match  {album_artist} – {release.get('title')}  [{release.get('year')}]")
    print(f"         orig {original_year or '?'}  cat {release_catno(release) or '-'}  "
          f"{len(dtracks)} tracks / {release_discs(release, dtracks)} disc(s)")
    print(f"         https://www.discogs.com/release/{release.get('id')}")

    if len(dtracks) != len(album.tracks):
        print(f"  warning: {len(album.tracks)} local files vs {len(dtracks)} Discogs tracks")

    if not yes and sys.stdin.isatty() and not dry_run:
        ans = prompt("  apply tags + rename? [Y/n] ").lower()
        if ans in {"n", "no"}:
            return "skipped"

    apply_album(album, dest_root, release, dtracks, original_year, dry_run=dry_run)
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

    print("self-test ok")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Tag and rename albums from Discogs using this library's folder rules."
    )
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=None,
        help="album folder or tree to process",
    )
    parser.add_argument("--dest", type=Path, help="library root to write into (Artist/Album/...)")
    parser.add_argument("--dry-run", action="store_true", help="search and show plan, write nothing")
    parser.add_argument("--auto", action="store_true", help="apply only high-confidence matches")
    parser.add_argument("-y", "--yes", action="store_true", help="do not ask before writing")
    parser.add_argument("--skip-tagged", action="store_true", help="skip albums that already have a Discogs id")
    parser.add_argument("--release-id", type=int, help="use this Discogs release id (single album)")
    parser.add_argument("--limit", type=int, default=0, help="process at most N albums")
    parser.add_argument("--save-token", action="store_true", help="store a Discogs user token and exit")
    parser.add_argument("--self-test", action="store_true", help="run built-in naming tests")
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
    if args.limit:
        albums = albums[: args.limit]
    if not albums:
        print(f"No album folders with audio under {root}")
        return 0
    if args.release_id and len(albums) > 1:
        print("error: --release-id only works on a single album folder", file=sys.stderr)
        return 2

    token, key, secret = load_token()
    try:
        api = Discogs(token, key, secret)
    except DiscogsError as exc:
        print(exc, file=sys.stderr)
        return 1

    print(f"albums {len(albums)}   dest {dest}" + ("   DRY RUN" if args.dry_run else ""))
    applied = skipped = failed = 0
    try:
        for album in albums:
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
                )
            except (DiscogsError, MutagenError) as exc:
                print(f"  error: {album.path}: {exc}", file=sys.stderr)
                failed += 1
                continue
            if result == "applied":
                applied += 1
            else:
                skipped += 1
    except KeyboardInterrupt:
        print("\nstopped")
    print(f"\ndone  applied={applied}  skipped={skipped}  failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
