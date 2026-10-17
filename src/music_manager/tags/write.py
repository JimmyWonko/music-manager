"""write_tags: atomic per-format tag writes.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen.aiff import AIFF
from mutagen.apev2 import APEv2File
from mutagen.asf import ASF
from mutagen.flac import FLAC
from mutagen.id3 import ID3, ID3NoHeaderError
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.oggflac import OggFLAC
from mutagen.oggopus import OggOpus
from mutagen.oggspeex import OggSpeex
from mutagen.oggvorbis import OggVorbis
from mutagen.wave import WAVE

from ..fs import COPY_CHUNK
from .ape import _write_ape
from .asf import _write_asf
from .fields import ALWAYS_REPLACED, ORIGINAL_FIELDS, RELEASE_FIELDS, TrackMeta, tag_values
from .id3 import _apply_id3
from .mp4 import _write_mp4_tags
from .read import already_tagged
from .vorbis import _write_vorbis


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


def write_tags(path: Path, meta: TrackMeta, clear_other: bool = False, dry_run: bool = False) -> list[str]:
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


def _write_tags_file(path: Path, meta: TrackMeta, clear_other: bool = False, dry_run: bool = False,
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
