"""disc_ambiguous names each refused file and what it conflicts with: its number against the
release's same-number tracks, and the tracks its title matches. Service and CLI."""

from __future__ import annotations

import pytest
from service_fixtures import search_result, tree
from test_review_flat_multidisc import (
    FOLDER,
    REVIEW_NAMES,
    REVIEW_RELEASE,
    RID,
    TRACKS,
    _choose,
    make_flat,
    make_release,
    run_cli,
    svc,  # noqa: F401  (fixture)
)

from music_manager import service
from music_manager.service._common import problems_from_json, problems_to_json


def choices(items):
    return [(c.disc, c.position, c.title) for c in items]


def held_problem(root, discogs, release_tracks, names=None):
    rel = make_release(release_tracks)
    discogs.releases[RID] = rel
    discogs.searches["live at the other end"] = [search_result(rel)]
    make_flat(root.joinpath(*FOLDER), release_tracks, names)
    album_id = _choose(root)
    plan = service.plan.build([album_id])
    [problem] = [p for p in plan.albums[0].problems if p.kind == "disc_ambiguous"]
    return problem


def test_service_names_each_conflict_of_the_reviews_example(svc):  # noqa: F811
    root, discogs = svc
    problem = held_problem(root, discogs, REVIEW_RELEASE, REVIEW_NAMES)
    by_name = {c.path.name: c for c in problem.disc_conflicts}
    assert sorted(by_name) == sorted(f"{n}.flac" for n in REVIEW_NAMES)
    assert sorted(p.name for p in problem.paths) == sorted(by_name)  # paths unchanged: the same files

    billy = by_name["1. Billy The Bum.flac"]
    assert (billy.number, billy.title, billy.reason) == (1, "Billy The Bum", "no_title_match")
    assert choices(billy.same_number) == [(1, "1-1", "Flashback Blues"), (2, "2-1", "Forbidden Jimmy")]
    assert choices(billy.title_matches) == [(1, "1-2", "Billy The Bum")]  # the release has it as track 2
    assert all(c.title_similarity < 0.8 for c in billy.same_number)
    assert billy.title_matches[0].title_similarity == pytest.approx(1.0)

    flash = by_name["1. Flashback Blues.flac"]
    assert flash.reason == "held_with_group"  # its own title fits 1-1, but its group is held whole
    assert choices(flash.title_matches) == [(1, "1-1", "Flashback Blues")]

    jimmy = by_name["2. Forbidden Jimmy.flac"]
    assert (jimmy.number, jimmy.reason) == (2, "no_title_match")
    assert choices(jimmy.same_number) == [(1, "1-2", "Billy The Bum"), (2, "2-2", "Living In The Future")]
    assert choices(jimmy.title_matches) == [(2, "2-1", "Forbidden Jimmy")]
    assert by_name["2. Living In The Future.flac"].reason == "held_with_group"
    assert all(c.folder_disc is None and c.tag_disc is None for c in problem.disc_conflicts)


def test_numbering_that_does_not_restart_like_the_release(svc):  # noqa: F811
    root, discogs = svc
    # The flat folder is missing disc 2's track 3, so its numbers aren't the release's per-disc numbering.
    present = [t for t in TRACKS if (t[0], t[1]) != (2, 3)]
    rel_tracks = TRACKS
    rel = make_release(rel_tracks)
    discogs.releases[RID] = rel
    discogs.searches["live at the other end"] = [search_result(rel)]
    make_flat(root.joinpath(*FOLDER), present)
    album_id = _choose(root)
    [problem] = [p for p in service.plan.build([album_id]).albums[0].problems if p.kind == "disc_ambiguous"]
    assert problem.disc_conflicts and {c.reason for c in problem.disc_conflicts} == {"numbering"}
    three = [c for c in problem.disc_conflicts if c.number == 3]
    assert three == []  # 3 appears once on disk (disc 1's), so it is not a repeated number
    one = next(c for c in problem.disc_conflicts if c.path.name == "1. Amber Fox.flac")
    assert choices(one.same_number) == [(1, "1-1", "Amber Fox"), (2, "2-1", "Atlas Hotel")]


def test_conflicts_survive_the_held_status_round_trip(svc):  # noqa: F811
    root, discogs = svc
    problem = held_problem(root, discogs, REVIEW_RELEASE, REVIEW_NAMES)
    assert problems_from_json(problems_to_json([problem])) == [problem]


def test_cli_lists_each_conflict(tmp_path, monkeypatch):
    src = make_flat(tmp_path.joinpath("in", *FOLDER), REVIEW_RELEASE, REVIEW_NAMES)
    lib = tmp_path / "lib"
    lib.mkdir()
    before = tree(tmp_path / "in")
    _code, out = run_cli(monkeypatch, make_release(REVIEW_RELEASE), src, "--dest", lib, "--release-id", RID, "--yes")
    lines = out.splitlines()

    def line(name):
        return next(x for x in lines if x.strip().startswith(name))

    billy = line("1. Billy The Bum.flac")
    assert 'number 1' in billy and '1-1 "Flashback Blues"' in billy and '2-1 "Forbidden Jimmy"' in billy
    assert 'title matches 1-2 "Billy The Bum"' in billy
    flash = line("1. Flashback Blues.flac")
    assert 'title matches 1-1 "Flashback Blues"' in flash and "held with" in flash
    assert '2-2 "Living In The Future"' in line("2. Forbidden Jimmy.flac")
    assert "skipped: nothing written" in out
    assert tree(tmp_path / "in") == before and not any(lib.rglob("*"))
