"""--normalize-folders / --undo-journal on temp trees (never a real library)."""

import json

import pytest
from mutagen import File as MutagenFile
from test_apply import make_album

from music_manager import journal as journal_mod
from music_manager.cli.discogs_tag import main
from music_manager.normalize import plan_folder_rename
from music_manager.playlists import (
    album_playlist_tracks,
    is_generated_playlist,
    playlist_path,
    render_playlist,
    write_playlist_file,
)


@pytest.mark.parametrize(
    "name, action, new",
    [
        ("[1984]  My War [1984] [SST023]", "rename", "[1984]  My War [SST023]"),
        ("[1989]  As Nasty as They Wanna Be [1989]", "rename", "[1989]  As Nasty as They Wanna Be"),
        ("[2013]  What The… [2013] [SST391]", "rename", "[2013]  What The… [SST391]"),
        ("[2023]  Integrated Tech Solutions 18CD [2023]", "rename", "[2023]  Integrated Tech Solutions 18CD"),
        ("[1998]  Mermaid Avenue 2CD [1998] [7559-62204-1]", "rename", "[1998]  Mermaid Avenue 2CD [7559-62204-1]"),
        ("[]  Unreleased, Compilations, And Features []", "rename", "Unreleased, Compilations, And Features"),
        ("[1979]  Nervous Breakdown [1988] [SSTCD001]", "ok", ""),
        ("[1983]  The First Four Years [1990]", "ok", ""),
        ("[1984]  My War [SST023]", "ok", ""),
        ("[2003]  Sad Clown Bad Dub 7", "ok", ""),
        ("[2012]  The Money Store [88691963512]", "ok", ""),
        ("[1998]  Hated in the Nation [1987]", "skip", ""),
        ("[]  Don't Be Mistaken [1983]", "skip", ""),
        ("[]  [standalone recordings] []", "skip", ""),
        ("[1991]  Black Sabbath [TECP-23892] [1970]", "skip", ""),
        ("(2002]  Transplants [80448-2][2002]", "skip", ""),
        ("[2012 · No cat#] Skelethon (Deluxe Version)", "skip", ""),
        ("[2023]  Exul 2CD [24-192 Vinyl FLAC] [SOM573LP]", "skip", ""),
        ("[1999]  Live [1999] [1999]", "skip", ""),
        ("[1998] (test) The Miseducation of Lauryn Hill [SRCS-8788] [1998]", "skip", ""),
        ("Black Flag - 2007 - The Failure", "skip", ""),
        ("2014 Forest Hills Drive", "skip", ""),
    ],
)
def test_plan_folder_rename(name, action, new):
    plan = plan_folder_rename(name)
    assert plan.action == action, plan
    assert plan.new_name == new
    if action == "skip":
        assert plan.reason


def test_plan_respects_reissue_tags():
    name = "[1985]  Side Story [1985] [WAX001]"
    assert plan_folder_rename(name, ("2016", "1985")).action == "skip"
    assert plan_folder_rename(name, ("1985", "1985")).new_name == "[1985]  Side Story [WAX001]"
    assert plan_folder_rename(name, ("1985", "")).action == "rename"


def generated_playlist(album):
    tracks = album_playlist_tracks(album)
    write_playlist_file(playlist_path(album), render_playlist(album, tracks))
    return playlist_path(album)


@pytest.fixture
def library(tmp_path):
    lib = tmp_path / "Music"
    mywar = make_album(lib / "Black Flag" / "[1984]  My War [1984] [SST023]", ["1. My War.flac", "2. Scream.flac"])
    (mywar / ".hidden").write_text("x")
    generated_playlist(mywar)
    make_album(lib / "Black Flag" / "[1979]  Nervous Breakdown [1988] [SSTCD001]", ["1. Nervous Breakdown.flac"])
    make_album(lib / "Black Flag" / "[2013]  What The… [2013]", ["1. Hollywood.flac"])
    tagged = make_album(lib / "Black Flag" / "[1983]  Tagged Reissue [1983] [X1]", ["1. a.flac"])
    audio = MutagenFile(tagged / "1. a.flac")
    audio.tags["DATE"] = ["1990"]
    audio.tags["ORIGINALDATE"] = ["1983"]
    audio.save()
    make_album(lib / "Black Flag" / "Black Flag - 2007 - Something", ["01 a.flac"])
    make_album(lib / "Black Flag" / "[1981]  Damaged [1981] [SST007]", ["1. Rise Above.flac"])
    make_album(lib / "Black Flag" / "[1981]  Damaged [SST007]", ["1. Rise Above.flac"])  # conflict
    make_album(lib / "Aesop Rock" / "[]  Unreleased []", ["01 x.flac"])
    return lib


def snapshot(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def test_preview_changes_nothing(library, capsys):
    before = snapshot(library)
    assert main(["--normalize-folders", str(library)]) == 0
    out = capsys.readouterr().out
    assert snapshot(library) == before
    assert "PREVIEW" in out
    assert "-> [1984]  My War [SST023]" in out
    assert "playlist [1984]  My War [1984] [SST023].m3u -> [1984]  My War [SST023].m3u" in out
    assert "-> [2013]  What The…" in out and "-> Unreleased" in out
    assert "3 to rename, 2 already fine, 2 skipped, 1 conflicts" in out
    assert "tags say reissue" in out and "already exists" in out


def test_apply_conflict_and_undo(library, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(journal_mod, "STATE_DIR", tmp_path / "state")
    before = snapshot(library)
    assert main(["--normalize-folders", "--apply", str(library)]) == 0
    out = capsys.readouterr().out
    bf = library / "Black Flag"
    new = bf / "[1984]  My War [SST023]"
    assert (new / ".hidden").is_file() and not (bf / "[1984]  My War [1984] [SST023]").exists()
    playlist = new / "[1984]  My War [SST023].m3u"
    assert playlist.is_file() and is_generated_playlist(playlist)
    assert (bf / "[2013]  What The…").is_dir() and (library / "Aesop Rock" / "Unreleased").is_dir()
    # Conflict and skipped folders untouched; files never retagged.
    assert (bf / "[1981]  Damaged [1981] [SST007]").is_dir() and (bf / "[1981]  Damaged [SST007]").is_dir()
    assert (bf / "[1983]  Tagged Reissue [1983] [X1]").is_dir()
    assert (bf / "Black Flag - 2007 - Something").is_dir()

    journals = list((tmp_path / "state" / "journal").glob("normalize-folders-*.jsonl"))
    assert len(journals) == 1 and str(journals[0]) in out
    entries = [json.loads(line) for line in journals[0].read_text(encoding="utf-8").splitlines()]
    assert entries[0]["action"] == "start"
    renames = [e for e in entries if e["action"] == "rename"]
    assert [e["kind"] for e in renames].count("folder") == 3 and [e["kind"] for e in renames].count("playlist") == 1
    assert all(e["old"].startswith("/") and e["new"].startswith("/") and e["time"] for e in renames)

    # Undo preview changes nothing, apply restores everything exactly.
    after = snapshot(library)
    assert main(["--undo-journal", str(journals[0])]) == 0
    assert snapshot(library) == after
    assert main(["--undo-journal", str(journals[0]), "--apply"]) == 0
    assert snapshot(library) == before
    assert is_generated_playlist(bf / "[1984]  My War [1984] [SST023]" / "[1984]  My War [1984] [SST023].m3u")


def test_undo_skips_what_changed_since(library, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(journal_mod, "STATE_DIR", tmp_path / "state")
    main(["--normalize-folders", "--apply", str(library)])
    journal = next((tmp_path / "state" / "journal").glob("normalize-folders-*.jsonl"))
    bf = library / "Black Flag"
    # The user re-created an old name and moved another folder away.
    (bf / "[2013]  What The… [2013]").mkdir()
    (library / "Aesop Rock" / "Unreleased").rename(library / "Aesop Rock" / "Moved")
    capsys.readouterr()
    assert main(["--undo-journal", str(journal), "--apply"]) == 1
    out = capsys.readouterr().out
    assert "exists again" in out and "no longer exists" in out
    assert (bf / "[1984]  My War [1984] [SST023]").is_dir()  # the rest was undone
    assert (bf / "[2013]  What The…").is_dir() and (library / "Aesop Rock" / "Moved").is_dir()


def test_apply_on_tree_with_nothing_to_do(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(journal_mod, "STATE_DIR", tmp_path / "state")
    make_album(tmp_path / "Music" / "A" / "[1984]  My War [SST023]", ["1. a.flac"])
    assert main(["--normalize-folders", "--apply", str(tmp_path / "Music")]) == 0
    assert "0 to rename, 1 already fine" in capsys.readouterr().out
    assert not (tmp_path / "state").exists()  # no journal when nothing was renamed


def test_apply_flag_alone_is_rejected(tmp_path):
    assert main([str(tmp_path), "--apply"]) == 2
