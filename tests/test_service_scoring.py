"""Candidate.score weighs the track count: a 4-track original beats a 5-track reissue with a better search score."""

from __future__ import annotations

import pytest
from service_fixtures import Library, make_album, search_result

from music_manager import service
from music_manager.match import score_result
from music_manager.service import JobState, ProblemKind

TITLES = ["One", "Two", "Three", "Four"]


def release(rid, year, catno, titles):
    return {"id": rid, "title": "Quartet", "artists": [{"name": "Band Q"}], "year": year,
            "labels": [{"name": "Q Records", "catno": catno}], "formats": [{"name": "CD", "qty": "1"}],
            "master_id": 0, "genres": ["Rock"],
            "tracklist": [{"position": str(i), "title": t, "type_": "track", "duration": ""}
                          for i, t in enumerate(titles, 1)]}


@pytest.fixture
def lib(tmp_path):
    library = Library(tmp_path)
    library.quartet = make_album(library.root / "Band Q" / "[2001]  Quartet [QQ-1]",
                                 [f"{i:02d} {t}" for i, t in enumerate(TITLES, 1)])
    library.original = release(5001, 2001, "ORIG-1", TITLES)          # right tracks, catno differs
    library.reissue = release(5002, 2015, "QQ-1", [*TITLES, "Bonus"])  # catno matches, extra track
    for rel in (library.original, library.reissue):
        library.discogs.releases[rel["id"]] = rel
    library.discogs.searches["quartet"] = [search_result(library.reissue), search_result(library.original)]
    library.start(tmp_path)
    yield library
    service.reset()


def test_count_match_beats_a_better_titled_reissue_with_a_bonus_track(lib):
    q = lib.album_id(lib.quartet)
    lib.find(q)
    original, reissue = service.match.candidates(q)
    assert (original.release_id, reissue.release_id) == (5001, 5002)
    # the --auto score is unchanged: 82% vs 93%, the reissue first
    guess_score = {c.release_id: c.auto_score for c in (original, reissue)}
    assert guess_score == {5001: pytest.approx(0.82), 5002: pytest.approx(0.93)}
    assert reissue.auto_score == pytest.approx(score_result(search_result(lib.reissue), {
        "artist": "Band Q", "album": "Quartet", "year": "2001", "catno": "QQ-1"}))
    # the service score counts the tracks: 4 = 4 wins, 5 != 4 loses
    parts = {c.release_id: {p.field: p for p in c.breakdown} for c in (original, reissue)}
    assert parts[5001]["track_count"].ok and not parts[5002]["track_count"].ok
    assert original.score == pytest.approx((0.75 * 0.82 + 0.20) / 0.95)
    assert reissue.score == pytest.approx((0.75 * 0.93 + 0.0) / 0.95)
    assert original.score > reissue.score and original.margin == pytest.approx(original.score - reissue.score)
    for c in (original, reissue):
        assert sum(p.weight * p.value for p in c.breakdown) == pytest.approx(c.score)
    assert service.library.album(q).confidence == pytest.approx(original.score)
    # auto_accept stays the --auto check: its top (the reissue) has the wrong count, so no clear winner
    assert service.match.auto_accept([original, reissue], local_track_count=4) is None
    # taking the review's top candidate gives a plan without a track mismatch
    service.match.choose(q, original.release_id)
    plan = service.plan.build([q])
    assert ProblemKind.TRACK_MISMATCH not in [p.kind for p in plan.albums[0].problems]
    assert service.jobs.wait(service.plan.apply(plan.id).id).state is JobState.DONE


def test_unfetched_candidates_keep_the_auto_score(lib):
    q = lib.album_id(lib.quartet)
    lib.discogs.fail_release.update({5001, 5002})
    lib.find(q)
    cands = service.match.candidates(q)
    assert [c.release_id for c in cands] == [5002, 5001]
    assert all(not c.fetched and c.score == c.auto_score for c in cands)
    assert [p.weight for p in cands[0].breakdown] == [0.40, 0.35, 0.18, 0.07]
