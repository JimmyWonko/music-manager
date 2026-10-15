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


def test_review2_dest_inside_source_album_is_refused(dt, tmp_path):
    src = make_album(tmp_path / "in" / "Testers - Side Story", [f"{n:02d} track.flac" for n in range(1, 11)])
    before = _tree(src)
    with pytest.raises((ValueError, dt.AlbumApplyError)):
        run_apply(dt, src, src / "library", release("vinyl_lp"))
    assert _tree(src) == before


def test_review2_symlinked_track_escaping_source_is_refused(dt, tmp_path):
    outside = make_album(tmp_path / "elsewhere", ["01 track.flac"])
    src = make_album(tmp_path / "in" / "Testers - Side Story", [f"{n:02d} track.flac" for n in range(2, 11)])
    (src / "01 track.flac").symlink_to(outside / "01 track.flac")
    outside_bytes = (outside / "01 track.flac").read_bytes()
    with pytest.raises((ValueError, dt.AlbumApplyError)):
        run_apply(dt, src, tmp_path / "lib", release("vinyl_lp"))
    assert (outside / "01 track.flac").read_bytes() == outside_bytes  # file outside the tree not re-tagged



def test_review2_target_containing_source_is_refused(dt, tmp_path):
    lib = tmp_path / "lib"
    # The album sits inside the folder it would be moved into.
    src = make_album(lib / "Testers, The" / "[1985]  Side Story [WAX001]" / "old rip", ["01 track.flac"])
    with pytest.raises(dt.AlbumApplyError, match="contains the source album"):
        run_apply(dt, src, lib, release("vinyl_lp"))
    assert _tree(src) == ["01 track.flac"]


def test_review2_symlink_inside_album_is_allowed(dt, tmp_path):
    src = make_album(tmp_path / "in" / "Testers - Side Story", [f"{n:02d} track.flac" for n in range(1, 11)])
    (src / "Scans").mkdir()
    (src / "Scans" / "front.jpg").write_bytes(b"jpg")
    (src / "cover.jpg").symlink_to("Scans/front.jpg")
    dt.validate_layout(next(a for a in dt.collect_albums(src) if a.path == src), tmp_path / "lib", tmp_path / "lib" / "x")
