"""The checks that used to live only in ``discogs-tag --self-test`` (review #14).

Each case calls the public helpers the way the tool does and asserts the exact
result. ``--self-test`` still runs its own copy; these tests do not depend on it.
Everything runs in tmp_path; no network, no real library.
"""

import stat

import pytest
from mutagen import File as MutagenFile
from mutagen.id3 import ID3, TIT2, TPE1, TXXX

from music_manager.cli import discogs_tag as cli
from music_manager.cli.discogs_tag import default_dest, self_test
from music_manager.fs import collect_albums, collect_extras
from music_manager.match import parse_album_dirname, similar
from music_manager.moves import _move_across_devices, _prune_empty
from music_manager.naming import (
    album_folder_name,
    folder_lead_year,
    format_album_title,
    format_artist,
    format_catalog,
    reissue_original_year,
    swapprefix,
    track_filename,
    track_prefix,
)
from music_manager.normalize import plan_folder_rename
from music_manager.playlists import (
    GENERATED_MARKER,
    album_playlist_tracks,
    encode_lines,
    is_generated_playlist,
    render_playlist,
)
from music_manager.providers.discogs import flatten_tracklist, parse_position, rank_by_year, save_token
from music_manager.tags.fields import clean_date
from music_manager.tags.read import already_tagged, read_easy_tags
from music_manager.tags.write import write_tags

# --- naming -------------------------------------------------------------------

@pytest.mark.parametrize("name, want", [("The Beatles", "Beatles, The"), ("Die Ärzte", "Ärzte, Die")])
def test_swapprefix(name, want):
    assert swapprefix(name) == want


@pytest.mark.parametrize("name, want", [
    ("The Grouch", "Grouch, The"),
    ("Aesop Rock (2)", "Aesop Rock"),
    ("Various", "Various Artists"),
    ("X meets Y", "X & Y"),
])
def test_format_artist(name, want):
    assert format_artist(name) == want


def test_format_album_title():
    assert format_album_title('Foo: "Bar"') == "Foo- Bar"


def test_format_catalog():
    assert format_catalog("BTR149; other") == "BTR149"
    assert format_catalog("none") is None


@pytest.mark.parametrize("args, want", [
    (("No Cause for Concern", "1981", "1993", "DOJO CD 167", 1), "[1981]  No Cause for Concern [1993] [DOJOCD167]"),
    (("Come On Die Young", "1999", "1999", "OLE365-2", 1), "[1999]  Come On Die Young [OLE365-2]"),
    (("Mermaid Avenue", "1998", "1998", "7559-62204-1", 2), "[1998]  Mermaid Avenue 2CD [7559-62204-1]"),
    (("Mermaid Avenue", "1990", "1998", "7559-62204-1", 2), "[1990]  Mermaid Avenue 2CD [1998] [7559-62204-1]"),
    (("My War", "", "1984", "SST 023", 1), "[1984]  My War [SST023]"),
    (("Nervous Breakdown", "1979", "1988", "SST CD 001", 1), "[1979]  Nervous Breakdown [1988] [SSTCD001]"),
    (("The First Four Years", "1983", "1990", None, 1), "[1983]  The First Four Years [1990]"),
    (("What The…", "2013", "2013", "", 1), "[2013]  What The…"),
    (("My War", "", "", "SST 023", 1), "My War [SST023]"),
    (("My War", "", "", None, 1), "My War"),
])
def test_album_folder_name(args, want):
    assert album_folder_name(*args) == want


@pytest.mark.parametrize("name, want", [
    ("[1984]  My War [1984] [SST023]", "[1984]  My War [SST023]"),
    ("[2013]  What The… [2013]", "[2013]  What The…"),
    ("[1998]  Mermaid Avenue 2CD [1998] [7559-62204-1]", "[1998]  Mermaid Avenue 2CD [7559-62204-1]"),
])
def test_plan_folder_rename_renames_old_style(name, want):
    plan = plan_folder_rename(name)
    assert (plan.action, plan.new_name) == ("rename", want)


@pytest.mark.parametrize("args, action", [
    (("[1979]  Nervous Breakdown [1988] [SSTCD001]",), "ok"),
    (("[1984]  My War [SST023]",), "ok"),
    (("[1991]  Black Sabbath [TECP-23892] [1970]",), "skip"),
    (("[1985]  X [1985] [C1]", ("2016", "1985")), "skip"),
])
def test_plan_folder_rename_ok_and_skip(args, action):
    assert plan_folder_rename(*args).action == action


@pytest.mark.parametrize("original, release, want", [
    ("1981", "1993", "1981"), ("1999", "1999", ""), ("", "1999", ""), ("2001", "1999", ""), ("1981", "", ""),
])
def test_reissue_original_year(original, release, want):
    assert reissue_original_year(original, release) == want


@pytest.mark.parametrize("original, release, want", [
    ("1984", "1984", "1984"), ("", "1984", "1984"), ("2001", "1999", "1999"), ("1981", "", "1981"),
])
def test_folder_lead_year(original, release, want):
    assert folder_lead_year(original, release) == want


def test_parse_album_dirname():
    parsed = parse_album_dirname("[1981]  No Cause for Concern [1993] [DOJOCD167]")
    assert (parsed["album"], parsed["originalyear"], parsed["year"], parsed["catno"]) == (
        "No Cause for Concern", "1981", "1993", "DOJOCD167")
    messy = parse_album_dirname("Doze - Iron & Rhymes (Aesop Rock + Iron & Wine) (2012) FLAC WEB")
    assert messy.get("artist") == "Doze"
    assert "Iron & Rhymes" in messy.get("album", "")


def test_parse_position():
    assert parse_position("A1")[0] == 1
    assert parse_position("1-3") == (1, 3)
    assert parse_position("CD2-4") == (2, 4)


# --- tags ---------------------------------------------------------------------

META = {
    "title": "Song", "artist": "Artist", "albumartist": "Artist", "album": "Album", "track": 1, "tracktotal": 1,
    "disc": 1, "disctotal": 1, "date": "1999", "originaldate": "1999", "label": "", "catalognumber": "",
    "genre": "", "release_id": 99, "master_id": 0,
}


def test_already_tagged_and_read_easy_tags(tmp_path):
    tagged = tmp_path / "tagged.mp3"
    tags = ID3()
    tags.add(TXXX(encoding=3, desc="DISCOGS_RELEASE_ID", text="12345"))
    tags["TIT2"] = TIT2(encoding=3, text="Song")
    tags["TPE1"] = TPE1(encoding=3, text="Artist")
    tags.save(tagged)
    assert already_tagged(tagged) == "12345"
    easy = read_easy_tags(tagged)
    assert (easy.get("title"), easy.get("artist")) == ("Song", "Artist")


def test_bare_mp3_gets_tagged(tmp_path):
    bare = tmp_path / "bare.mp3"
    bare.write_bytes(b"not an mpeg frame")
    assert already_tagged(bare) is None
    assert read_easy_tags(bare) == {}
    write_tags(bare, dict(META))
    assert already_tagged(bare) == "99"


@pytest.mark.parametrize("name, maker", [("t.wav", "_tiny_wav"), ("t.aiff", "_tiny_aiff")])
def test_wav_aiff_get_id3v24(tmp_path, name, maker):
    meta = {
        "title": "Ünïcode", "artist": "A", "albumartist": "A", "album": "B", "track": 2, "tracktotal": 9,
        "disc": 1, "disctotal": 1, "date": "2001-00-00", "originaldate": "1990", "label": "L",
        "catalognumber": "C1", "genre": "G", "release_id": 77, "master_id": 0, "position": "A2",
    }
    path = tmp_path / name
    getattr(cli, maker)(path)
    write_tags(path, meta)
    tags = MutagenFile(path).tags
    assert isinstance(tags, ID3) and tags.version[:2] == (2, 4)
    assert (str(tags["TIT2"]), str(tags["TRCK"]), str(tags["TDRC"])) == ("Ünïcode", "2/9", "2001")
    assert already_tagged(path) == "77"


def test_mp3_rewrite_drops_stale_master_keeps_replaygain(tmp_path):
    mp3 = tmp_path / "t.mp3"
    mp3.write_bytes(b"")
    old = ID3()
    old.add(TXXX(encoding=3, desc="DISCOGS_MASTER_ID", text="5"))
    old.add(TXXX(encoding=3, desc="REPLAYGAIN_TRACK_GAIN", text="-1 dB"))
    old.save(mp3)
    write_tags(mp3, {**META, "title": "Ünïcode", "release_id": 77, "position": "A2"})
    tags = ID3(mp3)
    assert "TXXX:DISCOGS_MASTER_ID" not in tags
    assert str(tags["TXXX:REPLAYGAIN_TRACK_GAIN"]) == "-1 dB"
    assert str(tags["TXXX:DISCOGS_POSITION"]) == "A2"


# --- tracklists and matching ----------------------------------------------------

def _track(pos, title, kind="track", **extra):
    return {"position": pos, "type_": kind, "title": title, **extra}


def test_vinyl_numbering_and_side_names():
    lp = {"tracklist": [_track("A1", "a"), _track("A2", "b"), _track("", "Side B", "heading"),
                        _track("B1", "c"), _track("B2", "d")]}
    tracks = flatten_tracklist(lp, "X")
    assert [(t.number, t.position) for t in tracks] == [(1, "A1"), (2, "A2"), (3, "B1"), (4, "B2")]
    assert (tracks[2].legacy_number, tracks[2].side) == (21, "B1")
    assert track_filename(track_prefix(tracks[2], side_names=True), "c", ".flac") == "B1. c.flac"
    assert track_filename(track_prefix(tracks[2], side_names=False), "c", ".flac") == "3. c.flac"


def test_index_tracks_flatten_sub_tracks():
    work = {"tracklist": [
        _track("1", "Intro"),
        _track("", "Suite", "index", sub_tracks=[_track("2a", "Part 1"), _track("2b", "Part 2")]),
        _track("3", "Outro"),
    ]}
    assert [(t.number, t.title) for t in flatten_tracklist(work, "X")] == [
        (1, "Intro"), (2, "Suite: Part 1"), (3, "Suite: Part 2"), (4, "Outro")]


def test_unicode_similarity():
    assert similar("荒井由実", "荒井由実") == 1.0
    assert similar("Кино", "кино") == 1.0
    assert similar("Björk", "Bjork") == 1.0
    assert similar("ひこうき雲", "ミスリム") < 0.5


def test_rank_by_year_and_clean_date():
    assert rank_by_year([{"year": "2000"}, {"year": "1990"}], "1990")[0]["year"] == "1990"
    assert clean_date("1999-00-00") == "1999"
    assert clean_date("1999-05-00") == "1999-05"


# --- files and folders ----------------------------------------------------------

def test_collect_extras(tmp_path):
    album_dir = tmp_path / "Artist - Album"
    (album_dir / "Scans").mkdir(parents=True)
    track = album_dir / "1. Song.flac"
    track.write_bytes(b"flac")
    (album_dir / "cover.jpg").write_bytes(b"jpg")
    (album_dir / "notes.txt").write_bytes(b"nfo")
    (album_dir / "Scans" / "front.jpg").write_bytes(b"scan")
    (album_dir / "CD1").mkdir()
    (album_dir / "CD1" / "2. Other.flac").write_bytes(b"flac")
    (album_dir / "CD1" / "rip.log").write_bytes(b"log")
    files, dirs = collect_extras(album_dir, [track, album_dir / "CD1" / "2. Other.flac"])
    assert {p.name for p in dirs} == {"Scans"}
    assert {str(p.relative_to(album_dir)) for p in files} == {"cover.jpg", "notes.txt", "CD1/rip.log"}


def test_collect_albums_art_folders(tmp_path):
    walk = tmp_path / "walk"
    for rel in ("Art/Album/1. a.flac", "Artist/Covers/1. b.flac", "Artist/Album/1. c.flac",
                "Artist/Album/Scans/1. not-a-track.flac"):
        (walk / rel).parent.mkdir(parents=True, exist_ok=True)
        (walk / rel).write_bytes(b"x")
    found = sorted(str(a.path.relative_to(walk)) for a in collect_albums(walk))
    assert found == ["Art/Album", "Artist/Album", "Artist/Covers"]


def test_generated_vs_handmade_playlists(tmp_path):
    album = tmp_path / "Album"
    album.mkdir()
    (album / "1. a.flac").write_bytes(b"x")
    (album / "2. b.flac").write_bytes(b"x")
    legacy = album / "Album.m3u"
    legacy.write_text("1. a.flac\n2. b.flac\n", encoding="utf-8")
    assert is_generated_playlist(legacy)
    mine = album / "mine.m3u"
    mine.write_text("1. a.flac\n2. b.flac\n", encoding="utf-8")
    assert not is_generated_playlist(mine)
    legacy.write_text("2. b.flac\n1. a.flac\n", encoding="utf-8")
    assert not is_generated_playlist(legacy)
    lines = render_playlist(album, album_playlist_tracks(album))
    assert lines[:2] == ["#EXTM3U", GENERATED_MARKER]
    marked = album / "x.m3u8"
    marked.write_bytes(encode_lines(lines))
    assert is_generated_playlist(marked)


def test_save_token_is_private(tmp_path):
    token = tmp_path / "cfg" / "token"
    save_token("abc", token)
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
    assert token.read_text() == "abc\n"


def test_prune_empty_stops_at_limit_and_keeps_files(tmp_path):
    arg = tmp_path / "bf-test" / "Black Flag"
    (arg / "Album" / "CD1").mkdir(parents=True)
    (arg / "Kept").mkdir()
    (arg / "Kept" / ".hidden").write_text("x")
    _prune_empty(arg / "Album", arg)
    assert not (arg / "Album").exists() and (arg / "Kept" / ".hidden").exists()
    (arg / "Kept" / ".hidden").unlink()
    _prune_empty(arg / "Kept", arg)
    assert not arg.exists() and (tmp_path / "bf-test").is_dir()


def test_move_across_devices_verifies_then_deletes(tmp_path):
    src = tmp_path / "xdev" / "a.flac"
    src.parent.mkdir()
    src.write_bytes(b"audio" * 1000)
    dest = tmp_path / "xdev-out" / "a.flac"
    dest.parent.mkdir()
    _move_across_devices(src, dest)
    assert not src.exists() and dest.read_bytes() == b"audio" * 1000


def test_default_dest(tmp_path):
    lib = tmp_path / "Library"
    lib.mkdir()
    assert default_dest(tmp_path / "Artist - Album", library=lib) == lib.resolve()
    featuring = lib / "Aesop Rock" / "Featuring" / "Some Album"
    assert default_dest(featuring, library=lib) == (lib / "Aesop Rock" / "Featuring").resolve()


def test_self_test_cli_entry_still_passes(capsys):
    assert self_test() == 0
    assert capsys.readouterr().out.strip().endswith("self-test ok")
