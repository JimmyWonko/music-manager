"""Moving albums leaves nothing behind: empty-folder cleanup and true cross-drive moves."""

import errno
import hashlib
import os

import pytest
from conftest import release
from test_apply import VINYL_DIR, make_album, run_apply

from music_manager import moves
from music_manager.cli import discogs_tag as cli
from music_manager.cli.discogs_tag import main
from music_manager.fs import collect_albums
from music_manager.moves import _prune_empty, prune_limit


class FakeDiscogs:
    def __init__(self, *args):
        pass

    def search_releases(self, artist, album, catno="", year=""):
        return []

    def release(self, release_id):
        return release({1: "vinyl_lp", 2: "multi_cd", 3: "cassette"}[release_id])

    def master(self, master_id):
        return {"year": 1985}


def fake_choose(api, guess, results, auto, release_id):
    album = guess["album"]
    return api.release(1 if "Side" in album else 2 if "Double" in album else 3)


@pytest.fixture
def fake_api(monkeypatch):
    monkeypatch.setattr(cli, "Discogs", FakeDiscogs)
    monkeypatch.setattr(cli, "load_token", lambda: ("t", None, None))
    monkeypatch.setattr(cli, "choose_release", fake_choose)


def files_under(folder):
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())


def add_extras(album):
    (album / "cover.jpg").write_bytes(b"jpg")
    (album / "rip.log").write_text("log")
    (album / ".hidden-notes").write_text("keep me")
    (album / "Artwork").mkdir()
    (album / "Artwork" / "front.jpg").write_bytes(b"front")
    (album / ".AppleDouble").mkdir()
    (album / ".AppleDouble" / "x").write_bytes(b"x")


def test_his_scenario_artist_folder_outside_dest(tmp_path, fake_api, capsys):
    """~/bf-test/Black Flag passed as the argument, --dest ~/bf-out: nothing may stay behind."""
    home = tmp_path / "home"
    arg = home / "bf-test" / "Black Flag"
    out = home / "bf-out"
    side = make_album(arg / "Testers - Side Story", [f"{n:02d} track.flac" for n in range(1, 11)])
    add_extras(side)
    double = make_album(arg / "Band A - Double Feature", ["CD1/01 a.flac", "CD1/02 b.flac", "CD2/01 c.flac", "CD2/02 d.flac"])
    (double / "CD1" / "disc.cue").write_text("cue")
    before = len(files_under(arg))

    code = main([str(arg), "--dest", str(out), "--auto", "--yes"])
    text = capsys.readouterr().out
    assert code == 0, text
    assert not arg.exists()  # the argument folder itself is removed once empty
    assert (home / "bf-test").is_dir() and not any((home / "bf-test").iterdir())  # but nothing above it
    moved = files_under(out)
    assert len(moved) == before
    target = out / VINYL_DIR
    for name in (".hidden-notes", "cover.jpg", "rip.log", "Artwork/front.jpg", ".AppleDouble/x"):
        assert (target / name).is_file(), name
    assert sum(1 for p in moved if p.endswith(".flac")) == 14
    assert "removed empty folder" in text


def test_folder_with_hidden_leftover_is_never_removed(tmp_path, fake_api):
    arg = tmp_path / "bf-test" / "Black Flag"
    side = make_album(arg / "Testers - Side Story", [f"{n:02d} track.flac" for n in range(1, 11)])
    (side / ".Trash-1000").mkdir()  # trash never travels with an album
    (side / ".Trash-1000" / "old.flac.bak").write_bytes(b"x")
    assert main([str(arg), "--dest", str(tmp_path / "lib"), "--yes"]) == 0
    assert files_under(side) == [".Trash-1000/old.flac.bak"]
    assert arg.is_dir()


def test_unmatched_track_keeps_source_folder(tmp_path):
    # ten titled files and a bonus one: the ten titles pair (so the disc is not held for its count)
    titles = ["Opening", "Second", "Third", "Fourth", "Fifth", "Flip", "Seventh", "Eighth", "Ninth", "Closing"]
    names = [f"{n:02d} {t}.flac" for n, t in enumerate(titles, 1)] + ["11 bonus.flac"]
    src = make_album(tmp_path / "in" / "Testers - Side Story", names)
    run_apply(src, tmp_path / "lib", release("vinyl_lp"), scan_root=tmp_path / "in")
    assert [p.name for p in src.iterdir()] == ["11 bonus.flac"]  # left in place on purpose, folder kept


def test_in_place_rename_removes_old_folder_keeps_artist(tmp_path):
    lib = tmp_path / "lib"
    old = make_album(lib / "Testers, The" / "[1985]  Side Story [1985] [WAX001]", [f"{n}. t.flac" for n in range(1, 11)])
    (lib / "Testers, The" / "[1979]  Other" / "x.flac").parent.mkdir(parents=True)
    run_apply(old, lib, release("vinyl_lp"), scan_root=lib)
    assert not old.exists()
    assert (lib / VINYL_DIR / "A1. Opening.flac").is_file()
    assert sorted(p.name for p in (lib / "Testers, The").iterdir()) == ["[1979]  Other", "[1985]  Side Story [WAX001]"]


def test_album_passed_inside_library_is_removed_library_root_kept(tmp_path):
    lib = tmp_path / "lib"
    loose = make_album(lib / "Testers - Side Story", [f"{n:02d} track.flac" for n in range(1, 11)])
    run_apply(loose, lib, release("vinyl_lp"), scan_root=loose)
    assert not loose.exists() and lib.is_dir()
    assert sorted(p.name for p in lib.iterdir()) == ["Testers, The"]


def test_emptied_artist_folder_removed_when_library_root_passed(tmp_path):
    lib = tmp_path / "lib"
    old = make_album(lib / "Old Artist Name" / "Side Story", [f"{n:02d} track.flac" for n in range(1, 11)])
    run_apply(old, lib, release("vinyl_lp"), scan_root=lib)
    assert not (lib / "Old Artist Name").exists()
    assert lib.is_dir() and (lib / VINYL_DIR).is_dir()


def test_prune_empty_rules(tmp_path):
    top = tmp_path / "top"
    deep = top / "a" / "b" / "album"
    (deep / "CD1" / "empty").mkdir(parents=True)
    (top / "a" / "keep").mkdir()
    (top / "a" / "keep" / ".hidden").write_text("x")
    removed = _prune_empty(deep, top)
    assert not (top / "a" / "b").exists()  # album, its empty subfolders and empty parent
    assert (top / "a" / "keep" / ".hidden").exists() and (top / "a").is_dir()
    assert len(removed) == 4

    # A folder holding only a hidden file is never removed.
    lone = tmp_path / "lone" / "album"
    lone.mkdir(parents=True)
    (lone / ".DS_Store").write_bytes(b"x")
    assert _prune_empty(lone, tmp_path / "lone") == []
    assert (lone / ".DS_Store").exists()

    # The limit itself may go; nothing above it; protected folders never.
    solo = tmp_path / "solo" / "arg" / "album"
    solo.mkdir(parents=True)
    _prune_empty(solo, tmp_path / "solo" / "arg")
    assert not (tmp_path / "solo" / "arg").exists() and (tmp_path / "solo").is_dir()
    kept = tmp_path / "kept" / "album"
    kept.mkdir(parents=True)
    _prune_empty(kept, tmp_path / "kept", protect=[tmp_path / "kept" / "album" / "new"])
    assert kept.is_dir()


def test_prune_limit(tmp_path):
    lib, ext = tmp_path / "lib", tmp_path / "home" / "bf-test" / "Black Flag"
    assert prune_limit(ext / "A", lib, ext) == ext
    assert prune_limit(ext, lib, ext) == ext
    assert prune_limit(lib / "Artist" / "A", lib, lib) == lib  # lib itself is protected in _prune_empty
    assert prune_limit(lib / "Artist" / "A", lib, None) == lib / "Artist"
    assert prune_limit(ext / "A", lib, None) == ext / "A"


@pytest.fixture
def cross_device(monkeypatch):
    """Make every plain rename fail with EXDEV, like a move to another drive."""
    real = os.rename

    def fake(src, dst, *a, **k):
        if ".discogs-tag-partial-" in str(src):
            return real(src, dst, *a, **k)
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "rename", fake)


def test_cross_device_file_move_verifies_and_deletes_source(tmp_path, cross_device):
    src = tmp_path / "a" / "song.flac"
    src.parent.mkdir()
    src.write_bytes(os.urandom(300_000))
    digest = hashlib.sha256(src.read_bytes()).hexdigest()
    os.utime(src, (1_000_000_000, 1_000_000_000))
    dest = moves._move(src, tmp_path / "b" / "song.flac")
    assert not src.exists()
    assert hashlib.sha256(dest.read_bytes()).hexdigest() == digest
    assert int(dest.stat().st_mtime) == 1_000_000_000
    assert [p.name for p in dest.parent.iterdir()] == ["song.flac"]  # no partial file left


def test_cross_device_dir_move(tmp_path, cross_device):
    src = tmp_path / "a" / "Scans"
    (src / "inner").mkdir(parents=True)
    (src / "front.jpg").write_bytes(b"front")
    (src / "inner" / ".hidden").write_bytes(b"h")
    dest = moves._move(src, tmp_path / "b" / "Scans")
    assert not src.exists()
    assert files_under(dest) == ["front.jpg", "inner/.hidden"]


def test_cross_device_verification_failure_keeps_source(tmp_path, cross_device, monkeypatch):
    src = tmp_path / "a" / "song.flac"
    src.parent.mkdir()
    src.write_bytes(b"audio" * 1000)
    real_sha = hashlib.sha256
    made = []

    def corrupting_sha256(*args):
        h = real_sha(*args)
        made.append(h)
        if len(made) == 2:  # the read-back of the copy "differs"
            h.update(b"corruption")
        return h

    monkeypatch.setattr(hashlib, "sha256", corrupting_sha256)
    with pytest.raises(OSError, match="verification failed"):
        moves._move(src, tmp_path / "b" / "song.flac")
    assert src.read_bytes() == b"audio" * 1000
    assert not any((tmp_path / "b").iterdir())


def test_cross_device_album_apply_leaves_nothing(tmp_path, cross_device):
    arg = tmp_path / "home" / "bf-test" / "Black Flag"
    src = make_album(arg / "Testers - Side Story", [f"{n:02d} track.flac" for n in range(1, 11)])
    add_extras(src)
    run_apply(src, tmp_path / "drive" / "Music", release("vinyl_lp"), scan_root=arg)
    assert not arg.exists()
    assert len(files_under(tmp_path / "drive" / "Music" / VINYL_DIR)) == 15


def test_source_checkout_is_not_an_album(tmp_path):
    root = tmp_path / "Black Flag"
    make_album(root / "music-manager" / "tests" / "samples", ["silent.flac"])
    (root / "music-manager" / ".git").mkdir()
    make_album(root / "My War", ["1 a.flac"])
    assert [a.path.name for a in collect_albums(root)] == ["My War"]
