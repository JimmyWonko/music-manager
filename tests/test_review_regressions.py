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


# (4)+(5) Auto-match ignores track counts; leftovers assigned by position ------
class FakeApi:
    def __init__(self, rel, result):
        self.rel, self.result = rel, result

    def search_releases(self, *a, **k):
        return [self.result]

    def release(self, _id):
        return self.rel

    def master(self, _id):
        raise dt_error("no master in tests")


dt_error = RuntimeError


def test_review4_auto_mode_refuses_track_count_mismatch(dt, tmp_path, monkeypatch):
    global dt_error
    dt_error = dt.DiscogsError
    src = make_album(tmp_path / "in" / "Testers" / "[1985]  Side Story [WAX001]", ["01 track.flac", "02 track.flac", "03 track.flac"])
    album = next(a for a in dt.collect_albums(src) if a.path == src)
    guess = dt.guess_from_album_dir(album.path, album.tracks)
    hit = {"id": 1000001, "title": f"{guess['artist']} - {guess['album']}", "catno": guess.get("catno") or "WAX001", "year": guess.get("year") or "1985"}
    assert dt.score_result(hit, guess) >= 0.90  # precondition: the search hit alone is "confident"
    result = dt.process_album(FakeApi(release("vinyl_lp"), hit), album, tmp_path / "lib", dry_run=False, auto=True,
                              yes=True, skip_tagged=False, release_id=None)
    assert result == "skipped"  # 3 local files vs 10 Discogs tracks must not auto-apply
    assert _tree(src) == ["01 track.flac", "02 track.flac", "03 track.flac"]


def test_review4_auto_needs_clear_margin_over_runner_up(dt):
    guess = {"artist": "Testers", "album": "Side Story", "catno": "WAX001", "year": "1985"}
    best = {"id": 1, "title": "Testers - Side Story", "catno": "WAX001", "year": "1985"}
    close = {"id": 2, "title": "Testers - Side Story", "catno": "WAX001", "year": "1986"}

    class Api:
        def release(self, _id):
            raise AssertionError("must not auto-pick")

    assert dt.score_result(best, guess) - dt.score_result(close, guess) < dt.AUTO_MIN_MARGIN
    assert dt.choose_release(Api(), guess, [best, close], auto=True, release_id=None) is None


def test_review4_allow_track_mismatch_flag_applies(dt, tmp_path):
    src = make_album(tmp_path / "in" / "Testers" / "[1985]  Side Story [WAX001]", ["01 track.flac", "02 track.flac", "03 track.flac"])
    album = next(a for a in dt.collect_albums(src) if a.path == src)
    guess = dt.guess_from_album_dir(album.path, album.tracks)
    hit = {"id": 1000001, "title": f"{guess['artist']} - {guess['album']}", "catno": guess.get("catno") or "WAX001", "year": guess.get("year") or "1985"}
    global dt_error
    dt_error = dt.DiscogsError
    result = dt.process_album(FakeApi(release("vinyl_lp"), hit), album, tmp_path / "lib", dry_run=True, auto=True,
                              yes=True, skip_tagged=False, release_id=None, allow_track_mismatch=True)
    assert result == "applied"
