"""Bug 1 (vinyl numbering), bug 7 (index sub-tracks), matching local files."""

from pathlib import Path

from conftest import release


def numbers(tracks):
    return [(t.disc, t.number, t.position, t.title) for t in tracks]


def test_vinyl_sides_numbered_sequentially(dt):
    tracks = dt.flatten_tracklist(release("vinyl_lp"), "The Testers")
    assert [t.number for t in tracks] == list(range(1, 11))
    assert {t.disc for t in tracks} == {1}
    flip = tracks[5]
    assert (flip.position, flip.number, flip.title) == ("B1", 6, "Flip")
    assert flip.legacy_number == 21  # what the old code produced
    # file name and tag use the sequential number
    assert dt.track_filename(flip.number, flip.title, ".FLAC") == "6. Flip.flac"


def test_cassette_letter_only_positions(dt):
    tracks = dt.flatten_tracklist(release("cassette"), "Кино")
    assert [(t.number, t.position) for t in tracks] == [(1, "A"), (2, "B")]


def test_cd_positions_kept(dt):
    tracks = dt.flatten_tracklist(release("multi_cd"), "Band A & Band B")
    assert numbers(tracks) == [
        (1, 1, "1-1", "One"),
        (1, 2, "1-2", "Two"),
        (2, 1, "2-1", "Three"),
        (2, 2, "2-2", "Four"),
    ]


def test_index_track_subtracks_included_in_order(dt):
    tracks = dt.flatten_tracklist(release("index_tracks"), "Orchestra")
    assert [(t.number, t.title) for t in tracks] == [
        (1, "Overture"),
        (2, "Symphony No. 1: I. Allegro"),
        (3, "Symphony No. 1: II. Adagio"),
        (4, "Symphony No. 1: III. Finale"),
        (5, "Encore"),
    ]
    assert tracks[1].position == "2a"


def test_headings_and_untitled_skipped(dt):
    rel = {"tracklist": [
        {"position": "", "type_": "heading", "title": "Part 1"},
        {"position": "1", "type_": "track", "title": "A"},
        {"position": "2", "type_": "track", "title": ""},
        {"position": "3", "type_": "track", "title": "C"},
    ]}
    tracks = dt.flatten_tracklist(rel, "X")
    assert [(t.number, t.title) for t in tracks] == [(1, "A"), (3, "C")]


def test_parse_position(dt):
    assert dt.parse_position("1-3") == (1, 3)
    assert dt.parse_position("CD2-4") == (2, 4)
    assert dt.parse_position("7") == (1, 7)
    assert dt.parse_position("B2") == (1, 0)  # numbered by order instead
    assert dt.is_side_position("AA1") and dt.is_side_position("C") and not dt.is_side_position("12")


def _album(dt, folder: Path, names):
    folder.mkdir(parents=True, exist_ok=True)
    tracks = []
    for name in names:
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        tracks.append(path)
    return dt.AlbumFolder(folder, tracks)


def test_match_fresh_vinyl_rip(dt, tmp_path):
    dtracks = dt.flatten_tracklist(release("vinyl_lp"), "T")
    names = [f"{n:02d} - x{n}.flac" for n in range(1, 11)]
    album = _album(dt, tmp_path / "a", names)
    pairs, legacy = dt.match_tracks(album, dtracks)
    assert not legacy
    assert [dtrack.number for _p, dtrack in pairs] == list(range(1, 11))


def test_match_files_renamed_with_old_21_numbering(dt, tmp_path):
    dtracks = dt.flatten_tracklist(release("vinyl_lp"), "T")
    names = [f"{n}. t.flac" for n in (1, 2, 3, 4, 5, 21, 22, 23, 24, 25)]
    album = _album(dt, tmp_path / "a", names)
    pairs, legacy = dt.match_tracks(album, dtracks)
    assert legacy
    got = {p.name: d.position for p, d in pairs}
    assert got["21. t.flac"] == "B1" and got["25. t.flac"] == "B5" and got["3. t.flac"] == "A3"


def test_match_side_prefixed_files(dt, tmp_path):
    dtracks = dt.flatten_tracklist(release("vinyl_lp"), "T")
    names = ["A1 x.flac", "A2 x.flac", "A3 x.flac", "A4 x.flac", "A5 x.flac",
             "B1. x.flac", "B2. x.flac", "B3. x.flac", "B4. x.flac", "B5. x.flac"]
    album = _album(dt, tmp_path / "a", names)
    pairs, _ = dt.match_tracks(album, dtracks)
    assert all(p.name.split()[0].rstrip(".") == d.position for p, d in pairs)


def test_match_disc_folders(dt, tmp_path):
    dtracks = dt.flatten_tracklist(release("multi_cd"), "T")
    # CD2 listed first on purpose: disc folders decide, not order
    album = _album(dt, tmp_path / "a", ["CD2/1. c.flac", "CD2/2. d.flac", "CD1/1. a.flac", "CD1/2. b.flac"])
    pairs, _ = dt.match_tracks(album, dtracks)
    assert {str(p.relative_to(album.path)): d.title for p, d in pairs} == {
        "CD1/1. a.flac": "One", "CD1/2. b.flac": "Two", "CD2/1. c.flac": "Three", "CD2/2. d.flac": "Four",
    }


def test_match_extra_local_file_left_unmatched(dt, tmp_path):
    dtracks = dt.flatten_tracklist(release("cassette"), "T")
    album = _album(dt, tmp_path / "a", ["1 a.flac", "2 b.flac", "3 bonus.flac"])
    pairs, _ = dt.match_tracks(album, dtracks)
    assert pairs[2][1] is None
