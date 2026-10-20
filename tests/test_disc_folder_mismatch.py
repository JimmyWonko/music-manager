"""The planner flags a destination that would put a track into another disc's CD folder.

plan.build gives the album a disc_folder_mismatch problem (held, shown in the preview) naming each
file, the disc tag it gets and the CD folder it would land in. discogs-tag --dry-run says the same
before anything is written. The apply-time check stays as a backstop when the planner is bypassed.
"""

from __future__ import annotations

import pytest
from prine_fixtures import FOLDER, PRINE_ID, TRACKS, flat, release
from service_fixtures import tree
from test_prine_layout import choose, contents, svc  # noqa: F401  (svc: fixture)
from test_review_flat_multidisc import run_cli

from music_manager import apply as apply_mod
from music_manager import service
from music_manager.service import JobState

WRONG = "%albumartist%/%album%/CD%disctotal%/%tracknumber% %title%"  # every track into CD2/
RIGHT = "%albumartist%/%album%/%tracknumber% %title%"


def use_template(text):
    t = service.templates.save("cd folders", text)
    service.settings.set(active_template=t.id)


def test_plan_build_flags_and_holds_tracks_bound_for_another_discs_folder(svc):  # noqa: F811
    album = flat(svc.joinpath(*FOLDER))
    album_id = choose(svc)
    use_template(WRONG)
    before = tree(svc)
    plan = service.plan.build([album_id])
    [problem] = [p for p in plan.albums[0].problems if p.kind == "disc_folder_mismatch"]
    disc1 = sorted(album / f"{n:02d} {t}.flac" for d, n, t, _s in TRACKS if d == 1)
    assert sorted(problem.paths) == disc1  # the 21 disc-1 files; disc 2 goes to CD2/ rightly
    by_name = {m.path.name: m for m in problem.folder_mismatches}
    assert sorted(by_name) == sorted(p.name for p in disc1)
    m = by_name["13 Mason Jar.flac"]
    assert (m.tag_disc, m.folder_disc, m.folder.name) == (1, 2, "CD2")
    assert m.destination.parent == m.folder and m.destination.name == "13 Mason Jar.flac"
    assert m.path == album / "13 Mason Jar.flac"
    assert "CD2" in problem.message and "13 Mason Jar.flac" in problem.message
    assert service.ProblemKind("disc_folder_mismatch") is service.ProblemKind.DISC_FOLDER_MISMATCH
    with pytest.raises(service.MMError):
        service.plan.apply(plan.id)  # every album held: nothing to apply
    assert tree(svc) == before
    rows = service.library.albums(service.AlbumFilter(), service.AlbumSort.FOLDER, 0, 10).items
    assert rows[0].status is service.MatchStatus.HELD
    assert [p.kind for p in rows[0].held_for] == ["disc_folder_mismatch"]
    assert rows[0].held_for[0].folder_mismatches == problem.folder_mismatches  # survives the DB


def test_a_template_without_cd_folders_is_fine(svc):  # noqa: F811
    flat(svc.joinpath(*FOLDER))
    album_id = choose(svc)
    use_template(RIGHT)
    plan = service.plan.build([album_id])
    assert plan.albums[0].problems == []


def test_backstop_fires_when_the_planner_check_is_bypassed(svc, monkeypatch):  # noqa: F811
    from music_manager.service import _planning

    flat(svc.joinpath(*FOLDER))
    album_id = choose(svc)
    use_template(WRONG)
    monkeypatch.setattr(_planning, "disc_folder_mismatches", lambda _layout: [])
    plan = service.plan.build([album_id])
    assert plan.albums[0].problems == []  # the planner was bypassed
    before = contents(svc)
    job = service.jobs.wait(service.plan.apply(plan.id).id)
    assert job.state in (JobState.FAILED, JobState.DONE)
    assert "refusing to put tracks into another disc's folder" in (job.message or ""), job.message
    assert contents(svc) == before  # nothing tagged, nothing moved


def _wrong_name(dt, local):
    return f"CD{3 - dt.disc}/{dt.number:02d} {dt.title}{local.suffix}"


@pytest.mark.parametrize("mode", ["--dry-run", "--yes"])
def test_cli_preview_and_apply_refuse_before_writing(tmp_path, monkeypatch, mode):
    src = flat(tmp_path.joinpath("in", *FOLDER))
    lib = tmp_path / "lib"
    lib.mkdir()
    monkeypatch.setattr(apply_mod, "default_file_name", _wrong_name)
    before = contents(tmp_path)
    code, out = run_cli(monkeypatch, release(), src, "--dest", lib, "--release-id", PRINE_ID, mode)
    assert "would put tracks into another disc's folder" in out, out
    assert "13 Mason Jar.flac -> CD2/ (disc tag 1)" in out
    assert contents(tmp_path) == before and code != 0
