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

from music_manager import moves
from music_manager.apply import AlbumApplyError, validate_layout
from music_manager.cli.discogs_tag import choose_release, process_album
from music_manager.fs import collect_albums, file_sha256
from music_manager.journal import Journal, undo_journal
from music_manager.match import AUTO_MIN_MARGIN, guess_from_album_dir, match_tracks, score_result
from music_manager.providers.discogs import Discogs, DiscogsError, flatten_tracklist, release_artists
from music_manager.tags.snapshot import LegacySnapshotError, capture_tags, restore_tags
from music_manager.tags.write import write_tags


@pytest.fixture
def exdev(monkeypatch):
    """Every plain rename fails with EXDEV, like a move to another drive."""
    real = os.rename

    def fake(src, dst, *a, **k):
        if ".discogs-tag-partial-" in str(src):
            return real(src, dst, *a, **k)
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "rename", fake)


def _tree(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()) if root.exists() else []


# (10) pid-named partial file ----------------------------------------------------
def test_review10_stale_partial_with_same_pid_does_not_block_copy(tmp_path):
    src = tmp_path / "a" / "song.flac"
    src.parent.mkdir()
    src.write_bytes(b"audio" * 100)
    dest = tmp_path / "b" / "song.flac"
    dest.parent.mkdir()
    (dest.parent / f".{dest.name}.discogs-tag-partial-{os.getpid()}").write_bytes(b"stale")
    moves._copy_verified(src, dest)
    assert dest.read_bytes() == b"audio" * 100


def test_review9_crash_during_tag_save_leaves_original_intact(sample, meta, monkeypatch):
    path = sample("flac")
    original = path.read_bytes()

    def crashing_save(self, *a, **k):
        target = getattr(self, "filename", None) or a[0]
        with open(target, "r+b") as fh:
            fh.truncate(16)  # half-written file
        raise OSError(errno.EIO, "simulated crash during save")

    monkeypatch.setattr(FLAC, "save", crashing_save)
    with pytest.raises(OSError):
        write_tags(path, meta)
    assert path.read_bytes() == original



def test_review9_atomic_save_keeps_mode_link_and_leaves_no_temp(sample, meta):
    path = sample("flac")
    os.chmod(path, 0o640)
    link = path.with_name("link.flac")
    link.symlink_to(path.name)
    ino = os.stat(path).st_ino
    write_tags(link, meta)
    assert link.is_symlink() and FLAC(path)["TITLE"]
    assert (os.stat(path).st_mode & 0o777) == 0o640
    assert os.stat(path).st_ino != ino  # replaced, not edited in place
    assert sorted(p.name for p in path.parent.iterdir() if ".discogs-tag-" in p.name) == []


def test_review9_crash_during_restore_leaves_file_intact(sample, meta, monkeypatch):
    path = sample("flac")
    snap = capture_tags(path)
    write_tags(path, meta)
    tagged = path.read_bytes()

    def crashing_save(self, *a, **k):
        with open(self.filename, "r+b") as fh:
            fh.truncate(16)
        raise OSError(errno.EIO, "simulated crash during save")

    monkeypatch.setattr(FLAC, "save", crashing_save)
    with pytest.raises(OSError):
        restore_tags(path, snap)
    assert path.read_bytes() == tagged
    assert [p.name for p in path.parent.iterdir() if ".discogs-tag-" in p.name] == []


# (7) Non-atomic cache writes, no recovery from corrupt cache -----------------
class _Resp:
    status_code = 200
    headers: dict = {}

    def json(self):
        return {"ok": True}


def test_review7_corrupt_cache_file_is_refetched(monkeypatch):
    client = Discogs("token", None, None)
    client.min_interval = 0
    monkeypatch.setattr(client.session, "get", lambda *a, **kw: _Resp())
    client._cache_path("release", "1").write_text('{"truncat', encoding="utf-8")
    assert client._get("https://example.invalid/x", None, "release", "1") == {"ok": True}


def test_review7_interrupted_cache_write_leaves_no_partial_json(monkeypatch):
    client = Discogs("token", None, None)
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


def test_review4_auto_mode_refuses_track_count_mismatch(tmp_path, monkeypatch):
    global dt_error
    dt_error = DiscogsError
    src = make_album(tmp_path / "in" / "Testers" / "[1985]  Side Story [WAX001]", ["01 track.flac", "02 track.flac", "03 track.flac"])
    album = next(a for a in collect_albums(src) if a.path == src)
    guess = guess_from_album_dir(album.path, album.tracks)
    hit = {"id": 1000001, "title": f"{guess['artist']} - {guess['album']}", "catno": guess.get("catno") or "WAX001", "year": guess.get("year") or "1985"}
    assert score_result(hit, guess) >= 0.90  # precondition: the search hit alone is "confident"
    result = process_album(FakeApi(release("vinyl_lp"), hit), album, tmp_path / "lib", dry_run=False, auto=True,
                              yes=True, skip_tagged=False, release_id=None)
    assert result == "skipped"  # 3 local files vs 10 Discogs tracks must not auto-apply
    assert _tree(src) == ["01 track.flac", "02 track.flac", "03 track.flac"]


def test_review4_auto_needs_clear_margin_over_runner_up():
    guess = {"artist": "Testers", "album": "Side Story", "catno": "WAX001", "year": "1985"}
    best = {"id": 1, "title": "Testers - Side Story", "catno": "WAX001", "year": "1985"}
    close = {"id": 2, "title": "Testers - Side Story", "catno": "WAX001", "year": "1986"}

    class Api:
        def release(self, _id):
            raise AssertionError("must not auto-pick")

    assert score_result(best, guess) - score_result(close, guess) < AUTO_MIN_MARGIN
    assert choose_release(Api(), guess, [best, close], auto=True, release_id=None) is None


def test_review4_allow_track_mismatch_flag_applies(tmp_path):
    src = make_album(tmp_path / "in" / "Testers" / "[1985]  Side Story [WAX001]", ["01 track.flac", "02 track.flac", "03 track.flac"])
    album = next(a for a in collect_albums(src) if a.path == src)
    guess = guess_from_album_dir(album.path, album.tracks)
    hit = {"id": 1000001, "title": f"{guess['artist']} - {guess['album']}", "catno": guess.get("catno") or "WAX001", "year": guess.get("year") or "1985"}
    global dt_error
    dt_error = DiscogsError
    result = process_album(FakeApi(release("vinyl_lp"), hit), album, tmp_path / "lib", dry_run=True, auto=True,
                              yes=True, skip_tagged=False, release_id=None, allow_track_mismatch=True)
    assert result == "applied"


def test_review5_unnumbered_files_match_by_title_not_position(tmp_path):
    src = make_album(tmp_path / "in" / "Album", ["Closing.flac", "Opening.flac"])
    album = next(a for a in collect_albums(src) if a.path == src)
    rel = release("vinyl_lp")
    pairs, _ = match_tracks(album, flatten_tracklist(rel, release_artists(rel)))
    got = {p.name: (t.title if t else None) for p, t in pairs}
    assert got == {"Closing.flac": "Closing", "Opening.flac": "Opening"}


def test_review5_unmatched_file_is_not_given_an_arbitrary_track(tmp_path):
    src = make_album(tmp_path / "in" / "Album", ["01 track.flac", "bonus demo.flac"])
    album = next(a for a in collect_albums(src) if a.path == src)
    rel = release("vinyl_lp")
    pairs, _ = match_tracks(album, flatten_tracklist(rel, release_artists(rel)))
    got = {p.name: (t.title if t else None) for p, t in pairs}
    assert got["bonus demo.flac"] is None


# (6) --skip-tagged checks only the first track --------------------------------
def test_review6_skip_tagged_does_not_skip_partially_tagged_album(tmp_path):
    src = make_album(tmp_path / "in" / "Testers - Side Story", ["01 a.flac", "02 b.flac", "03 c.flac"])
    first = FLAC(src / "01 a.flac")
    first["DISCOGS_RELEASE_ID"] = ["1000001"]
    first.save()  # only track 1 is tagged
    album = next(a for a in collect_albums(src) if a.path == src)

    class NoResults:
        def search_releases(self, *a, **k):
            return []

    result = process_album(NoResults(), album, tmp_path / "lib", dry_run=True, auto=True,
                              yes=True, skip_tagged=True, release_id=None)
    assert result != "skipped-tagged"


def test_review6_skip_tagged_skips_only_when_all_tracks_share_one_id(tmp_path, capsys):
    src = make_album(tmp_path / "in" / "Testers - Side Story", ["01 a.flac", "02 b.flac"])
    for name, rid in (("01 a.flac", "1000001"), ("02 b.flac", "1000001")):
        f = FLAC(src / name)
        f["DISCOGS_RELEASE_ID"] = [rid]
        f.save()
    album = next(a for a in collect_albums(src) if a.path == src)

    class NoResults:
        def search_releases(self, *a, **k):
            return []

    kw = dict(dry_run=True, auto=True, yes=True, skip_tagged=True, release_id=None)
    assert process_album(NoResults(), album, tmp_path / "lib", **kw) == "skipped-tagged"
    f = FLAC(src / "02 b.flac")
    f["DISCOGS_RELEASE_ID"] = ["2000002"]
    f.save()
    assert process_album(NoResults(), album, tmp_path / "lib", **kw) != "skipped-tagged"
    assert "mixed tags" in capsys.readouterr().out


# (11) dead code ------------------------------------------------------------------
@pytest.mark.parametrize("name", ["MASTER_URL", "is_sidecar", "_write_mp3", "_write_mp4"])
def test_review11_dead_code_removed(name):
    import importlib
    import pkgutil

    import music_manager

    for info in pkgutil.walk_packages(music_manager.__path__, "music_manager."):
        module = importlib.import_module(info.name)
        assert not hasattr(module, name), f"{info.name}.{name} is unused and should be removed"


def test_review1_interrupted_cross_device_dir_move_is_fully_undoable(tmp_path, exdev, monkeypatch):
    src = tmp_path / "a" / "Scans"
    src.mkdir(parents=True)
    for name in ("1-front.jpg", "2-back.jpg", "3-disc.jpg"):
        (src / name).write_bytes(name.encode())
    real_copy = moves._copy_verified
    calls = {"n": 0}

    def dies_on_second(a, b):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_copy(a, b)

    monkeypatch.setattr(moves, "_copy_verified", dies_on_second)
    journal = Journal("apply")
    with pytest.raises(OSError):
        moves._move(src, tmp_path / "b" / "Scans", journal)
    journal.close()
    assert (tmp_path / "b" / "Scans" / "1-front.jpg").exists()  # precondition: split state
    monkeypatch.setattr(moves, "_copy_verified", real_copy)
    undo_journal(journal.path, apply=True)
    assert _tree(src) == ["1-front.jpg", "2-back.jpg", "3-disc.jpg"]
    assert _tree(tmp_path / "b") == []


def test_review1_complete_cross_device_dir_move_undoes_cleanly(tmp_path, exdev):
    src = tmp_path / "a" / "Scans"
    (src / "inner").mkdir(parents=True)
    for name in ("1-front.jpg", "inner/2-back.jpg"):
        (src / name).write_bytes(name.encode())
    (src / "link.jpg").symlink_to("1-front.jpg")
    journal = Journal("apply")
    moves._move(src, tmp_path / "b" / "Scans", journal)
    journal.close()
    assert not src.exists() and (tmp_path / "b" / "Scans" / "inner" / "2-back.jpg").exists()
    assert undo_journal(journal.path, apply=False) == 0
    assert undo_journal(journal.path, apply=True) == 0
    assert _tree(src) == ["1-front.jpg", "inner/2-back.jpg", "link.jpg"]
    assert (src / "link.jpg").is_symlink()
    assert not (tmp_path / "b").exists()


def test_review2_dest_inside_source_album_is_refused(tmp_path):
    src = make_album(tmp_path / "in" / "Testers - Side Story", [f"{n:02d} track.flac" for n in range(1, 11)])
    before = _tree(src)
    with pytest.raises((ValueError, AlbumApplyError)):
        run_apply(src, src / "library", release("vinyl_lp"))
    assert _tree(src) == before


def test_review2_symlinked_track_escaping_source_is_refused(tmp_path):
    outside = make_album(tmp_path / "elsewhere", ["01 track.flac"])
    src = make_album(tmp_path / "in" / "Testers - Side Story", [f"{n:02d} track.flac" for n in range(2, 11)])
    (src / "01 track.flac").symlink_to(outside / "01 track.flac")
    outside_bytes = (outside / "01 track.flac").read_bytes()
    with pytest.raises((ValueError, AlbumApplyError)):
        run_apply(src, tmp_path / "lib", release("vinyl_lp"))
    assert (outside / "01 track.flac").read_bytes() == outside_bytes  # file outside the tree not re-tagged


def test_review2_target_containing_source_is_refused(tmp_path):
    lib = tmp_path / "lib"
    # The album sits inside the folder it would be moved into.
    src = make_album(lib / "Testers, The" / "[1985]  Side Story [WAX001]" / "old rip", ["01 track.flac"])
    with pytest.raises(AlbumApplyError, match="contains the source album"):
        run_apply(src, lib, release("vinyl_lp"))
    assert _tree(src) == ["01 track.flac"]


def test_review2_symlink_inside_album_is_allowed(tmp_path):
    src = make_album(tmp_path / "in" / "Testers - Side Story", [f"{n:02d} track.flac" for n in range(1, 11)])
    (src / "Scans").mkdir()
    (src / "Scans" / "front.jpg").write_bytes(b"jpg")
    (src / "cover.jpg").symlink_to("Scans/front.jpg")
    validate_layout(next(a for a in collect_albums(src) if a.path == src), tmp_path / "lib", tmp_path / "lib" / "x")


class _Evil:
    def __init__(self, marker):
        self.marker = marker

    def __reduce__(self):
        return (os.mkdir, (self.marker,))


def test_review8_restore_never_unpickles_journal_data(sample, tmp_path):
    path = sample("flac")
    marker = tmp_path / "pwned"
    evil = {"kind": "map", "data": base64.b64encode(pickle.dumps([_Evil(str(marker))])).decode("ascii")}
    try:
        restore_tags(path, evil)
    except Exception:  # noqa: BLE001 - rejecting the snapshot is fine
        pass
    assert not marker.exists(), "journal snapshot data was unpickled (arbitrary code ran)"


def test_review8_snapshot_is_plain_json_not_pickle(sample, meta):
    path = sample("flac")
    write_tags(path, meta)
    snap = capture_tags(path)
    data = snap.get("data")
    if isinstance(data, str):
        try:
            raw = base64.b64decode(data, validate=True)
        except Exception:  # noqa: BLE001
            raw = b""
        assert not raw.startswith(b"\x80"), "snapshot payload is a pickle stream"


FORMATS = ["flac", "mp3", "m4a", "ogg", "opus", "wma", "wv", "aiff", "wav"]


def _dump(path):
    from mutagen import File as MutagenFile
    from mutagen.id3 import ID3
    if path.suffix == ".mp3":
        return ID3(path).pprint()
    return MutagenFile(path).tags.pprint()


@pytest.mark.parametrize("ext", FORMATS)
def test_review8_json_snapshot_keeps_binary_art(sample, meta, ext):
    from mutagen import File as MutagenFile
    from mutagen.apev2 import BINARY, APEValue
    from mutagen.asf import ASFByteArrayAttribute
    from mutagen.flac import Picture
    from mutagen.id3 import APIC, ID3
    from mutagen.mp4 import MP4Cover
    path = sample(ext)
    write_tags(path, meta)
    art = b"\x89PNG\r\n\x1a\n\x00\xff" * 4
    if ext == "mp3":
        tags = ID3(path)
        tags.add(APIC(encoding=0, mime="image/png", type=3, desc="", data=art))
        tags.save(path, v2_version=3)
    else:
        audio = MutagenFile(path)
        if ext in ("aiff", "wav"):
            audio.tags.add(APIC(encoding=0, mime="image/png", type=3, desc="", data=art))
        elif ext == "m4a":
            audio.tags["covr"] = [MP4Cover(art, imageformat=MP4Cover.FORMAT_PNG)]
        elif ext == "wma":
            audio.tags["WM/Picture"] = [ASFByteArrayAttribute(art)]
        elif ext == "wv":
            audio.tags["Cover Art (Front)"] = APEValue(b"cover.png\x00" + art, BINARY)
        elif ext == "flac":
            pic = Picture()
            pic.data, pic.mime, pic.type = art, "image/png", 3
            audio.add_picture(pic)
        else:
            audio.tags["METADATA_BLOCK_PICTURE"] = [base64.b64encode(art).decode()]
        audio.save()
    before, raw = _dump(path), path.read_bytes()
    snap = json.loads(json.dumps(capture_tags(path)))
    assert "data" not in snap and snap.get("v") == 2
    write_tags(path, {**meta, "title": "Changed"}, clear_other=True)
    if ext == "m4a":
        MutagenFile(path).delete()
    restore_tags(path, snap)
    assert _dump(path) == before
    if ext == "mp3":
        assert ID3(path).version[:2] == (2, 3)  # v2.3 file stays v2.3
        assert path.read_bytes() == raw


def test_review8_legacy_pickle_journal_needs_explicit_trust(sample, meta, tmp_path):
    path = sample("flac")
    write_tags(path, {**meta, "title": "Old"})
    from mutagen.flac import FLAC
    legacy = {"kind": "list", "data": base64.b64encode(pickle.dumps(list(list.__iter__(FLAC(path).tags)))).decode()}
    with pytest.raises(LegacySnapshotError):
        restore_tags(path, legacy)
    write_tags(path, {**meta, "title": "New"})
    sha = file_sha256(path)
    journal = tmp_path / "old.jsonl"
    journal.write_text(
        json.dumps({"action": "tags", "phase": "before", "path": str(path), "snapshot": legacy}) + "\n"
        + json.dumps({"action": "tags", "phase": "after", "path": str(path), "sha256": sha}) + "\n")
    assert undo_journal(journal, apply=True) == 1  # skipped, not loaded
    assert FLAC(path)["TITLE"] == ["New"]
    assert undo_journal(journal, apply=True, trust_legacy=True) == 0
    assert FLAC(path)["TITLE"] == ["Old"]
