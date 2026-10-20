"""A 2-CD rip in CD1/ and CD2/ folders (synthetic copy of the OBR-040 report).

Bug 1: applying flattened both discs into the album folder ("5. Blue Umbrella (2).flac") while
CD1/ and CD2/ kept only the sidecars. Each disc's tracks now go into a per-disc folder named after
the source's disc folder (CD{n} when the source had none), with that disc's sidecars.

Bug 2: Discogs lists CD2 track 9 as two tracks (or one index track with two sub_tracks) while the
rip has one medley file: 14 tracks against 13 files. Pairing by number shifted every title after
track 9 by one. A disc whose file and track counts differ is now paired by number only when every
title agrees, else by title when titles pair every file unambiguously, else held.
"""

from __future__ import annotations

import pytest
from obr_fixtures import (
    ARTIST,
    D1,
    D2_FILES,
    OBR_ID,
    SIDECARS,
    TITLE,
    make_album,
    release,
    single_disc_release,
    tags,
)
from service_fixtures import FakeDiscogs, search_result, tree
from test_prine_layout import contents
from test_review_flat_multidisc import run_cli

from music_manager import service
from music_manager.apply import (
    AlbumApplyError,
    apply_album,
    disc_folder_mismatches,
    execute_layout,
    layout_album,
    template_paths,
    template_sets_disc_path,
)
from music_manager.fs import collect_albums
from music_manager.journal import undo_journal
from music_manager.match import align_tracks, similar
from music_manager.naming.templates import PRESETS, compile_template
from music_manager.providers.discogs import flatten_tracklist, release_artists
from music_manager.service import JobState

BUG2 = pytest.mark.xfail(strict=True, reason="a disc with fewer files than tracks is paired by number")
FOLDER = (ARTIST, f"[2016]  {TITLE} [OBR-040]")


def dtracks(rel):
    return flatten_tracklist(rel, release_artists(rel))


def plan_layout(tmp_path, rel, template=None, check=True, **album_kw):
    make_album(tmp_path / "in" / "Album", **album_kw)
    [album] = collect_albums(tmp_path / "in")
    target, namer = template_paths(template, tmp_path / "lib", rel, dtracks(rel), "")
    return layout_album(album, tmp_path / "lib", rel, dtracks(rel), "", target_dir=target, file_name=namer,
                        check=check, log=lambda _l: None, disc_folders=not template_sets_disc_path(template))


def rel_paths(layout):
    return sorted(str(i.dest.relative_to(layout.target_dir)) for i in [*layout.tracks, *layout.moves])


# --- bug 1: per-disc folders ---------------------------------------------------------------------

def test_archivist_puts_each_disc_in_its_disc_folder_with_its_sidecars(tmp_path):
    layout = plan_layout(tmp_path, release("match"))
    got = rel_paths(layout)
    assert "CD1/1. Hello in There.flac" in got and "CD1/5. Blue Umbrella.flac" in got
    assert "CD2/5. Blue Umbrella.flac" in got and "CD2/13. Spanish Pipedream.flac" in got
    assert f"CD1/{ARTIST} - {TITLE} CD1.cue" in got and f"CD2/{ARTIST} - {TITLE} CD2.accurip" in got
    assert "Covers" in got
    assert not any("/" not in p and p.endswith(".flac") for p in got)  # nothing loose in the album root
    assert layout.clashes == [] and disc_folder_mismatches(layout) == []


def test_the_sources_disc_folder_names_are_kept(tmp_path):
    layout = plan_layout(tmp_path, release("match"), disc_dir="Disc {d}")
    got = rel_paths(layout)
    assert "Disc 1/1. Hello in There.flac" in got and "Disc 2/13. Spanish Pipedream.flac" in got
    assert f"Disc 2/{ARTIST} - {TITLE} CD2.cue" in got and disc_folder_mismatches(layout) == []


@pytest.mark.parametrize("sidecars", ["left_in_disc_folders", "loose"])
def test_a_flat_source_gets_cd_n_folders(tmp_path, sidecars):
    """Audio flattened into the album folder ("1-05 - Blue Umbrella.flac"), as the old layout left it."""
    album = make_album(tmp_path / "in" / "Album")
    for d in (1, 2):
        for p in sorted((album / f"CD{d}").iterdir()):
            if p.suffix == ".flac":
                p.rename(album / f"{d}-{p.name}")
            elif sidecars == "loose":
                p.rename(album / p.name)
        if sidecars == "loose":
            (album / f"CD{d}").rmdir()
    rel = release("match")
    before = contents(tmp_path / "in")
    [src] = collect_albums(tmp_path / "in")
    layout = layout_album(src, tmp_path / "lib", rel, dtracks(rel), "", log=lambda _l: None)
    got = rel_paths(layout)
    assert "CD1/5. Blue Umbrella.flac" in got and "CD2/5. Blue Umbrella.flac" in got
    side = f"CD2/{ARTIST} - {TITLE} CD2.cue" if sidecars != "loose" else f"{ARTIST} - {TITLE} CD2.cue"
    assert side in got and not any("(2)" in p for p in got)
    assert layout.clashes == [] and disc_folder_mismatches(layout) == []
    execute_layout(layout, dry_run=False, journal_dir=tmp_path / "j", log=lambda _l: None)
    assert sorted(p.name for p in layout.target_dir.iterdir()) == ["CD1", "CD2", "Covers"] + (
        [] if sidecars != "loose" else sorted(f"{ARTIST} - {TITLE} CD{d}.{e}" for d in (1, 2) for e in SIDECARS))
    [journal] = (tmp_path / "j").glob("*")
    undo_journal(journal, apply=True)
    assert contents(tmp_path / "in") == before


@pytest.mark.parametrize("preset", [p[0] for p in PRESETS])
def test_every_preset_gets_disc_folders(tmp_path, preset):
    text = next(p[2] for p in PRESETS if p[0] == preset)
    layout = plan_layout(tmp_path, release("match"), compile_template(text))
    tracks = [i.dest.relative_to(layout.target_dir) for i in layout.tracks]
    assert len(tracks) == 24 and all(p.parts[0] in ("CD1", "CD2") and len(p.parts) == 2 for p in tracks)
    assert all(p.parts[0] == f"CD{i.meta['disc']}" for p, i in zip(tracks, layout.tracks))
    assert disc_folder_mismatches(layout) == []


def test_a_template_with_its_own_disc_folder_or_disc_number_is_left_alone(tmp_path):
    own = plan_layout(tmp_path / "a", release("match"),
                      compile_template("%albumartist%/%album%/%discnumber%-$num(%tracknumber%,2) %title%"))
    assert all("/" not in p for p in rel_paths(own) if p.endswith(".flac"))
    wrong = plan_layout(tmp_path / "b", release("match"),
                        compile_template("%albumartist%/%album%/CD%disctotal%/%tracknumber% %title%"), check=False)
    assert {i.dest.parent.name for i in wrong.tracks} == {"CD2"}  # not nested into CD2/CD1
    assert len(disc_folder_mismatches(wrong)) == 11  # and the check still catches it


def test_single_disc_and_vinyl_stay_flat(tmp_path):
    single = plan_layout(tmp_path / "s", single_disc_release(), discs={1: D1}, disc_dir="")
    assert "1. Hello in There.flac" in rel_paths(single)
    rel = single_disc_release()
    rel["formats"] = [{"name": "Vinyl", "qty": "2"}]  # 2LP: sides A-D, all disc 1 on Discogs
    for i, t in enumerate(rel["tracklist"]):
        t["position"] = f"{'ABCD'[i // 3]}{i % 3 + 1}"
    vinyl = plan_layout(tmp_path / "v", rel, discs={1: D1}, disc_dir="")
    got = rel_paths(vinyl)
    assert "A1. Hello in There.flac" in got and "D2. Kentucky Moon.flac" in got


def test_apply_and_undo_with_disc_folders(tmp_path):
    rel = release("match")
    layout = plan_layout(tmp_path, rel)
    before = contents(tmp_path / "in")
    execute_layout(layout, dry_run=False, journal_dir=tmp_path / "j", log=lambda _l: None)
    files = sorted(layout.target_dir.rglob("*.flac"))
    assert len(files) == 24 and all(tags(p)[0] == int(p.parent.name[2:]) for p in files)
    assert not (tmp_path / "in" / "Album").exists()

    [journal] = (tmp_path / "j").glob("*")
    undo_journal(journal, apply=True)
    assert contents(tmp_path / "in") == before
    assert not list((tmp_path / "lib").rglob("*.flac"))


# --- bug 2: a medley file against two Discogs tracks -------------------------------------------------

@BUG2
@pytest.mark.parametrize("shape", ["plain", "index"])
def test_titles_are_never_shifted(tmp_path, shape):
    make_album(tmp_path / "in" / "Album")
    [album] = collect_albums(tmp_path / "in")
    rel = release(shape)
    triples, _ = align_tracks(album, dtracks(rel))

    for local, dt, _m in triples:
        if dt is not None:
            assert similar(local.stem.split(" - ", 1)[1], dt.title) >= 0.8 or dt.disc == 1, (local.name, dt.title)
    cd2 = [(local.name, dt, m) for local, dt, m in triples if local.parent.name == "CD2"]
    assert all(dt is None and m == "track_count" for _n, dt, m in cd2)  # the disc is held
    assert all(dt is not None for local, dt, _m in triples if local.parent.name == "CD1")


@BUG2
@pytest.mark.parametrize("shape", ["plain", "index"])
def test_layout_and_apply_hold_the_disc_with_a_clear_message(tmp_path, shape):
    layout = plan_layout(tmp_path, release(shape))
    [hold] = layout.count_holds
    assert (hold.disc, hold.file_count, hold.track_count) == (2, 13, 14)
    assert "09 - Hey Good Lookin',Jambalaya (On the Bayou).flac" in hold.message
    assert "medley" in hold.message
    [album] = collect_albums(tmp_path / "in")
    before = contents(tmp_path / "in")
    with pytest.raises(AlbumApplyError, match="CD2.*13 files.*14 tracks"):
        apply_album(album, tmp_path / "lib", release(shape), dtracks(release(shape)), "", dry_run=False,
                    journal_dir=tmp_path / "j")
    assert contents(tmp_path / "in") == before


@BUG2
def test_titles_rescue_a_disc_whose_numbers_are_off(tmp_path):
    """A missing file renumbered the rest: titles pair every file unambiguously, so they win."""
    titles = D2_FILES[:3] + D2_FILES[4:]  # Zinc Bucket missing; files numbered 1..12
    make_album(tmp_path / "in" / "Album", discs={1: D1, 2: titles})
    [album] = collect_albums(tmp_path / "in")
    rel = release("match")
    triples, _ = align_tracks(album, dtracks(rel))
    for local, dt, _m in triples:
        assert dt is not None and dt.title == local.stem.split(" - ", 1)[1]


@pytest.fixture
def svc(tmp_path):
    discogs = FakeDiscogs()
    for rid, rel in ((OBR_ID, release("plain")),):
        discogs.releases[rid] = rel
        discogs.searches[TITLE.casefold()] = [search_result(rel)]
    root = tmp_path / "lib"
    root.mkdir()
    service.configure(data_dir=tmp_path / "data", config_dir=tmp_path / "config", discogs=discogs)
    service.settings.set(library_roots=[root])
    yield root, discogs
    service.reset()


def choose(root):
    assert service.jobs.wait(service.library.scan().id).state is JobState.DONE
    [row] = service.library.albums(service.AlbumFilter(), service.AlbumSort.FOLDER, 0, 100).items
    assert service.jobs.wait(service.match.find([row.id]).id).state is JobState.DONE
    service.match.choose(row.id, OBR_ID)
    return row.id


@BUG2
def test_service_holds_and_allow_track_mismatch_does_not_override(svc):
    root, _discogs = svc
    make_album(root.joinpath(*FOLDER))
    album_id = choose(root)
    before = tree(root)
    plan = service.plan.build([album_id], allow_track_mismatch={album_id})
    kinds = [p.kind for p in plan.albums[0].problems]
    assert "disc_track_count" in kinds, kinds
    [problem] = [p for p in plan.albums[0].problems if p.kind == "disc_track_count"]
    assert len(problem.paths) == 13 and all(p.parent.name == "CD2" for p in problem.paths)
    assert service.ProblemKind("disc_track_count") is list(service.ProblemKind)[-1]
    with pytest.raises(service.MMError):
        service.plan.apply(plan.id)
    assert tree(root) == before


def test_service_apply_with_disc_folders_and_undo(svc):
    root, discogs = svc
    discogs.releases[OBR_ID] = release("match")
    album = make_album(root.joinpath(*FOLDER))
    original = contents(root)
    album_id = choose(root)
    plan = service.plan.build([album_id])
    assert plan.albums[0].problems == []
    assert service.jobs.wait(service.plan.apply(plan.id).id).state is JobState.DONE
    [target] = (root / ARTIST).iterdir()
    assert sorted(p.name for p in target.iterdir()) == ["CD1", "CD2", "Covers"]
    assert (target / "CD2" / "5. Blue Umbrella.flac").is_file() and (target / "CD1" / "5. Blue Umbrella.flac").is_file()
    assert len(list((target / "CD2").glob("*.cue"))) == 1
    (op,) = service.journal.list(0, 10).items
    undo = service.journal.undo_plan(op.id)
    assert service.jobs.wait(service.journal.undo(op.id, undo.id).id).state is JobState.DONE
    assert contents(root) == original and album.is_dir()


@BUG2
@pytest.mark.parametrize("shape", ["plain", "index"])
def test_cli_skips_the_held_album(tmp_path, monkeypatch, shape):
    src = make_album(tmp_path.joinpath("in", *FOLDER))
    lib = tmp_path / "lib"
    lib.mkdir()
    before = contents(tmp_path / "in")
    _code, out = run_cli(monkeypatch, release(shape), src, "--dest", lib, "--release-id", OBR_ID, "--yes",
                         "--allow-track-mismatch")
    assert "CD2" in out and "skipped: nothing written" in out, out
    assert contents(tmp_path / "in") == before and not any(lib.rglob("*"))
