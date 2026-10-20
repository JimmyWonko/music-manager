"""layout_ambiguous says which subfolders and which loose files caused the hold: structured on
the Problem (Problem.layout) and on AlbumFolder, and spelled out by discogs-tag."""

from __future__ import annotations

from disc_fixtures import OVERLAP_ID, silent_flac
from service_fixtures import tree
from test_review_walk import (
    _held_problem,
    _rows,
    artist_with_stray,
    mixed,
    run_cli,
    svc,  # noqa: F401  (fixture)
)

from music_manager.fs import collect_albums
from music_manager.service import LayoutConflict
from music_manager.service._common import problems_from_json, problems_to_json

DOUBLE = ("Band A & Band B", "[1998]  Double Feature [LT-2CD]")


def test_disc_folders_and_loose_files_are_named(tmp_path):
    folder = mixed(tmp_path.joinpath(*DOUBLE))
    [album] = collect_albums(tmp_path)
    assert album.layout_conflict == LayoutConflict("disc_folders", [folder / "CD1", folder / "CD2"],
                                                   [folder / "01 One.flac"])


def test_album_folders_next_to_strays_are_named(tmp_path):
    artist = tmp_path / "Black Flag"
    real = artist_with_stray(artist)
    silent_flac(artist / "another stray.flac", 98)
    other = artist / "[1981]  Damaged [SST007]"
    silent_flac(other / "Disc 1" / "1. Rise Above.flac", 150)  # audio only deeper down still counts
    (artist / "Scans").mkdir()
    (artist / "Scans" / "promo.jpg").write_bytes(b"j")  # no audio: not a cause
    got = {a.path: a for a in collect_albums(tmp_path)}
    assert got[artist].layout_conflict == LayoutConflict(
        "album_folders", [other, real], [artist / "another stray.flac", artist / "stray.flac"])
    assert got[real].layout_conflict is None and got[other].layout_conflict is None


def test_service_problem_carries_the_layout(svc):  # noqa: F811
    folder = mixed(svc.joinpath(*DOUBLE))
    rows = _rows()
    _plan, [problem] = _held_problem(rows[folder].id)
    assert problem.paths == [folder]  # unchanged: the album folder only
    assert problem.layout_reason == "disc_folders"
    assert problem.blocking_subdirs == [folder / "CD1", folder / "CD2"]
    assert problem.loose_files == [folder / "01 One.flac"]
    assert problem.disc_conflicts == []
    assert problems_from_json(problems_to_json([problem])) == [problem]


def test_service_stray_problem_names_the_album_below(svc):  # noqa: F811
    artist = svc / "Black Flag"
    real = artist_with_stray(artist)
    rows = _rows()
    _plan, [problem] = _held_problem(rows[artist].id)
    assert problem.paths == [artist]
    assert (problem.layout_reason, problem.blocking_subdirs, problem.loose_files) == (
        "album_folders", [real], [artist / "stray.flac"])


def test_cli_names_the_subfolders_and_loose_files(tmp_path, monkeypatch):
    folder = mixed(tmp_path.joinpath("in", *DOUBLE))
    lib = tmp_path / "lib"
    lib.mkdir()
    before = tree(tmp_path / "in")
    _code, out = run_cli(monkeypatch, folder, "--dest", lib, "--release-id", OVERLAP_ID, "--yes")
    lines = [x.strip() for x in out.splitlines()]
    assert "held because of: CD1/, CD2/ (disc folders with audio)" in lines, out
    assert "loose files: 01 One.flac" in lines
    assert "skipped: nothing written" in lines
    assert tree(tmp_path / "in") == before


def test_cli_names_the_album_below_a_stray(tmp_path, monkeypatch):
    artist = tmp_path / "in" / "Black Flag"
    artist_with_stray(artist)
    lib = tmp_path / "lib"
    lib.mkdir()
    # Two albums now (--release-id refuses that), so --auto: the stray is reported before any matching.
    _code, out = run_cli(monkeypatch, artist, "--dest", lib, "--auto", "--yes")
    lines = [x.strip() for x in out.splitlines()]
    assert "held because of: [1984]  My War [SST023]/ (other folders with audio)" in lines, out
    assert "loose files: stray.flac" in lines


def test_problem_kinds_are_only_ever_appended():
    """Clients match on the value; the order is kept anyway so the contract's listing stays true."""
    from music_manager.service import ProblemKind

    assert [k.value for k in ProblemKind] == [
        "dest_inside_source", "source_inside_target", "symlink_outside", "collision", "track_mismatch",
        "file_changed", "legacy_journal", "release_not_fetched", "disc_ambiguous", "layout_ambiguous",
        "disc_folder_mismatch", "disc_track_count", "pin_conflict"]
