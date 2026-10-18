"""Two-disc albums whose discs share track numbers: files pair on (disc, track), never on the number alone.

Formerly known gap 18: re-applying a release to the flat "2CD" folder the first apply wrote ("1. One",
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
from music_manager.match import align_tracks, align_tracks_explained
from music_manager.providers.discogs import flatten_tracklist, release_artists
from music_manager.service import JobState

FOLDER = ("Band A & Band B", "[1998]  Double Feature [LT-2CD]")
TARGET = ("Band A & Band B", "[1998]  Double Feature 2CD [LT-2CD]")


def dtracks():
    rel = overlap_release()
    return flatten_tracklist(rel, release_artists(rel))


def album_of(folder):
    return next(a for a in collect_albums(folder) if a.path == folder)


# --- the engine --------------------------------------------------------------------------------

@pytest.mark.parametrize("layout", sorted(LAYOUTS))
def test_engine_pairs_each_layout_on_disc_and_number(tmp_path, layout):
    folder = make_overlap_album(tmp_path / "in", LAYOUTS[layout])
    triples, _ = align_tracks(album_of(folder), dtracks())
    for local, dt, _method in triples:
        assert (dt.disc, dt.number, dt.title) == identity(local), local.name


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


def _tag_discs(folder, discs):
    from mutagen.flac import FLAC

    for path, disc in discs.items():
        audio = FLAC(folder / path)
        audio["discnumber"] = disc
        audio.save()


def test_engine_reads_the_disc_from_tags_first(tmp_path):
    """Names say nothing about the disc (and nothing useful as titles); the disc tags place them."""
    folder = make_overlap_album(tmp_path / "in", NO_TITLES)
    _tag_discs(folder, {"01 a.flac": "1", "02 b.flac": "1/2", "03 c.flac": "1", "01 d.flac": "2/2", "02 e.flac": "2"})
    triples, _ = align_tracks(album_of(folder), dtracks())
    for local, dt, _method in triples:
        assert (dt.disc, dt.number) == identity(local)[:2], local.name


def test_engine_holds_when_the_tags_and_the_folder_disagree(tmp_path):
    """A CD2 folder whose files are tagged disc 1 (and the other way round): nothing is paired.

    Until Maestro's 2026-10-18 decision the tags won here; now folder, tag and cue/m3u must all
    agree (see discsources), so every file is refused with reason sources_disagree.
    """
    folder = make_overlap_album(tmp_path / "in", ["CD2/01 a", "CD2/02 b", "CD2/03 c", "CD1/01 d", "CD1/02 e"])
    _tag_discs(folder, {"CD2/01 a.flac": "1", "CD2/02 b.flac": "1", "CD2/03 c.flac": "1", "CD1/01 d.flac": "2",
                        "CD1/02 e.flac": "2"})
    triples, _, conflicts = align_tracks_explained(album_of(folder), dtracks())
    assert [(dt, m) for _l, dt, m in triples] == [(None, "ambiguous_disc")] * 5
    assert [(c.path.relative_to(folder).as_posix(), c.folder_disc, c.tag_disc, c.reason) for c in conflicts] == [
        ("CD1/01 d.flac", 1, 2, "sources_disagree"), ("CD1/02 e.flac", 1, 2, "sources_disagree"),
        ("CD2/01 a.flac", 2, 1, "sources_disagree"), ("CD2/02 b.flac", 2, 1, "sources_disagree"),
        ("CD2/03 c.flac", 2, 1, "sources_disagree")]


def test_engine_ignores_disc_tags_that_do_not_fit_the_release(tmp_path):
    """Every file tagged disc 1: two files claim 1-1, so the tags are ignored and the titles decide."""
    folder = make_overlap_album(tmp_path / "in", LAYOUTS["flat-restarting"])
    _tag_discs(folder, {f"{n}.flac": "1" for n in LAYOUTS["flat-restarting"]})
    triples, _ = align_tracks(album_of(folder), dtracks())
    for local, dt, _method in triples:
        assert (dt.disc, dt.number, dt.title) == identity(local), local.name
    folder2 = make_overlap_album(tmp_path / "in2", NO_TITLES)
    _tag_discs(folder2, {f"{n}.flac": "1" for n in NO_TITLES})
    triples, _ = align_tracks(album_of(folder2), dtracks())
    assert sum(1 for _l, _dt, m in triples if m == "ambiguous_disc") == 4


def test_engine_refuses_when_the_numbers_do_not_restart_like_the_release(tmp_path):
    """Disc 2's track 2 is missing: the folder's numbers are not the release's, so 1 and 1 stay refused."""
    folder = make_overlap_album(tmp_path / "in", NO_TITLES[:4] + ["99 extra"])
    triples, _ = align_tracks(album_of(folder), dtracks())
    methods = {local.name: m for local, _dt, m in triples}
    assert methods["01 a.flac"] == methods["01 d.flac"] == "ambiguous_disc"


def test_engine_continuous_numbers_across_discs_are_unchanged(tmp_path):
    """01..05 straight through a 2-disc release: no number repeats, nothing is refused, no tags read."""
    folder = make_overlap_album(tmp_path / "in", ["01 One", "02 Two", "03 Three", "04 Four", "05 Five"])
    triples, _ = align_tracks(album_of(folder), dtracks())
    assert [m for _l, _dt, m in triples] == ["number", "number", "number", "title", "title"]
    for local, dt, _method in triples:
        assert (dt.disc, dt.number, dt.title) == identity(local), local.name


def test_engine_reads_no_tags_for_a_single_disc_release(tmp_path, monkeypatch):
    """Single-disc pairing is exactly the old rules: repeated numbers take the tracks in order, no tag reads."""
    import music_manager.match as match_mod

    def boom(_path):
        raise AssertionError("a single-disc release must not read disc tags")

    monkeypatch.setattr(match_mod, "read_disc_number", boom)
    rel = overlap_release()
    for item in rel["tracklist"]:
        item["position"] = item["position"].split("-")[1]
    single = flatten_tracklist(rel, release_artists(rel))
    assert {dt.disc for dt in single} == {1}
    folder = make_overlap_album(tmp_path / "in", LAYOUTS["flat-restarting"])
    triples, _ = align_tracks(album_of(folder), single)
    # what f74133c (before the disc-aware pairing) gives, unchanged
    assert [(local.name, dt.title, m) for local, dt, m in triples] == [
        ("01 Four.flac", "One", "number"), ("01 One.flac", "Four", "position"),
        ("02 Five.flac", "Two", "number"), ("02 Two.flac", "Five", "position"),
        ("03 Three.flac", "Three", "number")]
