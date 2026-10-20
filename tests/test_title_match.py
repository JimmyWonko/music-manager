"""AutoRules.title_match_min drives the title pass of track pairing (engine and service)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from service_fixtures import Library, fixture_release, make_album

from music_manager import service
from music_manager.fs import collect_albums
from music_manager.match import TITLE_MATCH_MIN, align_tracks, match_tracks, similar
from music_manager.providers.discogs import flatten_tracklist, release_artists
from music_manager.service import JobState, MatchStatus, ProblemKind
from music_manager.service import match as service_match

LOOSE = ["Opening", "Secnd", "Zzz Unknown"]  # exact; 0.909 to "Second"; nothing close (< 0.30)


def loose_album(root: Path):
    folder = make_album(root / "loose" / "The Testers - Side Story", LOOSE, ext="mp3")
    return next(a for a in collect_albums(folder) if a.path == folder)


def vinyl_tracks():
    rel = fixture_release("vinyl_lp")
    return flatten_tracklist(rel, release_artists(rel))


def methods(triples):
    return {local.stem: (dt.title if dt else None, method) for local, dt, method in triples}


def test_defaults_are_the_cli_value():
    assert TITLE_MATCH_MIN == 0.80
    assert service.AutoRules().title_match_min == TITLE_MATCH_MIN
    assert service.match.auto_rules().title_match_min == TITLE_MATCH_MIN
    assert similar("Secnd", "Second") == pytest.approx(0.909, abs=1e-3)


def test_engine_default_pairs_above_the_threshold_and_leaves_the_rest(tmp_path):
    album, dtracks = loose_album(tmp_path), vinyl_tracks()
    triples, _ = align_tracks(album, dtracks)  # what discogs-tag (and --auto) does
    assert methods(triples) == {"Opening": ("Opening", "title"), "Secnd": ("Second", "title"),
                                "Zzz Unknown": (None, "unmatched")}
    pairs, _ = match_tracks(album, dtracks)
    assert [(p.stem, dt.title if dt else None) for p, dt in pairs] == [
        (local.stem, dt.title if dt else None) for local, dt, _m in triples]


@pytest.mark.parametrize(("threshold", "secnd"), [
    (0.50, ("Second", "title")),
    (0.90, ("Second", "title")),   # 0.909 >= 0.90: paired
    (0.92, (None, "unmatched")),   # below: left in place
    (1.00, (None, "unmatched")),
])
def test_engine_threshold_decides_the_title_pass(tmp_path, threshold, secnd):
    triples, _ = align_tracks(loose_album(tmp_path), vinyl_tracks(), threshold)
    got = methods(triples)
    assert got["Secnd"] == secnd
    assert got["Opening"] == ("Opening", "title")  # an exact title clears any threshold
    assert got["Zzz Unknown"] == (None, "unmatched")
    pairs, _ = match_tracks(loose_album(tmp_path / "again"), vinyl_tracks(), threshold)
    assert dict((p.stem, dt.title if dt else None) for p, dt in pairs)["Secnd"] == secnd[0]


def test_equal_track_counts_still_fall_back_to_position(tmp_path):
    """--auto requires matching track counts; then step 4 pairs leftovers in order, whatever the threshold."""
    rel = fixture_release("cassette")
    dtracks = flatten_tracklist(rel, release_artists(rel))
    names = [f"Qqq {i}" for i in range(len(dtracks))]
    folder = make_album(tmp_path / "x" / "A - B", names, ext="ogg")
    album = next(a for a in collect_albums(folder) if a.path == folder)
    for threshold in (0.80, 1.0):
        triples, _ = align_tracks(album, dtracks, threshold)
        assert {m for _l, _d, m in triples} == {"position"}


# --- service ---------------------------------------------------------------------------------

@pytest.fixture
def lib(tmp_path):
    library = Library(tmp_path).start(tmp_path)
    library.loose = loose_album(library.root).path
    job = service.jobs.wait(service.library.scan().id)
    assert job.state is JobState.DONE, job.message
    yield library
    service.reset()


def accepted_loose(lib):
    aid = lib.album_id(lib.loose)
    lib.find(aid)
    service.match.choose(aid, lib.vinyl["id"])
    return aid


def by_stem(pairs):
    return {p.local.path.stem: p for p in pairs if p.local is not None}


def test_service_alignment_pairs_above_and_leaves_below(lib):
    aid = accepted_loose(lib)
    (cand,) = service.match.candidates(aid)
    pairs = by_stem(cand.alignment)
    assert (pairs["Opening"].method, pairs["Opening"].remote.title, pairs["Opening"].ok) == ("title", "Opening", True)
    assert (pairs["Secnd"].method, pairs["Secnd"].remote.title) == ("title", "Second")
    assert pairs["Secnd"].title_similarity == pytest.approx(0.909, abs=1e-3) and pairs["Secnd"].ok
    assert (pairs["Zzz Unknown"].method, pairs["Zzz Unknown"].remote) == ("unmatched", None)


def test_service_plan_and_apply_follow_the_pairing(lib):
    aid = accepted_loose(lib)
    held = service.plan.build([aid])
    assert ProblemKind.TRACK_MISMATCH in [p.kind for p in held.albums[0].problems]
    with pytest.raises(service.MMError):
        service.plan.apply(held.id)  # nothing applicable: the album is held, not moved
    row = service.library.album(aid)
    assert row.status is MatchStatus.HELD and row.held_for[0].kind is ProblemKind.TRACK_MISMATCH

    service.match.choose(aid, lib.vinyl["id"])
    plan = service.plan.build([aid], allow_track_mismatch={aid})
    files = {c.path_before.stem: c for c in plan.albums[0].files}
    assert files["Zzz Unknown"].skip_reason and files["Zzz Unknown"].path_after == files["Zzz Unknown"].path_before
    assert files["Secnd"].skip_reason is None and files["Secnd"].path_after != files["Secnd"].path_before
    assert files["Secnd"].path_after.name.endswith("Second.mp3")
    assert plan.skipped == 1
    job = service.jobs.wait(service.plan.apply(plan.id).id)
    assert job.state is JobState.DONE, job.message
    assert (lib.loose / "Zzz Unknown.mp3").is_file()  # unmatched: left in place
    assert files["Secnd"].path_after.is_file() and not (lib.loose / "Secnd.mp3").exists()


def test_service_pairing_reads_auto_rules_title_match_min(lib, monkeypatch):
    aid = accepted_loose(lib)
    strict = dataclasses.replace(service.match.auto_rules(), title_match_min=0.95)
    monkeypatch.setattr(service_match, "auto_rules", lambda: strict)
    (cand,) = service.match.candidates(aid)
    pairs = by_stem(cand.alignment)
    assert pairs["Secnd"].method == "unmatched" and pairs["Opening"].method == "title"
    plan = service.plan.build([aid], allow_track_mismatch={aid})
    files = {c.path_before.stem: c for c in plan.albums[0].files}
    assert files["Secnd"].skip_reason and files["Opening"].skip_reason is None
    assert plan.skipped == 2


def test_auto_accept_honours_a_stricter_title_match_min(lib):
    aid = accepted_loose(lib)
    cands = service.match.candidates(aid)
    loose_rules = service.AutoRules(min_score=0.0, min_margin=0.0, require_track_count=False)
    assert service.match.auto_accept(cands, loose_rules) is cands[0]
    assert service.match.auto_accept(cands, dataclasses.replace(loose_rules, title_match_min=0.90)) is cands[0]
    assert service.match.auto_accept(cands, dataclasses.replace(loose_rules, title_match_min=0.95)) is None
    # the default rules: track count differs, so --auto would skip it too
    assert service.match.auto_accept(cands, local_track_count=3) is None
