"""Moves: verified copy/move (also across drives), journaled renames, staging, and empty-folder
cleanup after an album has moved.
"""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .fs import COPY_CHUNK, file_sha256, send_to_trash
from .tags.fields import TrackMeta

if TYPE_CHECKING:
    from .journal import Journal, JournalEntry


@dataclass
class PlannedFile:
    source: Path
    dest: Path
    meta: TrackMeta | None = None  # None for sidecars and extra folders


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


def _journal_rename(journal: "Journal | None", kind: str, src: Path, dest: Path) -> None:
    """Record a rename before it happens, including the file's current hash."""
    if journal is None:
        return
    st = os.lstat(src)
    entry: JournalEntry = {"action": "rename", "kind": kind, "old": str(src), "new": str(dest), "dev": st.st_dev, "ino": st.st_ino}
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
    trashed = send_to_trash(path)
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
