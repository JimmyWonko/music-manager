"""Two-disc albums whose discs share track numbers: files pair on (disc, track), never on the number alone.

Known gap 18: re-applying a release to the flat "2CD" folder the first apply wrote ("1. One",
"1. Four", ...) paired by number and swapped disc-1 and disc-2 tracks with the same number.
Every case is checked through the service and through the discogs-tag CLI, apply then re-apply.
Synthetic files only; each file's length says which track it really is (see disc_fixtures).
"""

from __future__ import annotations

import contextlib
import io

import pytest
from disc_fixtures import (
    LAYOUTS,
    NO_TITLES,
    OVERLAP_ID,
    FakeCliDiscogs,
    identity,
    make_overlap_album,
    overlap_release,
    swapped_files,
    tagged,
)
from service_fixtures import FakeDiscogs, search_result, tree

from music_manager import service
from music_manager.cli import discogs_tag as cli
from music_manager.fs import collect_albums
from music_manager.match import align_tracks
from music_manager.providers.discogs import flatten_tracklist, release_artists
from music_manager.service import JobState

GAP18 = pytest.mark.xfail(strict=True, reason="known gap 18: pairing ignores the disc")
FOLDER = ("Band A & Band B", "[1998]  Double Feature [LT-2CD]")
TARGET = ("Band A & Band B", "[1998]  Double Feature 2CD [LT-2CD]")


def dtracks():
    rel = overlap_release()
    return flatten_tracklist(rel, release_artists(rel))


def album_of(folder):
    return next(a for a in collect_albums(folder) if a.path == folder)


# --- the engine --------------------------------------------------------------------------------

@pytest.mark.parametrize("layout", [
    "cd-subfolders", "flat-disc-prefix", pytest.param("flat-restarting", marks=GAP18)])
def test_engine_pairs_each_layout_on_disc_and_number(tmp_path, layout):
    folder = make_overlap_album(tmp_path / "in", LAYOUTS[layout])
    triples, _ = align_tracks(album_of(folder), dtracks())
    for local, dt, _method in triples:
        assert (dt.disc, dt.number, dt.title) == identity(local), local.name


@GAP18
def test_engine_refuses_restarting_numbers_it_cannot_place(tmp_path):
    folder = make_overlap_album(tmp_path / "in", NO_TITLES)
    triples, _ = align_tracks(album_of(folder), dtracks())
    methods = {local.name: (method, dt) for local, dt, method in triples}
    for name in ("01 a.flac", "02 b.flac", "01 d.flac", "02 e.flac"):
        assert methods[name] == ("ambiguous_disc", None)
    assert methods["03 c.flac"][1].title == "Three"  # number 3 is only on disc 1


# --- the service -------------------------------------------------------------------------------

@pytest.fixture
def svc(tmp_path):
    discogs = FakeDiscogs()
    rel = overlap_release()
    discogs.releases[OVERLAP_ID] = rel
    discogs.searches["double feature"] = [search_result(rel)]
    root = tmp_path / "lib"
    root.mkdir()
    service.configure(data_dir=tmp_path / "data", config_dir=tmp_path / "config", discogs=discogs)
    service.settings.set(library_roots=[root])
    yield root
    service.reset()


def _scan_and_choose(root):
    assert service.jobs.wait(service.library.scan().id).state is JobState.DONE
    rows = service.library.albums(service.AlbumFilter(), service.AlbumSort.FOLDER, 0, 100).items
    album_id = rows[0].id
    assert service.jobs.wait(service.match.find([album_id]).id).state is JobState.DONE
    service.match.choose(album_id, OVERLAP_ID)
    return album_id


def _apply(plan):
    job = service.jobs.wait(service.plan.apply(plan.id).id)
    assert job.state is JobState.DONE, job.message


@GAP18
@pytest.mark.parametrize("layout", sorted(LAYOUTS))
def test_service_apply_then_reapply_keeps_every_track_on_its_disc(svc, layout):
    make_overlap_album(svc.joinpath(*FOLDER), LAYOUTS[layout])
    album_id = _scan_and_choose(svc)
    first = service.plan.build([album_id])
    assert first.albums[0].problems == []
    _apply(first)
    target = svc.joinpath(*TARGET)
    assert sorted(p.name for p in target.glob("*.flac")) == [
        "1. Four.flac", "1. One.flac", "2. Five.flac", "2. Two.flac", "3. Three.flac"]
    assert swapped_files(svc) == []

    service.match.choose(album_id, OVERLAP_ID)
    again = service.plan.build([album_id])
    assert again.albums[0].problems == []
    assert [f.path_before.name for f in again.albums[0].files if f.path_before != f.path_after] == []
    assert again.tags_to_write == 0
    _apply(again)
    assert swapped_files(svc) == []
    for path in target.glob("*.flac"):
        assert tagged(path) == identity(path)


@GAP18
def test_service_refuses_restarting_numbers_it_cannot_place(svc):
    folder = make_overlap_album(svc.joinpath(*FOLDER), NO_TITLES)
    album_id = _scan_and_choose(svc)
    before = tree(svc)
    plan = service.plan.build([album_id])
    kinds = [p.kind for p in plan.albums[0].problems]
    assert "disc_ambiguous" in kinds
    problem = next(p for p in plan.albums[0].problems if p.kind == "disc_ambiguous")
    assert sorted(p.name for p in problem.paths) == ["01 a.flac", "01 d.flac", "02 b.flac", "02 e.flac"]
    with pytest.raises(service.MMError):
        service.plan.apply(plan.id)
    assert tree(svc) == before and folder.is_dir()


# --- the discogs-tag CLI -----------------------------------------------------------------------

@pytest.fixture
def fake_cli(monkeypatch):
    monkeypatch.setattr(cli, "Discogs", FakeCliDiscogs)
    monkeypatch.setattr(cli, "load_token", lambda: ("token", None, None))


def run_cli(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = cli.main([str(a) for a in argv])
    return code, out.getvalue()


@GAP18
@pytest.mark.parametrize("pick", [("--release-id", str(OVERLAP_ID)), ("--auto",)], ids=["release-id", "auto"])
@pytest.mark.parametrize("layout", sorted(LAYOUTS))
def test_cli_apply_then_reapply_keeps_every_track_on_its_disc(tmp_path, fake_cli, layout, pick):
    src = make_overlap_album(tmp_path.joinpath("in", *FOLDER), LAYOUTS[layout])
    lib = tmp_path / "lib"
    lib.mkdir()
    code, out = run_cli(src, "--dest", lib, *pick, "--yes")
    assert code == 0, out
    assert swapped_files(lib) == []
    target = lib.joinpath(*TARGET)
    for _ in range(2):  # a re-apply, and one more: nothing may flip back and forth
        code, out = run_cli(target, "--dest", lib, *pick, "--yes")
        assert code == 0, out
        assert swapped_files(lib) == []
    assert len(list(target.glob("*.flac"))) == 5


@GAP18
def test_cli_warns_and_skips_restarting_numbers_it_cannot_place(tmp_path, fake_cli):
    src = make_overlap_album(tmp_path.joinpath("in", *FOLDER), NO_TITLES)
    lib = tmp_path / "lib"
    lib.mkdir()
    before = tree(tmp_path / "in")
    _code, out = run_cli(src, "--dest", lib, "--release-id", OVERLAP_ID, "--yes")
    assert "warning: cannot tell which disc" in out
    assert "01 a.flac" in out and "02 e.flac" in out
    assert tree(tmp_path / "in") == before
    assert not any(lib.rglob("*.flac"))
