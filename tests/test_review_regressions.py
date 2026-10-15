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


# (6) --skip-tagged checks only the first track --------------------------------
def test_review6_skip_tagged_does_not_skip_partially_tagged_album(dt, tmp_path):
    src = make_album(tmp_path / "in" / "Testers - Side Story", ["01 a.flac", "02 b.flac", "03 c.flac"])
    first = FLAC(src / "01 a.flac")
    first["DISCOGS_RELEASE_ID"] = ["1000001"]
    first.save()  # only track 1 is tagged
    album = next(a for a in dt.collect_albums(src) if a.path == src)

    class NoResults:
        def search_releases(self, *a, **k):
            return []

    result = dt.process_album(NoResults(), album, tmp_path / "lib", dry_run=True, auto=True,
                              yes=True, skip_tagged=True, release_id=None)
    assert result != "skipped-tagged"


def test_review6_skip_tagged_skips_only_when_all_tracks_share_one_id(dt, tmp_path, capsys):
    src = make_album(tmp_path / "in" / "Testers - Side Story", ["01 a.flac", "02 b.flac"])
    for name, rid in (("01 a.flac", "1000001"), ("02 b.flac", "1000001")):
        f = FLAC(src / name)
        f["DISCOGS_RELEASE_ID"] = [rid]
        f.save()
    album = next(a for a in dt.collect_albums(src) if a.path == src)

    class NoResults:
        def search_releases(self, *a, **k):
            return []

    kw = dict(dry_run=True, auto=True, yes=True, skip_tagged=True, release_id=None)
    assert dt.process_album(NoResults(), album, tmp_path / "lib", **kw) == "skipped-tagged"
    f = FLAC(src / "02 b.flac")
    f["DISCOGS_RELEASE_ID"] = ["2000002"]
    f.save()
    assert dt.process_album(NoResults(), album, tmp_path / "lib", **kw) != "skipped-tagged"
    assert "mixed tags" in capsys.readouterr().out


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
