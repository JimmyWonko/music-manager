"""Vinyl/cassette tracks are named by side position (A1. Title) by default;
tags keep a sequential track number plus the side in DISCOGS_POSITION."""

import pytest
from conftest import release


def tl(*positions):
    return {"tracklist": [{"position": p, "type_": "track", "title": f"T{p}"} for p in positions]}


def test_double_lp_sides_a_to_d(dt):
    tracks = dt.flatten_tracklist(tl("A1", "A2", "B1", "C1", "C2", "D1"), "X")
    assert [t.side for t in tracks] == ["A1", "A2", "B1", "C1", "C2", "D1"]
    assert [t.number for t in tracks] == [1, 2, 3, 4, 5, 6]
    assert [dt.track_prefix(t) for t in tracks] == ["A1", "A2", "B1", "C1", "C2", "D1"]


def test_side_label_normalised(dt):
    assert dt.side_label("a1") == "A1"
    assert dt.side_label("B 2") == "B2"
    assert dt.side_label("A3a") == "A3a"
    assert dt.side_label("1-3") is None


def test_cd_keeps_numbers(dt):
    tracks = dt.flatten_tracklist(release("multi_cd"), "X")
    assert all(t.side is None for t in tracks)
    assert [dt.track_prefix(t) for t in tracks] == ["1", "2", "1", "2"]


@pytest.mark.parametrize("positions", [("A1", "A1", "B1"), ("A1", "2", "B1")])
def test_repeated_or_mixed_positions_fall_back_to_numbers(dt, positions):
    tracks = dt.flatten_tracklist(tl(*positions), "X")
    assert all(t.side is None for t in tracks)
    assert [dt.track_prefix(t) for t in tracks] == ["1", "2", "3"]


def test_sequential_sides_switch(dt):
    tracks = dt.flatten_tracklist(tl("A1", "B1"), "X")
    assert [dt.track_prefix(t, side_names=False) for t in tracks] == ["1", "2"]


def test_side_named_files_rematch(dt, tmp_path, sample):
    """A second run over already side-named files pairs each file with its own track."""
    album_dir = tmp_path / "LP"
    for name in ("B1. Flip.flac", "A1. Open.flac", "A2. Second.flac"):
        sample("flac", name, folder=album_dir)
    albums = dt.collect_albums(album_dir)
    assert len(albums) == 1
    tracks = dt.flatten_tracklist(
        {"tracklist": [
            {"position": "A1", "type_": "track", "title": "Open"},
            {"position": "A2", "type_": "track", "title": "Second"},
            {"position": "B1", "type_": "track", "title": "Flip"},
        ]},
        "X",
    )
    pairs, _legacy = dt.match_tracks(albums[0], tracks)
    assert {local.name: d.position for local, d in pairs} == {
        "A1. Open.flac": "A1", "A2. Second.flac": "A2", "B1. Flip.flac": "B1",
    }


def test_cli_flag_listed():
    import subprocess
    import sys
    from conftest import REPO
    out = subprocess.run([sys.executable, str(REPO / "discogs-tag.py"), "--help"], capture_output=True, text=True).stdout
    assert "--sequential-sides" in out
