"""File-system helpers: walking a library, audio/sidecar/artwork detection, album extras,
file hashing and the freedesktop trash. The one copy of these helpers (they used to be
duplicated between discogs_tag.py and mm_common.py).
"""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import re
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from .conflicts import LayoutConflict

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
    # Non-empty: the folder's layout is ambiguous (see collect_albums) and it is never applied.
    layout_problem: str = ""
    layout_conflict: LayoutConflict | None = None  # what caused it: subfolders and loose files


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


def _has_audio_below(folder: Path) -> bool:
    """True when folder, or any folder below it that is walked for albums, holds an audio file."""
    for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        current = Path(dirpath)
        dirnames[:] = [d for d in dirnames if not should_skip_dir(current / d)]
        if any(Path(name).suffix.lower() in AUDIO_EXTS and (current / name).is_file() for name in filenames):
            return True
    return False


MIXED_DISCS = ("loose audio files sit next to disc folders with audio ({discs}); it is not clear which files "
               "belong to which disc, so nothing is applied. Move the loose files into a disc folder (or the "
               "disc folders' files up) and scan again")
STRAY_AUDIO = ("loose audio files sit in a folder that also holds other album folders ({folders}); this folder "
               "is not an album and nothing in it is applied or moved. Move the loose files into their album "
               "folder and scan again")


def collect_albums(root: Path) -> list[AlbumFolder]:
    """Every album folder under root.

    A folder with audio files is an album; its other folders travel with it as extras. A
    folder with CD1/Disc 2/... subfolders of audio (and no loose audio) is one album made of
    those discs. Two layouts are ambiguous and come back with a layout_problem, so they are
    reported and never applied:
      - loose audio next to disc folders with audio: one album with all of that audio;
      - loose audio next to other folders that hold audio (an artist folder with a stray
        file): only the loose files, with no extras, and the folders below are walked as
        albums of their own.
    """
    albums: list[AlbumFolder] = []
    for dirpath, dirnames, _filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        dirnames[:] = [d for d in dirnames if not should_skip_dir(current / d)]
        if should_skip_dir(current) and current != root:
            continue

        here = audio_in(current)
        disc_dirs = [d for d in list(dirnames) if DISC_DIR.match(d) and audio_in(current / d)]

        if here:
            disc_tracks: list[Path] = []
            for name in sorted(disc_dirs, key=natural_key):
                disc_tracks.extend(audio_in(current / name))
            nested = sorted((d for d in dirnames if d not in disc_dirs and _has_audio_below(current / d)),
                            key=natural_key)
            if nested:
                problem = STRAY_AUDIO.format(folders=", ".join(f"{d}/" for d in nested))
                conflict = LayoutConflict("album_folders", [current / d for d in nested], list(here))
                albums.append(AlbumFolder(current, here + disc_tracks, layout_problem=problem,
                                          layout_conflict=conflict))
                dirnames[:] = nested
                continue
            if disc_dirs:
                tracks = here + disc_tracks
                files, dirs = collect_extras(current, tracks)
                ordered = sorted(disc_dirs, key=natural_key)
                problem = MIXED_DISCS.format(discs=", ".join(f"{d}/" for d in ordered))
                conflict = LayoutConflict("disc_folders", [current / d for d in ordered], list(here))
                albums.append(AlbumFolder(current, tracks, files, dirs, layout_problem=problem,
                                          layout_conflict=conflict))
                dirnames[:] = []
                continue

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


log = logging.getLogger(__name__)

TrashKind = Literal["home", "topdir", "topdir_user"]


@dataclass(frozen=True)
class TrashTarget:
    """Which trash a file goes to (freedesktop Trash spec, "Trash directories")."""
    root: Path  # the trash folder: holds files/ and info/
    kind: TrashKind  # home; topdir = $topdir/.Trash/$uid; topdir_user = $topdir/.Trash-$uid
    topdir: Path | None = None  # the mount point, for the topdir kinds (Path= is relative to it)
    notes: tuple[str, ...] = ()  # checks that failed on the way (the spec asks to report them)


def _uid() -> int:
    return os.getuid()


def _device(path: Path) -> int:
    """st_dev of path, or of its nearest existing parent (the home trash may not exist yet)."""
    current = Path(os.path.abspath(path))
    while True:
        try:
            return os.lstat(current).st_dev
        except OSError:
            if current.parent == current:
                raise
            current = current.parent


def _mount_point(path: Path) -> Path:
    """The top directory of the filesystem path is on ($topdir in the spec)."""
    current = Path(os.path.abspath(path))
    dev = _device(current)
    while current.parent != current:
        try:
            if os.lstat(current.parent).st_dev != dev:
                return current
        except OSError:
            return current
        current = current.parent
    return current


def _own_dir_problem(path: Path, uid: int) -> str:
    """Why an existing per-user trash folder can't be used ("" when it can)."""
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        return f"{path} is a symbolic link"
    if not stat.S_ISDIR(st.st_mode):
        return f"{path} is not a directory"
    if st.st_uid != uid:
        return f"{path} is owned by uid {st.st_uid}, not {uid}"
    if stat.S_IMODE(st.st_mode) & 0o077:
        return f"{path} has mode {stat.S_IMODE(st.st_mode):04o}; others can get in (needs 0700)"
    return ""


def _make_own_dir(path: Path, notes: list[str], create: bool) -> bool:
    """Use an existing safe per-user trash folder, or create it 0700. False (with a note) if not."""
    if os.path.lexists(path):
        problem = _own_dir_problem(path, _uid())
        if problem:
            notes.append(f"not using {path}: {problem}")
            return False
        return True
    if not create:
        return True
    try:
        os.mkdir(path, 0o700)
        os.chmod(path, 0o700)  # whatever the umask
    except OSError as exc:
        notes.append(f"cannot create {path}: {exc.strerror or exc}")
        return False
    return True


def trash_for(path: Path, create: bool = True) -> TrashTarget:
    """The trash a file at path goes to, following the freedesktop Trash spec.

    On the home trash's drive: the home trash (trash_root()). Elsewhere, on the
    file's filesystem top directory: 1. $topdir/.Trash/$uid, if $topdir/.Trash is a
    real directory (not a symlink) with the sticky bit; 2. $topdir/.Trash-$uid,
    created 0700 when missing, used only if it is ours, a real folder and 0700;
    3. otherwise the home trash (send_to_trash then copies, verified). Failed checks
    are listed in notes. create=False only looks: nothing is made.
    """
    home = trash_root()
    path = Path(path)
    if _device(path) == _device(home):
        return TrashTarget(home, "home")
    topdir = _mount_point(Path(os.path.abspath(path)).parent)
    notes: list[str] = []
    uid = _uid()
    shared = topdir / ".Trash"
    if os.path.lexists(shared):
        st = os.lstat(shared)
        if stat.S_ISLNK(st.st_mode):
            notes.append(f"not using {shared}: it is a symbolic link")
        elif not stat.S_ISDIR(st.st_mode):
            notes.append(f"not using {shared}: it is not a directory")
        elif not st.st_mode & stat.S_ISVTX:
            notes.append(f"not using {shared}: it has no sticky bit")
        elif _make_own_dir(shared / str(uid), notes, create):
            return TrashTarget(shared / str(uid), "topdir", topdir, tuple(notes))
    user = topdir / f".Trash-{uid}"
    if _make_own_dir(user, notes, create):
        return TrashTarget(user, "topdir_user", topdir, tuple(notes))
    notes.append(f"using the home trash {home} (a verified copy) for {path}")
    return TrashTarget(home, "home", None, tuple(notes))


@dataclass(frozen=True)
class Trashed:
    dest: Path  # where the item now is
    target: TrashTarget


def send_to_trash(path: Path) -> Path:
    """Move a file into the trash (see trash_for and trash_file). Returns where it landed."""
    return trash_file(path).dest


def trash_file(path: Path) -> Trashed:
    """Move a file into the freedesktop trash for its drive (see trash_for).

    Writes the .trashinfo file first; its Path= is the original location,
    percent-encoded as the spec asks: absolute in the home trash, relative to
    the top directory in a drive's trash. In a drive's own trash, or on the home
    trash's drive, it is a plain rename. Into the home trash from another drive
    a file gets the mover's verified copy (size and SHA-256 read back) before
    the original is removed, so a failure never loses it. Failed spec checks are
    logged as warnings. Raises OSError and leaves the original in place when the
    trash can't be written.
    """
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        raise FileNotFoundError(path)
    target = trash_for(path)
    for note in target.notes:
        log.warning("trash: %s", note)
    root = target.root
    files, info = root / "files", root / "info"
    files.mkdir(parents=True, exist_ok=True)
    info.mkdir(parents=True, exist_ok=True)
    dest = files / path.name
    n = 1
    while os.path.lexists(dest) or (info / f"{dest.name}.trashinfo").exists():
        n += 1
        dest = files / f"{path.name}.{n}"
    info_path = info / f"{dest.name}.trashinfo"
    when = time.strftime("%Y-%m-%dT%H:%M:%S")
    original = Path(os.path.abspath(path.parent)).resolve() / path.name
    if target.topdir is not None:
        top = target.topdir.resolve()
        recorded = original.relative_to(top) if top in original.parents else original
    else:
        recorded = original
    info_path.write_text(f"[Trash Info]\nPath={quote(str(recorded), safe='/')}\nDeletionDate={when}\n",
                         encoding="utf-8")
    try:
        cross = target.kind == "home" and _device(path) != _device(root)
        try:
            if cross:
                raise OSError(errno.EXDEV, "the home trash is on another drive")
            os.rename(path, dest)
        except OSError as exc:
            if exc.errno != errno.EXDEV or path.is_symlink() or not path.is_file():
                raise
            from .moves import _copy_verified  # moves imports fs

            _copy_verified(path, dest)
            try:
                path.unlink()
            except BaseException:
                dest.unlink(missing_ok=True)  # the original stays; don't leave a second copy behind
                raise
    except BaseException:
        info_path.unlink(missing_ok=True)
        raise
    return Trashed(dest, target)
