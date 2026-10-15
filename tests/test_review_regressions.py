"""Regression tests for confirmed points of the external code review (Oct 2026).

Every test here FAILS on main (ac04a6d / tree 740b7d1) and should pass once
the matching fix lands. Everything runs inside tmp_path; no real library.
"""

import base64
import errno
import json
import os
import pickle
from pathlib import Path

import pytest
from conftest import release
from mutagen.flac import FLAC

from test_apply import make_album, run_apply


@pytest.fixture
def exdev(dt, monkeypatch):
    """Every plain rename fails with EXDEV, like a move to another drive."""
    real = os.rename

    def fake(src, dst, *a, **k):
        if ".discogs-tag-partial-" in str(src):
            return real(src, dst, *a, **k)
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(dt.os, "rename", fake)


def _tree(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()) if root.exists() else []


# (7) Non-atomic cache writes, no recovery from corrupt cache -----------------
class _Resp:
    status_code = 200
    headers: dict = {}

    def json(self):
        return {"ok": True}


def test_review7_corrupt_cache_file_is_refetched(dt, monkeypatch):
    client = dt.Discogs("token", None, None)
    client.min_interval = 0
    monkeypatch.setattr(client.session, "get", lambda *a, **kw: _Resp())
    client._cache_path("release", "1").write_text('{"truncat', encoding="utf-8")
    assert client._get("https://example.invalid/x", None, "release", "1") == {"ok": True}


def test_review7_interrupted_cache_write_leaves_no_partial_json(dt, monkeypatch):
    client = dt.Discogs("token", None, None)
    client.min_interval = 0
    monkeypatch.setattr(client.session, "get", lambda *a, **kw: _Resp())
    cache = client._cache_path("release", "2")

    def half_then_die(self, data, *a, **k):  # process killed mid-write
        with open(self, "w", encoding="utf-8") as fh:
            fh.write(data[: len(data) // 2])
        raise KeyboardInterrupt

    monkeypatch.setattr(Path, "write_text", half_then_die)
    try:
        client._get("https://example.invalid/x", None, "release", "2")
    except KeyboardInterrupt:
        pass
    monkeypatch.undo()
    assert not cache.exists() or json.loads(cache.read_text(encoding="utf-8")) == {"ok": True}


def test_review9_crash_during_tag_save_leaves_original_intact(dt, sample, meta, monkeypatch):
    path = sample("flac")
    original = path.read_bytes()

    def crashing_save(self, *a, **k):
        target = getattr(self, "filename", None) or a[0]
        with open(target, "r+b") as fh:
            fh.truncate(16)  # half-written file
        raise OSError(errno.EIO, "simulated crash during save")

    monkeypatch.setattr(FLAC, "save", crashing_save)
    with pytest.raises(OSError):
        dt.write_tags(path, meta)
    assert path.read_bytes() == original



def test_review9_atomic_save_keeps_mode_link_and_leaves_no_temp(dt, sample, meta):
    path = sample("flac")
    os.chmod(path, 0o640)
    link = path.with_name("link.flac")
    link.symlink_to(path.name)
    ino = os.stat(path).st_ino
    dt.write_tags(link, meta)
    assert link.is_symlink() and FLAC(path)["TITLE"]
    assert (os.stat(path).st_mode & 0o777) == 0o640
    assert os.stat(path).st_ino != ino  # replaced, not edited in place
    assert sorted(p.name for p in path.parent.iterdir() if ".discogs-tag-" in p.name) == []


def test_review9_crash_during_restore_leaves_file_intact(dt, sample, meta, monkeypatch):
    path = sample("flac")
    snap = dt.capture_tags(path)
    dt.write_tags(path, meta)
    tagged = path.read_bytes()

    def crashing_save(self, *a, **k):
        with open(self.filename, "r+b") as fh:
            fh.truncate(16)
        raise OSError(errno.EIO, "simulated crash during save")

    monkeypatch.setattr(FLAC, "save", crashing_save)
    with pytest.raises(OSError):
        dt.restore_tags(path, snap)
    assert path.read_bytes() == tagged
    assert [p.name for p in path.parent.iterdir() if ".discogs-tag-" in p.name] == []
