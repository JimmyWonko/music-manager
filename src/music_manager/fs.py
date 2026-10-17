"""File-system helpers: walking a library, audio/sidecar/artwork detection, album extras,
file hashing and the freedesktop trash. The one copy of these helpers (they used to be
duplicated between discogs_tag.py and mm_common.py).
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

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
DISC_DIR = re.compile(r"^(?:cd|disc|disk)\s*[-_. ]?\s*\d+", re.I)


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
    return bool(disc_dirs_with_audio(parent))


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


COPY_CHUNK = 4 * 1024 * 1024


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


def disc_dirs_with_audio(folder: Path) -> list[Path]:
    """CD1/Disc 2/... subfolders of folder that contain audio, in order."""
    found: list[Path] = []
    try:
        entries = list(folder.iterdir())
    except OSError:
        return []
    for entry in entries:
        if entry.is_dir() and not entry.is_symlink() and DISC_DIR.match(entry.name) and audio_in(entry):
            found.append(entry)
    found.sort(key=lambda p: natural_key(p.name))
    return found


# None: the user's freedesktop trash. service.configure(data_dir=...) points it inside that dir.
TRASH_DIR: Path | None = None


def trash_root() -> Path:
    """TRASH_DIR when set, else the freedesktop trash ($XDG_DATA_HOME/Trash, else ~/.local/share/Trash)."""
    if TRASH_DIR is not None:
        return Path(TRASH_DIR)
    data = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(data).expanduser() if data else Path.home() / ".local" / "share"
    return base / "Trash"


def send_to_trash(path: Path) -> Path:
    """Move a file into the freedesktop trash. Returns where it landed.

    Writes the .trashinfo file first. On a different drive the move copies,
    checks size, then removes the original, so a failure never loses the file.
    Raises OSError and leaves the original in place when the trash can't be written.
    """
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        raise FileNotFoundError(path)
    root = trash_root()
    files, info = root / "files", root / "info"
    files.mkdir(parents=True, exist_ok=True)
    info.mkdir(parents=True, exist_ok=True)
    dest = files / path.name
    n = 1
    while dest.exists() or (info / f"{dest.name}.trashinfo").exists():
        n += 1
        dest = files / f"{path.name}.{n}"
    info_path = info / f"{dest.name}.trashinfo"
    when = time.strftime("%Y-%m-%dT%H:%M:%S")
    info_path.write_text(f"[Trash Info]\nPath={path.resolve()}\nDeletionDate={when}\n", encoding="utf-8")
    try:
        shutil.move(str(path), dest)
    except BaseException:
        info_path.unlink(missing_ok=True)
        raise
    return dest
