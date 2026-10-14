"""End-to-end apply on temp folders: bug 1 (file names), 4 (errors), 9 (--limit), 13 (playlists)."""

import shutil

import pytest
from conftest import SAMPLES, release
from mutagen import File as MutagenFile


def make_album(folder, names, ext="flac"):
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        dest = folder / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SAMPLES / f"silent.{ext}", dest)
    return folder


def run_apply(dt, album_dir, dest_root, rel, orig="1985", **kw):
    album = next(a for a in dt.collect_albums(album_dir) if a.path == album_dir)
    artist = dt.release_artists(rel)
    dtracks = dt.flatten_tracklist(rel, artist)
    return dt.apply_album(album, dest_root, rel, dtracks, orig, dry_run=kw.pop("dry_run", False), **kw)


VINYL_DIR = "Testers, The/[1985]  Side Story [WAX001]"  # not a reissue: one year, no release-year bracket


def test_vinyl_album_named_by_side_tags_sequential(dt, tmp_path):
    src = make_album(tmp_path / "in" / "Testers - Side Story", [f"{n:02d} track.flac" for n in range(1, 11)])
    out = tmp_path / "lib"
    run_apply(dt, src, out, release("vinyl_lp"))
    target = out / VINYL_DIR
    names = sorted(p.name for p in target.iterdir())
    assert names[:2] == ["A1. Opening.flac", "A2. Second.flac"]
    assert "B1. Flip.flac" in names and "B5. Closing.flac" in names
    assert not any(n.startswith(("21.", "6.")) for n in names)
    tags = MutagenFile(target / "B1. Flip.flac").tags
    assert tags["TRACKNUMBER"] == ["6"] and tags["TRACKTOTAL"] == ["10"] and tags["DISCOGS_POSITION"] == ["B1"]
    assert not src.exists()  # everything moved; the emptied source folder was removed


def test_reissue_gets_original_date_and_orig_folder_year(dt, tmp_path):
    src = make_album(tmp_path / "in" / "Testers - Side Story", [f"{n:02d} track.flac" for n in range(1, 11)])
    out = tmp_path / "lib"
    rel = dict(release("vinyl_lp"), year=2016, released="2016-04-16")  # 2016 repress of a 1985 album
    run_apply(dt, src, out, rel, orig="1985")
    target = out / "Testers, The" / "[1985]  Side Story [2016] [WAX001]"
    tags = MutagenFile(target / "A1. Opening.flac").tags
    assert tags["ORIGINALDATE"] == ["1985"] and tags["ORIGINALYEAR"] == ["1985"]
    assert tags["DATE"] == ["2016-04-16"]


@pytest.mark.parametrize("orig", ["1985", "", "1990"])  # same year, no master, master year later
def test_original_release_gets_no_original_date(dt, tmp_path, orig):
    src = make_album(tmp_path / "in" / "Testers - Side Story", [f"{n:02d} track.flac" for n in range(1, 11)])
    out = tmp_path / "lib"
    run_apply(dt, src, out, release("vinyl_lp"), orig=orig)
    target = out / VINYL_DIR
    tags = MutagenFile(target / "A1. Opening.flac").tags
    assert "ORIGINALDATE" not in tags and "ORIGINALYEAR" not in tags
    assert tags["DATE"] == ["1985"]


def test_stale_original_date_from_earlier_run_removed(dt, tmp_path):
    out = tmp_path / "lib"
    folder = make_album(out / VINYL_DIR, [f"{n}. t.flac" for n in range(1, 11)])
    for path in folder.glob("*.flac"):
        audio = MutagenFile(path)
        audio.tags["DISCOGS_RELEASE_ID"] = ["1000001"]  # tagged by an earlier run
        audio.tags["ORIGINALDATE"] = ["1985"]
        audio.tags["ORIGINALYEAR"] = ["1985"]
        audio.save()
    run_apply(dt, folder, out, release("vinyl_lp"), orig="1985")
    tags = MutagenFile(out / VINYL_DIR / "A1. Opening.flac").tags
    assert "ORIGINALDATE" not in tags and "ORIGINALYEAR" not in tags
    assert folder == out / VINYL_DIR and folder.is_dir()  # folder name unchanged (re-tagged in place)


def test_non_reissue_folder_keeps_leading_year_my_war(dt, tmp_path):
    rel = {
        "id": 2000023, "master_id": 3000023, "title": "My War", "year": 1984, "released": "1984-03-00",
        "artists": [{"name": "Black Flag"}], "labels": [{"name": "SST Records", "catno": "SST 023"}],
        "formats": [{"name": "Vinyl", "qty": "1"}], "genres": ["Rock"], "styles": ["Hardcore"],
        "tracklist": [{"position": "A1", "type_": "track", "title": "My War"},
                      {"position": "B1", "type_": "track", "title": "Nothing Left Inside"}],
    }
    src = make_album(tmp_path / "in" / "Black Flag - My War", ["01.flac", "02.flac"])
    out = tmp_path / "lib"
    run_apply(dt, src, out, rel, orig="1984")  # master year == release year
    target = out / "Black Flag" / "[1984]  My War [SST023]"
    assert sorted(p.name for p in target.iterdir()) == ["A1. My War.flac", "B1. Nothing Left Inside.flac"]
    tags = MutagenFile(target / "A1. My War.flac").tags
    assert "ORIGINALDATE" not in tags and "ORIGINALYEAR" not in tags  # tags: not a reissue
    assert tags["DATE"] == ["1984-03"]


@pytest.mark.parametrize(
    "title, master, release_year, catno, expected",
    [
        ("Nervous Breakdown", "1979", "1988", "SST CD 001", "[1979]  Nervous Breakdown [1988] [SSTCD001]"),
        ("My War", "1984", "1984", "SST 023", "[1984]  My War [SST023]"),
        ("My War", "", "1984", "SST 023", "[1984]  My War [SST023]"),
        ("Who's Got the 10½?", "1986", "1986", "SST CD 060", "[1986]  Who's Got the 10½_ [SSTCD060]"),
        ("What The…", "", "2013", "SST 391", "[2013]  What The… [SST391]"),
        ("What The…", "2013", "2013", "", "[2013]  What The…"),
        ("What The…", "", "", "", "What The…"),
        ("What The…", "", "", "SST 391", "What The… [SST391]"),
        ("The First Four Years", "1983", "1990", "", "[1983]  The First Four Years [1990]"),
    ],
)
def test_folder_names_match_existing_library(dt, title, master, release_year, catno, expected):
    assert dt.album_folder_name(title, master, release_year, catno, 1) == expected


def test_original_date_from_other_tagger_kept_when_not_reissue(dt, sample, meta):
    path = sample("flac")
    audio = MutagenFile(path)
    audio.tags["ORIGINALDATE"] = ["1979"]  # e.g. from Picard; this tool never wrote it
    audio.save()
    dt.write_tags(path, dict(meta, originaldate=""))
    assert MutagenFile(path).tags["ORIGINALDATE"] == ["1979"]


@pytest.mark.parametrize("ext", ["mp3", "m4a", "wma", "wav"])
def test_stale_original_date_removed_all_formats(dt, sample, meta, ext):
    path = sample(ext)
    dt.write_tags(path, meta)  # reissue: original date written
    dt.write_tags(path, dict(meta, originaldate=""))  # same release, now treated as original
    audio = MutagenFile(path)
    keys = {k.casefold() for k in (dt.ID3(path).keys() if ext == "mp3" else audio.tags.keys())}
    assert not {"tdor", "tory", "originaldate", "----:com.apple.itunes:originaldate",
                "----:com.apple.itunes:originalyear", "wm/originalreleasetime", "wm/originalreleaseyear"} & keys


def test_legacy_numbered_files_fixed(dt, tmp_path):
    names = ["1. Opening.flac", "2. Second.flac", "3. Third.flac", "4. Fourth.flac", "5. Fifth.flac",
             "21. Flip.flac", "22. Seventh.flac", "23. Eighth.flac", "24. Ninth.flac", "25. Closing.flac"]
    out = tmp_path / "lib"
    src = make_album(out / VINYL_DIR, names)  # re-tag in place
    run_apply(dt, src, out, release("vinyl_lp"))
    got = sorted(p.name for p in src.iterdir())
    assert got == ["A1. Opening.flac", "A2. Second.flac", "A3. Third.flac", "A4. Fourth.flac", "A5. Fifth.flac",
                   "B1. Flip.flac", "B2. Seventh.flac", "B3. Eighth.flac", "B4. Ninth.flac", "B5. Closing.flac"]


def test_sequential_sides_option_names_by_number(dt, tmp_path, monkeypatch):
    monkeypatch.setattr(dt, "SIDE_NAMES", False)
    src = make_album(tmp_path / "in" / "Testers - Side Story", [f"{n:02d} track.flac" for n in range(1, 11)])
    out = tmp_path / "lib"
    run_apply(dt, src, out, release("vinyl_lp"))
    names = {p.name for p in (out / VINYL_DIR).iterdir()}
    assert "6. Flip.flac" in names and "10. Closing.flac" in names
    tags = MutagenFile(out / VINYL_DIR / "6. Flip.flac").tags
    assert tags["TRACKNUMBER"] == ["6"] and tags["DISCOGS_POSITION"] == ["B1"]


def test_side_named_album_retag_is_stable(dt, tmp_path):
    """Re-running on an album already named A1. B1. leaves every file where it is."""
    src = make_album(tmp_path / "in" / "Testers - Side Story", [f"{n:02d} track.flac" for n in range(1, 11)])
    out = tmp_path / "lib"
    run_apply(dt, src, out, release("vinyl_lp"))
    before = sorted(p.name for p in (out / VINYL_DIR).iterdir())
    run_apply(dt, out / VINYL_DIR, out, release("vinyl_lp"))
    assert sorted(p.name for p in (out / VINYL_DIR).iterdir()) == before


def test_in_place_swap_never_produces_suffixes(dt, tmp_path):
    rel = release("cassette")  # A = "Группа крови", B = "Звезда по имени Солнце"
    out = tmp_path / "lib"
    folder = out / "Кино" / "[1985]  Tape Loops [1991] [C60-001]"
    make_album(folder, ["1. Звезда по имени Солнце.flac", "2. Группа крови.flac"])
    # files named with the right numbers but swapped titles: matched by number
    run_apply(dt, folder, out, rel)
    assert sorted(p.name for p in folder.iterdir()) == ["A. Группа крови.flac", "B. Звезда по имени Солнце.flac"]


def test_index_subtracks_become_files(dt, tmp_path):
    src = make_album(tmp_path / "in" / "Orchestra - Works", [f"{n:02d}.flac" for n in range(1, 6)])
    out = tmp_path / "lib"
    run_apply(dt, src, out, release("index_tracks"), orig="")
    target = out / "Orchestra" / "[2001]  Works [CL-9]"  # no master year: release year leads
    assert sorted(p.name for p in target.iterdir()) == sorted([
        "1. Overture.flac", "2. Symphony No. 1_ I. Allegro.flac", "3. Symphony No. 1_ II. Adagio.flac",
        "4. Symphony No. 1_ III. Finale.flac", "5. Encore.flac"])


def test_generated_playlist_regenerated_handmade_moved(dt, common, tmp_path):
    src = make_album(tmp_path / "in" / "Testers - Side Story", [f"{n:02d} track.flac" for n in range(1, 11)])
    legacy = src / "Testers - Side Story.m3u"
    legacy.write_text("\n".join(f"{n:02d} track.flac" for n in range(1, 11)) + "\n", encoding="utf-8")
    mine = src / "favourites.m3u"
    mine.write_text("03 track.flac\n01 track.flac\n", encoding="utf-8")
    out = tmp_path / "lib"
    run_apply(dt, src, out, release("vinyl_lp"))
    target = out / VINYL_DIR
    assert not (target / "Testers - Side Story.m3u").exists()  # stale playlist not carried along
    fresh = target / f"{target.name}.m3u"
    lines = fresh.read_text(encoding="utf-8").splitlines()
    assert lines[:2] == ["#EXTM3U", common.GENERATED_MARKER]
    assert lines[2:] == [f"{s}. {t}.flac" for s, t in [
        ("A1", "Opening"), ("A2", "Second"), ("A3", "Third"), ("A4", "Fourth"), ("A5", "Fifth"),
        ("B1", "Flip"), ("B2", "Seventh"), ("B3", "Eighth"), ("B4", "Ninth"), ("B5", "Closing")]]
    assert (target / "favourites.m3u").read_text(encoding="utf-8") == "03 track.flac\n01 track.flac\n"


def test_no_playlist_created_when_there_was_none(dt, tmp_path):
    src = make_album(tmp_path / "in" / "Testers - Side Story", [f"{n:02d} track.flac" for n in range(1, 11)])
    out = tmp_path / "lib"
    run_apply(dt, src, out, release("vinyl_lp"))
    assert not list((out / VINYL_DIR).glob("*.m3u"))


def test_dry_run_changes_nothing(dt, tmp_path):
    src = make_album(tmp_path / "in" / "Testers - Side Story", [f"{n:02d} track.flac" for n in range(1, 11)])
    before = {p: p.read_bytes() for p in src.iterdir()}
    run_apply(dt, src, tmp_path / "lib", release("vinyl_lp"), dry_run=True)
    assert {p: p.read_bytes() for p in src.iterdir()} == before
    assert not (tmp_path / "lib").exists()


def test_tag_failure_moves_nothing(dt, tmp_path):
    src = make_album(tmp_path / "in" / "Testers - Side Story", [f"{n:02d} track.flac" for n in range(1, 11)])
    (src / "05 track.flac").write_bytes(b"corrupt")
    with pytest.raises(dt.AlbumApplyError) as err:
        run_apply(dt, src, tmp_path / "lib", release("vinyl_lp"))
    assert "nothing was moved" in str(err.value)
    assert len(list(src.glob("*.flac"))) == 10
    assert not (tmp_path / "lib" / VINYL_DIR).exists() or not any((tmp_path / "lib" / VINYL_DIR).iterdir())


def test_move_failure_reports_what_moved(dt, tmp_path, monkeypatch):
    src = make_album(tmp_path / "in" / "Testers - Side Story", [f"{n:02d} track.flac" for n in range(1, 11)])
    real_move = dt._move
    calls = {"n": 0}

    def flaky(a, b, journal=None):
        calls["n"] += 1
        if calls["n"] == 4:
            raise OSError(28, "No space left on device")
        return real_move(a, b, journal)

    monkeypatch.setattr(dt, "_move", flaky)
    with pytest.raises(dt.AlbumApplyError) as err:
        run_apply(dt, src, tmp_path / "lib", release("vinyl_lp"))
    text = str(err.value)
    assert "No space left on device" in text and "moved 3 item(s)" in text
    assert "01 track.flac" in text and "-> " in text and "not moved (7)" in text
    assert len(list(src.glob("*.flac"))) == 7  # nothing lost, nothing half-moved


class FakeDiscogs:
    def __init__(self, *args):
        pass

    def search_releases(self, artist, album, catno="", year=""):
        return []

    def release(self, release_id):
        return release({1000001: "vinyl_lp", 1000002: "cassette"}[release_id])

    def master(self, master_id):
        return {"year": 1985}


def fake_choose(api, guess, results, auto, release_id):
    return api.release(1000001 if "Side" in guess["album"] else 1000002)


def test_batch_continues_after_unexpected_error(dt, tmp_path, monkeypatch, capsys):
    root = tmp_path / "in"
    make_album(root / "Testers - Side Story", [f"{n:02d} track.flac" for n in range(1, 11)])
    bad = make_album(root / "Kino - Tape Loops", ["1 a.flac", "2 b.flac"])
    (bad / "1 a.flac").write_bytes(b"not flac")  # TypeError/RuntimeError territory, not MutagenError
    monkeypatch.setattr(dt, "Discogs", FakeDiscogs)
    monkeypatch.setattr(dt, "load_token", lambda: ("t", None, None))
    monkeypatch.setattr(dt, "choose_release", fake_choose)
    code = dt.main([str(root), "--dest", str(tmp_path / "lib"), "--yes"])
    out = capsys.readouterr()
    assert code == 1
    assert "applied=1" in out.out and "failed=1" in out.out
    assert (tmp_path / "lib" / VINYL_DIR / "B1. Flip.flac").exists()
    assert "Tape Loops" in out.out or "Kino - Tape Loops" in out.out


def test_limit_counts_only_albums_processed(dt, tmp_path, monkeypatch, capsys):
    root = tmp_path / "in"
    lib = tmp_path / "lib"
    # first album (alphabetically) is already tagged
    tagged = make_album(root / "A Tagged - Album", ["1 a.flac"])
    audio = MutagenFile(tagged / "1 a.flac")
    audio.tags["DISCOGS_RELEASE_ID"] = ["5"]
    audio.save()
    make_album(root / "Testers - Side Story", [f"{n:02d} track.flac" for n in range(1, 11)])
    monkeypatch.setattr(dt, "Discogs", FakeDiscogs)
    monkeypatch.setattr(dt, "load_token", lambda: ("t", None, None))
    monkeypatch.setattr(dt, "choose_release", fake_choose)
    code = dt.main([str(root), "--dest", str(lib), "--yes", "--skip-tagged", "--limit", "1"])
    out = capsys.readouterr().out
    assert code == 0
    assert "applied=1" in out
    assert (lib / VINYL_DIR / "A1. Opening.flac").exists()
