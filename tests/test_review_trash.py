"""Review item 3: the freedesktop trash. Path= in .trashinfo is percent-encoded (the spec's
URL escaping), and a trash on another drive gets the same verified copy as the audio mover."""

from __future__ import annotations

import errno
import os
import re
from urllib.parse import unquote

import pytest

from music_manager import fs
from music_manager import moves as moves_mod


@pytest.fixture
def trash(tmp_path, monkeypatch):
    root = tmp_path / "Trash"
    monkeypatch.setattr(fs, "TRASH_DIR", root)
    return root


def info_path_line(trash_root, trashed):
    text = (trash_root / "info" / f"{trashed.name}.trashinfo").read_text(encoding="utf-8")
    return next(line for line in text.splitlines() if line.startswith("Path="))[len("Path="):]


@pytest.mark.parametrize("name", ["My War.m3u", "100% Pure.m3u", "Who's Got the 10½?.m3u", "a#b&c;d.m3u"])
def test_trashinfo_path_is_percent_encoded(tmp_path, trash, name):
    src = tmp_path / "lib" / "Album [SST 023]" / name
    src.parent.mkdir(parents=True)
    src.write_text("1. a.flac\n", encoding="utf-8")
    trashed = fs.send_to_trash(src)
    raw = info_path_line(trash, trashed)
    assert re.fullmatch(r"[A-Za-z0-9_.~/%-]+", raw), raw  # everything else is %XX-escaped (UTF-8)
    assert unquote(raw) == str(src.resolve())  # decodes back to the exact original path
    assert raw.startswith("/") and "/" in raw[1:]  # slashes stay literal


def test_trashinfo_path_of_a_plain_name_is_unchanged(tmp_path, trash):
    src = tmp_path / "plain" / "list.m3u"
    src.parent.mkdir()
    src.write_text("x\n", encoding="utf-8")
    trashed = fs.send_to_trash(src)
    assert info_path_line(trash, trashed) == str(src.resolve())


def _exdev_for(path, monkeypatch):
    real = os.rename

    def rename(a, b, *args, **kwargs):
        if os.fspath(a) == os.fspath(path):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real(a, b, *args, **kwargs)

    monkeypatch.setattr(os, "rename", rename)


def test_trash_across_drives_uses_the_verified_copy(tmp_path, trash, monkeypatch):
    src = tmp_path / "lib" / "My War.m3u"
    src.parent.mkdir()
    src.write_bytes(b"1. My War.flac\n" * 100)
    _exdev_for(src, monkeypatch)
    calls = []
    real = moves_mod._copy_verified

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(moves_mod, "_copy_verified", spy)
    trashed = fs.send_to_trash(src)
    assert calls and calls[0][0] == src
    assert not src.exists() and trashed.read_bytes() == b"1. My War.flac\n" * 100


def test_a_failed_verified_copy_keeps_the_original_and_no_trash_entry(tmp_path, trash, monkeypatch):
    src = tmp_path / "lib" / "My War.m3u"
    src.parent.mkdir()
    src.write_bytes(b"data")
    _exdev_for(src, monkeypatch)

    def broken(a, b):
        raise OSError(errno.EIO, "copy verification failed")

    monkeypatch.setattr(moves_mod, "_copy_verified", broken)
    with pytest.raises(OSError, match="verification"):
        fs.send_to_trash(src)
    assert src.read_bytes() == b"data"
    assert list((trash / "info").iterdir()) == [] and list((trash / "files").iterdir()) == []
