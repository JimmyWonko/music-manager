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


# (10) pid-named partial file ----------------------------------------------------
def test_review10_stale_partial_with_same_pid_does_not_block_copy(dt, tmp_path):
    src = tmp_path / "a" / "song.flac"
    src.parent.mkdir()
    src.write_bytes(b"audio" * 100)
    dest = tmp_path / "b" / "song.flac"
    dest.parent.mkdir()
    (dest.parent / f".{dest.name}.discogs-tag-partial-{os.getpid()}").write_bytes(b"stale")
    dt._copy_verified(src, dest)
    assert dest.read_bytes() == b"audio" * 100
