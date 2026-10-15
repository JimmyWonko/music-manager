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


# (11) dead code ------------------------------------------------------------------
@pytest.mark.parametrize("name", ["MASTER_URL", "is_sidecar", "_write_mp3", "_write_mp4"])
def test_review11_dead_code_removed(dt, name):
    assert not hasattr(dt, name), f"{name} is unused and should be removed"
