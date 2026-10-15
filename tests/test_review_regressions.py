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


def test_review1_interrupted_cross_device_dir_move_is_fully_undoable(dt, tmp_path, exdev, monkeypatch):
    src = tmp_path / "a" / "Scans"
    src.mkdir(parents=True)
    for name in ("1-front.jpg", "2-back.jpg", "3-disc.jpg"):
        (src / name).write_bytes(name.encode())
    real_copy = dt._copy_verified
    calls = {"n": 0}

    def dies_on_second(a, b):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_copy(a, b)

    monkeypatch.setattr(dt, "_copy_verified", dies_on_second)
    journal = dt.Journal("apply")
    with pytest.raises(OSError):
        dt._move(src, tmp_path / "b" / "Scans", journal)
    journal.close()
    assert (tmp_path / "b" / "Scans" / "1-front.jpg").exists()  # precondition: split state
    monkeypatch.setattr(dt, "_copy_verified", real_copy)
    dt.undo_journal(journal.path, apply=True)
    assert _tree(src) == ["1-front.jpg", "2-back.jpg", "3-disc.jpg"]
    assert _tree(tmp_path / "b") == []



def test_review1_complete_cross_device_dir_move_undoes_cleanly(dt, tmp_path, exdev):
    src = tmp_path / "a" / "Scans"
    (src / "inner").mkdir(parents=True)
    for name in ("1-front.jpg", "inner/2-back.jpg"):
        (src / name).write_bytes(name.encode())
    (src / "link.jpg").symlink_to("1-front.jpg")
    journal = dt.Journal("apply")
    dt._move(src, tmp_path / "b" / "Scans", journal)
    journal.close()
    assert not src.exists() and (tmp_path / "b" / "Scans" / "inner" / "2-back.jpg").exists()
    assert dt.undo_journal(journal.path, apply=False) == 0
    assert dt.undo_journal(journal.path, apply=True) == 0
    assert _tree(src) == ["1-front.jpg", "inner/2-back.jpg", "link.jpg"]
    assert (src / "link.jpg").is_symlink()
    assert not (tmp_path / "b").exists()
