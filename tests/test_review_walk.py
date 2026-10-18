"""Review item 2: a folder with loose audio is not automatically the whole album.

a) Loose tracks next to CD1/ and CD2/ audio: tagging only the loose files and moving the disc
   folders as untagged extras is wrong; the album is held (layout_ambiguous) with all its audio.
b) A stray file in an artist folder must never make the artist folder an album that moves the
   real albums under it as extras: the albums below are found, the stray is held on its own.
"""

from __future__ import annotations

import contextlib
import io

import pytest
from disc_fixtures import OVERLAP_ID, FakeCliDiscogs, overlap_release, silent_flac
from service_fixtures import FakeDiscogs, search_result, tree

from music_manager import service
from music_manager.apply import AlbumApplyError, apply_album
from music_manager.cli import discogs_tag as cli
from music_manager.fs import collect_albums
from music_manager.providers.discogs import flatten_tracklist, release_artists
from music_manager.service import JobState


def mixed(folder):
    """Loose audio plus CD1/ and CD2/ audio in one album folder."""
    silent_flac(folder / "01 One.flac", 181)
    silent_flac(folder / "CD1" / "02 Two.flac", 202)
    silent_flac(folder / "CD1" / "03 Three.flac", 213)
    silent_flac(folder / "CD2" / "01 Four.flac", 244)
    silent_flac(folder / "CD2" / "02 Five.flac", 265)
    (folder / "CD2" / "disc2.cue").write_text("cue", encoding="utf-8")
    return folder


def artist_with_stray(artist):
    silent_flac(artist / "stray.flac", 99)
    album = artist / "[1984]  My War [SST023]"
    for i, name in enumerate(["1. My War", "2. Can't Decide"], 1):
        silent_flac(album / f"{name}.flac", 100 + i)
    (album / "cover.jpg").write_bytes(b"jpg")
    return album


def test_loose_audio_next_to_disc_folders_is_held_with_all_its_audio(tmp_path):
    folder = mixed(tmp_path / "Band A & Band B" / "[1998]  Double Feature [LT-2CD]")
    [album] = collect_albums(tmp_path)
    assert album.path == folder
    assert sorted(p.relative_to(folder).as_posix() for p in album.tracks) == [
        "01 One.flac", "CD1/02 Two.flac", "CD1/03 Three.flac", "CD2/01 Four.flac", "CD2/02 Five.flac"]
    assert "CD1" in album.layout_problem and "loose" in album.layout_problem
    assert album.extra_dirs == []  # the disc folders are not cargo


def test_a_stray_file_never_makes_the_artist_folder_an_album(tmp_path):
    artist = tmp_path / "Black Flag"
    real = artist_with_stray(artist)
    albums = {a.path: a for a in collect_albums(tmp_path)}
    assert set(albums) == {artist, real}
    assert albums[real].layout_problem == "" and len(albums[real].tracks) == 2
    stray = albums[artist]
    assert [p.name for p in stray.tracks] == ["stray.flac"] and stray.layout_problem
    assert stray.extra_dirs == [] and stray.sidecars == []  # nothing would ever travel with it


def test_plain_and_disc_only_albums_are_unchanged(tmp_path):
    plain = tmp_path / "a" / "Album"
    silent_flac(plain / "01 x.flac", 100)
    (plain / "Scans").mkdir()
    (plain / "Scans" / "front.jpg").write_bytes(b"j")
    (plain / "Bonus").mkdir()
    (plain / "Bonus" / "notes.txt").write_text("n", encoding="utf-8")
    discs = tmp_path / "b" / "Discs"
    silent_flac(discs / "CD1" / "01 x.flac", 100)
    silent_flac(discs / "CD2" / "01 y.flac", 101)
    got = {a.path: a for a in collect_albums(tmp_path)}
    assert set(got) == {plain, discs}
    assert [d.name for d in got[plain].extra_dirs] == ["Bonus", "Scans"]
    assert len(got[discs].tracks) == 2
    assert not got[plain].layout_problem and not got[discs].layout_problem


def test_apply_album_refuses_a_layout_problem(tmp_path):
    folder = mixed(tmp_path / "in" / "Album")
    [album] = collect_albums(tmp_path / "in")
    rel = overlap_release()
    before = tree(tmp_path)
    with pytest.raises(AlbumApplyError, match="loose"):
        apply_album(album, tmp_path / "lib", rel, flatten_tracklist(rel, release_artists(rel)), "", dry_run=False)
    assert tree(tmp_path) == before and folder.is_dir()


# --- CLI -------------------------------------------------------------------------------------

def run_cli(monkeypatch, *argv):
    monkeypatch.setattr(cli, "Discogs", FakeCliDiscogs)
    monkeypatch.setattr(cli, "load_token", lambda: ("token", None, None))
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = cli.main([str(a) for a in argv])
    return code, out.getvalue()


def test_cli_skips_loose_audio_next_to_disc_folders(tmp_path, monkeypatch):
    folder = mixed(tmp_path / "in" / "Band A & Band B" / "[1998]  Double Feature [LT-2CD]")
    lib = tmp_path / "lib"
    lib.mkdir()
    before = tree(tmp_path / "in")
    code, out = run_cli(monkeypatch, folder, "--dest", lib, "--release-id", OVERLAP_ID, "--yes")
    assert "warning:" in out and "loose" in out and "skipped" in out, out
    assert tree(tmp_path / "in") == before and not any(lib.rglob("*"))
    assert code == 0


@pytest.mark.parametrize("pick", [("--release-id", str(OVERLAP_ID)), ("--auto",)], ids=["release-id", "auto"])
def test_cli_never_moves_an_album_as_an_artist_folders_extra(tmp_path, monkeypatch, pick):
    artist = tmp_path / "in" / "Black Flag"
    real = artist_with_stray(artist)
    lib = tmp_path / "lib"
    lib.mkdir()
    before = tree(artist)
    _code, out = run_cli(monkeypatch, artist, "--dest", lib, *pick, "--yes")
    assert real.is_dir() and tree(artist) == before, out
    assert not any(lib.rglob("*.flac"))


# --- service ---------------------------------------------------------------------------------

@pytest.fixture
def svc(tmp_path):
    discogs = FakeDiscogs()
    rel = overlap_release()
    discogs.releases[OVERLAP_ID] = rel
    for key in ("double feature", "my war", "black flag", ""):
        discogs.searches[key] = [search_result(rel)]
    root = tmp_path / "lib"
    root.mkdir()
    service.configure(data_dir=tmp_path / "data", config_dir=tmp_path / "config", discogs=discogs)
    service.settings.set(library_roots=[root])
    yield root
    service.reset()


def _rows():
    assert service.jobs.wait(service.library.scan().id).state is JobState.DONE
    return {r.folder: r for r in service.library.albums(service.AlbumFilter(), service.AlbumSort.FOLDER, 0, 100).items}


def _held_problem(album_id):
    assert service.jobs.wait(service.match.find([album_id]).id).state is JobState.DONE
    service.match.choose(album_id, OVERLAP_ID)
    plan = service.plan.build([album_id])
    return plan, [p for p in plan.albums[0].problems if p.kind == "layout_ambiguous"]


def test_service_holds_loose_audio_next_to_disc_folders(svc):
    folder = mixed(svc / "Band A & Band B" / "[1998]  Double Feature [LT-2CD]")
    rows = _rows()
    assert list(rows) == [folder] and rows[folder].track_count == 5
    before = tree(svc)
    plan, problems = _held_problem(rows[folder].id)
    assert len(problems) == 1 and problems[0].paths == [folder]
    with pytest.raises(service.MMError):
        service.plan.apply(plan.id)
    assert tree(svc) == before


def test_service_lists_the_real_album_and_holds_the_stray(svc):
    artist = svc / "Black Flag"
    real = artist_with_stray(artist)
    rows = _rows()
    assert set(rows) == {artist, real}
    plan, problems = _held_problem(rows[artist].id)
    assert len(problems) == 1
    assert all(f.path_before.parent == artist for f in plan.albums[0].files)  # never the album below


def test_normalize_folders_leaves_ambiguous_layouts_alone(tmp_path):
    from music_manager.normalize import plan_library_normalization

    folder = mixed(tmp_path / "Black Flag" / "[1984]  My War [1984] [SST023]")
    renames, skipped, _fine = plan_library_normalization(tmp_path)
    assert renames == [] and [p for p, _reason in skipped] == [folder]
    assert "loose audio" in skipped[0][1]
