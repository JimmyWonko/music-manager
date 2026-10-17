"""The title pass of track pairing takes its threshold from the caller (default TITLE_MATCH_MIN)."""

from __future__ import annotations

from pathlib import Path

import pytest
from service_fixtures import fixture_release, make_album

from music_manager.fs import collect_albums
from music_manager.match import TITLE_MATCH_MIN, align_tracks, match_tracks, similar
from music_manager.providers.discogs import flatten_tracklist, release_artists

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
