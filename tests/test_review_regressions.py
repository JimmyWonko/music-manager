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


def test_review5_unnumbered_files_match_by_title_not_position(dt, tmp_path):
    src = make_album(tmp_path / "in" / "Album", ["Closing.flac", "Opening.flac"])
    album = next(a for a in dt.collect_albums(src) if a.path == src)
    rel = release("vinyl_lp")
    pairs, _ = dt.match_tracks(album, dt.flatten_tracklist(rel, dt.release_artists(rel)))
    got = {p.name: (t.title if t else None) for p, t in pairs}
    assert got == {"Closing.flac": "Closing", "Opening.flac": "Opening"}


def test_review5_unmatched_file_is_not_given_an_arbitrary_track(dt, tmp_path):
    src = make_album(tmp_path / "in" / "Album", ["01 track.flac", "bonus demo.flac"])
    album = next(a for a in dt.collect_albums(src) if a.path == src)
    rel = release("vinyl_lp")
    pairs, _ = dt.match_tracks(album, dt.flatten_tracklist(rel, dt.release_artists(rel)))
    got = {p.name: (t.title if t else None) for p, t in pairs}
    assert got["bonus demo.flac"] is None
