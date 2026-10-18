"""A hand-sorted 2-CD copy whose CD folders disagree with the files' disc tags (synthetic copy).

1. --check-disc-swaps flags every file whose CDn folder disagrees with its DISCNUMBER, with the
   folder, the tag and what the folder's .cue/.m3u say (the tiebreaker): verdict "disagree", exit 3.
2. plan/apply hold the album (disc_ambiguous, reason sources_disagree) unless folder, tag and
   cue/m3u all agree for every file, whichever two of them agree.
3. An apply never puts a file in a CDn folder whose number is not its disc tag.
4. The flat original (41 loose files, DISCNUMBER tags, numbers restarting) pairs from the tags and
   applies onto the right discs; undo restores it.
"""

from __future__ import annotations

import pytest
from prine_fixtures import (
    FOLDER,
    PRINE_ID,
    SWAPPED,
    TRACKS,
    flat,
    hand_sorted,
    identity,
    name_of,
    release,
    tagged,
    wrong_folder,
)
from service_fixtures import FakeDiscogs, search_result, tree
from test_review_flat_multidisc import run_cli

from music_manager import service
from music_manager.apply import AlbumApplyError, execute_layout, layout_album
from music_manager.fs import collect_albums
from music_manager.match import align_tracks
from music_manager.providers.discogs import flatten_tracklist, release_artists
from music_manager.service import JobState
from music_manager.swapcheck import check_disc_swaps, format_report

MOVED = sorted(f"CD{3 - d}/{name_of(n, t)}" for d, n, t, _s in TRACKS if n in SWAPPED)


def contents(root):
    """Every path under root and each file's bytes (undo restores content; mtimes may differ)."""
    return {str(p.relative_to(root)): (p.read_bytes() if p.is_file() else None) for p in root.rglob("*")}


class Source:
    def release(self, release_id):
        assert release_id == PRINE_ID
        return release()


def dtracks():
    rel = release()
    return flatten_tracklist(rel, release_artists(rel))


def test_the_synthetic_copy_mirrors_the_report(tmp_path):
    album = hand_sorted(tmp_path.joinpath(*FOLDER))
    assert len(list((album / "CD1").glob("*.flac"))) == 21 and len(list((album / "CD2").glob("*.flac"))) == 20
    assert wrong_folder(album) == MOVED and len(MOVED) == 10
    assert all(tagged(p) == identity(p) for p in album.rglob("*.flac"))  # every tag is right


# --- 1. the read-only check -------------------------------------------------------------------------

@pytest.mark.parametrize("cue", ["tags", "folders"])
def test_check_flags_each_file_with_folder_tag_and_cue_evidence(tmp_path, cue):
    album = hand_sorted(tmp_path.joinpath(*FOLDER), cue_follows=cue)
    before = tree(tmp_path)
    [check] = check_disc_swaps(tmp_path, Source())
    assert tree(tmp_path) == before
    assert check.status == "disagree" and check.flagged
    got = {str(e.path.relative_to(album)): e for e in check.disagreements}
    assert sorted(got) == MOVED
    for rel, e in got.items():
        folder = int(rel[2])
        assert (e.folder_disc, e.tag_disc) == (folder, 3 - folder)
        want = 3 - folder if cue == "tags" else folder
        assert e.sidecar_disc == want
        assert {p.name for p in e.sidecars} == {f"John Prine - Anthology CD{want}.cue",
                                                f"John Prine - Anthology CD{want}.m3u"}
        assert e.odd_one_out == ("folder" if cue == "tags" else "tag")
    text = format_report([check])
    assert text.startswith("DISAGREE") and "1 flagged" in text
    line = next(x for x in text.splitlines() if "CD2/13 Mason Jar.flac" in x)
    assert "folder CD2 (disc 2)" in line and "DISCNUMBER 1" in line
    assert ("cue/m3u disc 1" in line and "the folder is wrong" in line) if cue == "tags" else \
        ("cue/m3u disc 2" in line and "the disc tag is wrong" in line)


def test_check_reports_ok_files_as_ok(tmp_path):
    hand_sorted(tmp_path.joinpath(*FOLDER), swapped=())
    [check] = check_disc_swaps(tmp_path, Source())
    assert check.disagreements == [] and check.status == "ok"


def test_check_cli_says_disagree_and_exits_3(tmp_path, monkeypatch):
    hand_sorted(tmp_path.joinpath("lib", *FOLDER))
    before = tree(tmp_path)
    code, out = run_cli(monkeypatch, release(), "--check-disc-swaps", tmp_path / "lib")
    assert code == 3, out
    assert "DISAGREE" in out and out.count("the folder is wrong") == 10
    assert tree(tmp_path) == before


# --- 2. plan / apply hold -------------------------------------------------------------------------

@pytest.mark.parametrize("cue", ["tags", "folders"])
def test_engine_refuses_every_file_whose_sources_disagree(tmp_path, cue):
    album_dir = hand_sorted(tmp_path.joinpath(*FOLDER), cue_follows=cue)
    [album] = collect_albums(tmp_path)
    triples, _ = align_tracks(album, dtracks())
    refused = sorted(str(p.relative_to(album_dir)) for p, _dt, m in triples if m == "ambiguous_disc")
    assert refused == MOVED


@pytest.fixture
def svc(tmp_path):
    discogs = FakeDiscogs()
    rel = release()
    discogs.releases[PRINE_ID] = rel
    discogs.searches["anthology"] = [search_result(rel)]
    root = tmp_path / "lib"
    root.mkdir()
    service.configure(data_dir=tmp_path / "data", config_dir=tmp_path / "config", discogs=discogs)
    service.settings.set(library_roots=[root])
    yield root
    service.reset()


def choose(root):
    assert service.jobs.wait(service.library.scan().id).state is JobState.DONE
    [row] = service.library.albums(service.AlbumFilter(), service.AlbumSort.FOLDER, 0, 100).items
    assert service.jobs.wait(service.match.find([row.id]).id).state is JobState.DONE
    service.match.choose(row.id, PRINE_ID)
    return row.id


@pytest.mark.parametrize("cue", ["tags", "folders"])
def test_service_holds_with_the_three_sources_per_file(svc, cue):
    album = hand_sorted(svc.joinpath(*FOLDER), cue_follows=cue)
    album_id = choose(svc)
    before = tree(svc)
    plan = service.plan.build([album_id])
    [problem] = [p for p in plan.albums[0].problems if p.kind == "disc_ambiguous"]
    got = {str(c.path.relative_to(album)): c for c in problem.disc_conflicts}
    assert sorted(got) == MOVED and sorted(str(p.relative_to(album)) for p in problem.paths) == MOVED
    c = got["CD2/13 Mason Jar.flac"]
    assert (c.reason, c.number, c.folder_disc, c.tag_disc) == ("sources_disagree", 13, 2, 1)
    assert c.sidecar_disc == (1 if cue == "tags" else 2)
    assert c.sidecar is not None and c.sidecar.parent.name == f"CD{c.sidecar_disc}"
    assert [(x.disc, x.position) for x in c.same_number] == [(1, "1-13"), (2, "2-13")]
    with pytest.raises(service.MMError):
        service.plan.apply(plan.id)
    assert tree(svc) == before


@pytest.mark.parametrize("cue", ["tags", "folders"])
def test_cli_holds_and_names_the_three_sources(tmp_path, monkeypatch, cue):
    album = hand_sorted(tmp_path.joinpath("in", *FOLDER), cue_follows=cue)
    lib = tmp_path / "lib"
    lib.mkdir()
    before = tree(tmp_path / "in")
    _code, out = run_cli(monkeypatch, release(), album, "--dest", lib, "--release-id", PRINE_ID, "--yes")
    line = next(x for x in out.splitlines() if x.strip().startswith("CD2/13 Mason Jar.flac"))
    assert "folder says disc 2" in line and "DISCNUMBER tag says 1" in line
    assert f"says {1 if cue == 'tags' else 2}" in line.rsplit("cue/m3u", 1)[1]
    assert "skipped: nothing written" in out
    assert tree(tmp_path / "in") == before and not any(lib.rglob("*"))


def test_once_the_folders_are_fixed_it_applies_and_undoes(svc):
    album = hand_sorted(svc.joinpath(*FOLDER))
    for rel in MOVED:  # the user moves the 10 files back by hand
        src = album / rel
        src.rename(album / f"CD{3 - int(rel[2])}" / src.name)
    assert wrong_folder(album) == []
    original = contents(svc)
    album_id = choose(svc)
    plan = service.plan.build([album_id])
    assert plan.albums[0].problems == []
    assert service.jobs.wait(service.plan.apply(plan.id).id).state is JobState.DONE
    files = list(svc.rglob("*.flac"))
    assert len(files) == 41 and all(tagged(p) == identity(p) for p in files) and wrong_folder(svc) == []
    (op,) = service.journal.list(0, 10).items
    undo = service.journal.undo_plan(op.id)
    assert service.jobs.wait(service.journal.undo(op.id, undo.id).id).state is JobState.DONE
    assert contents(svc) == original


# --- 3. the invariant -------------------------------------------------------------------------------

def _layout(album, dest, file_name, check=True):
    rel = release()
    return layout_album(album, dest, rel, dtracks(), "1993", file_name=file_name, log=lambda _l: None, check=check)


def test_apply_never_files_a_track_into_a_cd_folder_of_another_disc(tmp_path):
    flat(tmp_path / "in" / "Anthology")
    [album] = collect_albums(tmp_path / "in")
    before = tree(tmp_path / "in")

    def wrong(dt, local):  # a template bug: disc 1 tracks into CD2/ and back
        return f"CD{3 - dt.disc}/{dt.number:02d} {dt.title}{local.suffix}"

    with pytest.raises(AlbumApplyError, match="would put tracks into another disc's folder"):
        _layout(album, tmp_path / "lib", wrong)  # the planner (check=True) refuses first
    layout = _layout(album, tmp_path / "lib", wrong, check=False)
    with pytest.raises(AlbumApplyError, match="refusing to put tracks into another disc's folder.*CD2"):
        execute_layout(layout, dry_run=False, journal_dir=tmp_path / "j", log=lambda _l: None)
    assert tree(tmp_path / "in") == before and not (tmp_path / "lib").exists()


def test_cd_folders_that_match_the_disc_are_fine(tmp_path):
    flat(tmp_path / "in" / "Anthology")
    [album] = collect_albums(tmp_path / "in")

    def right(dt, local):
        return f"CD{dt.disc}/{dt.number:02d} {dt.title}{local.suffix}"

    layout = _layout(album, tmp_path / "lib", right)
    execute_layout(layout, dry_run=False, journal_dir=tmp_path / "j", log=lambda _l: None)
    files = list((tmp_path / "lib").rglob("*.flac"))
    assert len(files) == 41 and wrong_folder(tmp_path / "lib") == []
    assert all(tagged(p) == identity(p) for p in files)


# --- 4. the flat original ---------------------------------------------------------------------------

def test_flat_original_pairs_every_file_from_its_disc_tag(tmp_path):
    flat(tmp_path.joinpath(*FOLDER))
    [album] = collect_albums(tmp_path)
    triples, _ = align_tracks(album, dtracks())
    assert len(triples) == 41
    assert all(dt is not None and (dt.disc, dt.number, dt.title) == identity(p) for p, dt, _m in triples)
    assert {m for _p, _dt, m in triples} == {"number"}


def test_flat_original_applies_onto_the_right_discs_and_undoes(svc):
    flat(svc.joinpath(*FOLDER))
    original = contents(svc)
    album_id = choose(svc)
    plan = service.plan.build([album_id])
    assert plan.albums[0].problems == [] and len(plan.albums[0].files) == 41
    assert service.jobs.wait(service.plan.apply(plan.id).id).state is JobState.DONE
    [target] = (svc / "John Prine").iterdir()  # the source folder was renamed into it
    assert target.name.startswith("[1993]  Anthology 2CD")
    files = list(target.glob("*.flac"))
    assert len(files) == 41 and all(tagged(p) == identity(p) for p in files)
    (op,) = service.journal.list(0, 10).items
    undo = service.journal.undo_plan(op.id)
    assert service.jobs.wait(service.journal.undo(op.id, undo.id).id).state is JobState.DONE
    assert contents(svc) == original


def test_flat_original_cli_apply(tmp_path, monkeypatch):
    src = flat(tmp_path.joinpath("in", *FOLDER))
    lib = tmp_path / "lib"
    lib.mkdir()
    code, out = run_cli(monkeypatch, release(), src, "--dest", lib, "--release-id", PRINE_ID, "--yes")
    assert code == 0 and "applied=1" in out, out
    files = list(lib.rglob("*.flac"))
    assert len(files) == 41 and all(tagged(p) == identity(p) for p in files)
