"""discogs-tag: tag and rename album folders using Discogs, with this library's Picard rules.

Get a token (not an OAuth app):
  https://www.discogs.com/settings/developers  →  Generate token

  discogs-tag --save-token
  discogs-tag --dry-run "/path/to/album"
  discogs-tag "/path/to/Featuring"
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any

from mutagen import File as MutagenFile
from mutagen.id3 import ID3, TIT2, TPE1, TXXX

from .. import naming
from ..apply import AlbumApplyError, apply_album, disc_warning, track_count_message
from ..conflicts import describe_disc_conflict
from ..fs import AlbumFolder, collect_albums, collect_extras
from ..journal import undo_journal
from ..match import (
    AUTO_MIN_MARGIN,
    AUTO_MIN_SCORE,
    align_tracks_detailed,
    ambiguous_disc_files,
    format_score_components,
    guess_from_album_dir,
    parse_album_dirname,
    score_components,
    score_result,
    similar,
)
from ..moves import _move_across_devices, _prune_empty
from ..naming import (
    album_folder_name,
    folder_lead_year,
    format_album_title,
    format_artist,
    format_catalog,
    reissue_original_year,
    swapprefix,
    track_filename,
    track_prefix,
    year4,
)
from ..normalize import normalize_folders, plan_folder_rename
from ..playlists import (
    GENERATED_MARKER,
    album_playlist_tracks,
    encode_lines,
    is_generated_playlist,
    render_playlist,
)
from ..providers.discogs import (
    RELEASE_URL,
    Discogs,
    DiscogsError,
    DiscogsRelease,
    SearchResult,
    flatten_tracklist,
    load_token,
    parse_position,
    rank_by_year,
    release_artists,
    release_catno,
    release_discs,
    save_token,
    split_result_title,
    token_path,
)
from ..tags.fields import TrackMeta, clean_date
from ..tags.read import album_release_ids, already_tagged, read_easy_tags
from ..tags.write import write_tags

# There is deliberately no built-in library path: a missing --dest must never
# send albums into whatever library happens to be mounted. $DEFAULT_MUSIC is
# used only when the user has set it.
_env_music = os.getenv("DEFAULT_MUSIC", "").strip()
DEFAULT_MUSIC: Path | None = Path(_env_music).expanduser() if _env_music else None


def default_dest(start: Path, library: Path | None = None) -> Path | None:
    """Destination when --dest is not given, or None when there isn't a safe one.

    The Featuring directory the path sits under, else $DEFAULT_MUSIC when the
    user set it (and it exists). Never a guessed or built-in library.
    """
    library = DEFAULT_MUSIC if library is None else library
    resolved = start.resolve()
    parts = list(resolved.parts)
    if "Featuring" in parts:
        return Path(*parts[: parts.index("Featuring") + 1])
    if library is not None and library.is_dir():
        return library.resolve()
    return None


# ---------------------------------------------------------------------------
# Interactive flow
# ---------------------------------------------------------------------------

def print_candidates(results: list[tuple[float, SearchResult]]) -> None:
    for i, (score, item) in enumerate(results, 1):
        artist, album = split_result_title(item.get("title") or "")
        fmt = ", ".join(item.get("format") or [])
        print(
            f"  {i:2}. {int(score * 100):3}%  {artist} – {album}  "
            f"[{item.get('year') or '?'}]  {item.get('catno') or '-'}  "
            f"{item.get('country') or ''}  {fmt}"
        )
        print(f"       https://www.discogs.com/release/{item.get('id')}")


def prompt(text: str) -> str:
    try:
        return input(text).strip()
    except EOFError:
        return "q"


def choose_release(
    api: Discogs,
    guess: dict[str, str],
    results: list[SearchResult],
    auto: bool,
    release_id: int | None,
) -> DiscogsRelease | None:
    if release_id:
        return api.release(release_id)

    ranked = sorted(((score_result(r, guess), r) for r in results), key=lambda x: -x[0])
    if auto and not ranked:
        print("  no search results; nothing to auto-match")
        return None
    if auto and ranked:
        top = ranked[0][0]
        second = ranked[1][0] if len(ranked) > 1 else 0.0
        print(f"  best  {top:.2f}  [{format_score_components(score_components(ranked[0][1], guess))}]  "
              f"{ranked[0][1].get('title')}")
        if len(ranked) > 1:
            print(f"  next  {second:.2f}  [{format_score_components(score_components(ranked[1][1], guess))}]  "
                  f"{ranked[1][1].get('title')}")
        if top >= AUTO_MIN_SCORE and top - second >= AUTO_MIN_MARGIN:
            print(f"  auto  {int(top * 100)}%  margin {top - second:.2f}  {ranked[0][1].get('title')}")
            return api.release(int(ranked[0][1]["id"]))
        print(f"  no confident auto-match (need score >= {AUTO_MIN_SCORE:.2f} and margin >= {AUTO_MIN_MARGIN:.2f})")
        return None

    if not sys.stdin.isatty():
        raise DiscogsError("No TTY. Pass --release-id or --auto.")

    while True:
        if ranked:
            print_candidates(ranked[:12])
        else:
            print("  (no results)")
        print("  [1-N] choose   s skip   /query   i <id or url>   q quit")
        choice = prompt("> ")
        if not choice or choice.lower() == "s":
            return None
        if choice.lower() in {"q", "quit"}:
            raise KeyboardInterrupt
        if choice.startswith("/"):
            query = choice[1:].strip()
            if " - " in query:
                artist, album = query.split(" - ", 1)
            else:
                artist, album = guess.get("artist", ""), query
            results = api.search_releases(artist.strip(), album.strip(), guess.get("catno", ""), guess.get("year", ""))
            ranked = sorted(((score_result(r, guess), r) for r in results), key=lambda x: -x[0])
            continue
        if choice.lower().startswith("i"):
            raw = choice[1:].strip()
            match = RELEASE_URL.search(raw) or re.fullmatch(r"\d+", raw)
            if not match:
                print("  need a release id or discogs.com/release/… URL")
                continue
            return api.release(int(match.group(1) if match.lastindex else match.group(0)))
        if choice.isdigit() and 1 <= int(choice) <= len(ranked):
            return api.release(int(ranked[int(choice) - 1][1]["id"]))
        print("  not understood")


def process_album(
    api: Discogs,
    album: AlbumFolder,
    dest_root: Path,
    dry_run: bool,
    auto: bool,
    yes: bool,
    skip_tagged: bool,
    release_id: int | None,
    clear_other: bool = False,
    scan_root: Path | None = None,
    allow_track_mismatch: bool = False,
) -> str:
    """Returns "applied", "skipped", or "skipped-tagged" (not counted by --limit).

    In --auto mode a release whose track count differs from the local files
    is not applied unless allow_track_mismatch is set.
    """
    if album.layout_problem:
        print()
        print(f"warning: {album.path}: {album.layout_problem}")
        conflict = album.layout_conflict
        if conflict is not None:
            what = "disc folders with audio" if conflict.reason == "disc_folders" else "other folders with audio"
            print(f"  held because of: {', '.join(f'{d.name}/' for d in conflict.subfolders)} ({what})")
            print(f"  loose files: {', '.join(f.name for f in conflict.loose_files)}")
        print("  skipped: nothing written")
        return "skipped"

    if skip_tagged:
        tagged = album_release_ids(album.tracks)
        ids = set(tagged.values())
        if len(ids) == 1 and None not in ids:
            print(f"skip tagged (Discogs {ids.pop()}): {album.path}")
            return "skipped-tagged"
        if ids - {None}:
            untagged = sum(1 for v in tagged.values() if v is None)
            found = ", ".join(sorted(i for i in ids if i))
            print(f"mixed tags: {album.path}: release id(s) {found}; {untagged} of {len(tagged)} file(s) untagged"
                  " - not skipped")

    guess = guess_from_album_dir(album.path, album.tracks)
    print()
    print("=" * 72)
    print(f"folder  {album.path}")
    print(f"guess   {guess['artist']}  –  {guess['album']}  [{guess.get('year') or '?'}]  {guess.get('catno') or ''}")
    print(f"tracks  {len(album.tracks)}")

    results = []
    if not release_id:
        results = api.search_releases(guess["artist"], guess["album"], guess.get("catno", ""), guess.get("year", ""))
        if not results and guess["album"]:
            results = api.search_releases("", guess["album"], guess.get("catno", ""), guess.get("year", ""))

    try:
        release = choose_release(api, guess, results, auto=auto, release_id=release_id)
    except KeyboardInterrupt:
        raise
    if release is None:
        print("  skipped")
        return "skipped"

    album_artist = release_artists(release)
    dtracks = flatten_tracklist(release, album_artist)
    release_year = year4(release.get("released") or release.get("year"))
    original_year = ""  # the master's year; only used when it is earlier (reissue)
    master_id = release.get("master_id")
    if master_id:
        try:
            master = api.master(int(master_id))
            original_year = year4(master.get("year"))
        except DiscogsError as exc:
            print(f"  warning: master fetch failed: {exc}")

    reissue_of = reissue_original_year(original_year, release_year)
    print(f"  match  {album_artist} – {release.get('title')}  [{release.get('year')}]")
    print(f"         orig {reissue_of + ' (reissue)' if reissue_of else '- (original release)'}  "
          f"cat {release_catno(release) or '-'}  "
          f"{len(dtracks)} tracks / {release_discs(release, dtracks)} disc(s)")
    print(f"         https://www.discogs.com/release/{release.get('id')}")

    if len(dtracks) != len(album.tracks):
        print(f"  warning: {len(album.tracks)} local files vs {len(dtracks)} Discogs tracks")
        if auto and not release_id and not allow_track_mismatch:
            print("  skipped: track count mismatch in --auto mode (use --allow-track-mismatch to apply anyway)")
            return "skipped"

    triples, _legacy, conflicts, holds = align_tracks_detailed(album, dtracks)
    unsure = ambiguous_disc_files(triples)
    if unsure:
        print(f"  warning: {disc_warning(album, unsure, conflicts)}")
        for conflict in conflicts:
            print(f"    {describe_disc_conflict(conflict, album.path)}")
        print("  skipped: nothing written")
        return "skipped"
    if holds:
        print(f"  warning: {track_count_message(album, holds)}")
        print("  skipped: nothing written")
        return "skipped"

    if not yes and sys.stdin.isatty() and not dry_run:
        ans = prompt("  apply tags + rename? [Y/n] ").lower()
        if ans in {"n", "no"}:
            return "skipped"

    apply_album(
        album, dest_root, release, dtracks, original_year,
        dry_run=dry_run, clear_other=clear_other, scan_root=scan_root,
    )
    return "applied"


def self_test() -> int:
    assert swapprefix("The Beatles") == "Beatles, The"
    assert swapprefix("Die Ärzte") == "Ärzte, Die"
    assert format_artist("The Grouch") == "Grouch, The"
    assert format_artist("Aesop Rock (2)") == "Aesop Rock"
    assert format_artist("Various") == "Various Artists"
    assert format_artist("X meets Y") == "X & Y"
    assert format_album_title('Foo: "Bar"') == "Foo- Bar"
    assert format_catalog("BTR149; other") == "BTR149"
    assert format_catalog("none") is None
    name = album_folder_name("No Cause for Concern", "1981", "1993", "DOJO CD 167", 1)
    assert name == "[1981]  No Cause for Concern [1993] [DOJOCD167]", name
    # Release-year bracket only for reissues; unknown parts left out entirely.
    for args, want in (
        (("Come On Die Young", "1999", "1999", "OLE365-2", 1), "[1999]  Come On Die Young [OLE365-2]"),
        (("Mermaid Avenue", "1998", "1998", "7559-62204-1", 2), "[1998]  Mermaid Avenue 2CD [7559-62204-1]"),
        (("Mermaid Avenue", "1990", "1998", "7559-62204-1", 2), "[1990]  Mermaid Avenue 2CD [1998] [7559-62204-1]"),
        (("My War", "", "1984", "SST 023", 1), "[1984]  My War [SST023]"),
        (("Nervous Breakdown", "1979", "1988", "SST CD 001", 1), "[1979]  Nervous Breakdown [1988] [SSTCD001]"),
        (("The First Four Years", "1983", "1990", None, 1), "[1983]  The First Four Years [1990]"),
        (("What The…", "2013", "2013", "", 1), "[2013]  What The…"),
        (("My War", "", "", "SST 023", 1), "My War [SST023]"),
        (("My War", "", "", None, 1), "My War"),
    ):
        assert album_folder_name(*args) == want, (args, album_folder_name(*args))
    # Library cleanup (--normalize-folders): pure planning on names.
    for name, want in (
        ("[1984]  My War [1984] [SST023]", "[1984]  My War [SST023]"),
        ("[2013]  What The… [2013]", "[2013]  What The…"),
        ("[1998]  Mermaid Avenue 2CD [1998] [7559-62204-1]", "[1998]  Mermaid Avenue 2CD [7559-62204-1]"),
    ):
        plan = plan_folder_rename(name)
        assert (plan.action, plan.new_name) == ("rename", want), (name, plan)
    assert plan_folder_rename("[1979]  Nervous Breakdown [1988] [SSTCD001]").action == "ok"
    assert plan_folder_rename("[1984]  My War [SST023]").action == "ok"
    assert plan_folder_rename("[1991]  Black Sabbath [TECP-23892] [1970]").action == "skip"
    assert plan_folder_rename("[1985]  X [1985] [C1]", ("2016", "1985")).action == "skip"
    # Tags: original date only for reissues.
    assert reissue_original_year("1981", "1993") == "1981"
    assert reissue_original_year("1999", "1999") == "" and reissue_original_year("", "1999") == ""
    assert reissue_original_year("2001", "1999") == "" and reissue_original_year("1981", "") == ""
    assert folder_lead_year("1984", "1984") == "1984" and folder_lead_year("", "1984") == "1984"
    assert folder_lead_year("2001", "1999") == "1999" and folder_lead_year("1981", "") == "1981"
    parsed = parse_album_dirname("[1981]  No Cause for Concern [1993] [DOJOCD167]")
    assert parsed["album"] == "No Cause for Concern"
    assert parsed["originalyear"] == "1981"
    assert parsed["year"] == "1993"
    assert parsed["catno"] == "DOJOCD167"
    messy = parse_album_dirname("Doze - Iron & Rhymes (Aesop Rock + Iron & Wine) (2012) FLAC WEB")
    assert messy.get("artist") == "Doze"
    assert "Iron & Rhymes" in messy.get("album", "")
    assert parse_position("A1")[0] == 1
    assert parse_position("1-3") == (1, 3)
    assert parse_position("CD2-4") == (2, 4)

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tagged = Path(tmp) / "tagged.mp3"
        tags = ID3()
        tags.add(TXXX(encoding=3, desc="DISCOGS_RELEASE_ID", text="12345"))
        tags["TIT2"] = TIT2(encoding=3, text="Song")
        tags["TPE1"] = TPE1(encoding=3, text="Artist")
        tags.save(tagged)
        assert already_tagged(tagged) == "12345"
        assert read_easy_tags(tagged).get("title") == "Song"
        assert read_easy_tags(tagged).get("artist") == "Artist"

        bare = Path(tmp) / "bare.mp3"
        bare.write_bytes(b"not an mpeg frame")
        assert already_tagged(bare) is None
        assert read_easy_tags(bare) == {}
        write_tags(
            bare,
            {
                "title": "Song",
                "artist": "Artist",
                "albumartist": "Artist",
                "album": "Album",
                "track": 1,
                "tracktotal": 1,
                "disc": 1,
                "disctotal": 1,
                "date": "1999",
                "originaldate": "1999",
                "label": "",
                "catalognumber": "",
                "genre": "",
                "release_id": 99,
                "master_id": 0,
            },
        )
        assert already_tagged(bare) == "99"

        album_dir = Path(tmp) / "Artist - Album"
        scans = album_dir / "Scans"
        scans.mkdir(parents=True)
        track = album_dir / "1. Song.flac"
        track.write_bytes(b"flac")
        (album_dir / "cover.jpg").write_bytes(b"jpg")
        (album_dir / "notes.txt").write_bytes(b"nfo")
        (scans / "front.jpg").write_bytes(b"scan")
        leftover = album_dir / "CD1"
        leftover.mkdir()
        (leftover / "2. Other.flac").write_bytes(b"flac")
        (leftover / "rip.log").write_bytes(b"log")
        files, dirs = collect_extras(album_dir, [track, leftover / "2. Other.flac"])
        assert {p.name for p in dirs} == {"Scans"}
        names = {str(p.relative_to(album_dir)) for p in files}
        assert names == {"cover.jpg", "notes.txt", "CD1/rip.log"}

    _self_test_fixes()

    with tempfile.TemporaryDirectory() as tmp:
        lib = Path(tmp) / "Library"
        lib.mkdir()
        assert default_dest(Path(tmp) / "Artist - Album", library=lib) == lib.resolve()
        featuring = lib / "Aesop Rock" / "Featuring" / "Some Album"
        assert default_dest(featuring, library=lib) == (lib / "Aesop Rock" / "Featuring").resolve()

    print("self-test ok")
    return 0


def _tiny_wav(path: Path) -> None:
    import wave

    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * 8)


def _tiny_aiff(path: Path) -> None:
    import struct

    frames = 8
    rate_8000 = b"\x40\x0b\xfa\x00\x00\x00\x00\x00\x00\x00"  # 80-bit float 8000.0
    comm = struct.pack(">hLh", 1, frames, 16) + rate_8000
    ssnd = struct.pack(">LL", 0, 0) + b"\x00\x00" * frames
    body = b"AIFF" + b"COMM" + struct.pack(">L", len(comm)) + comm + b"SSND" + struct.pack(">L", len(ssnd)) + ssnd
    path.write_bytes(b"FORM" + struct.pack(">L", len(body)) + body)


def _self_test_fixes() -> None:
    """Checks for the tagging/matching/playlist fixes. No network, no samples."""
    import stat
    import tempfile

    def track(pos, title, kind="track", **extra) -> Any:
        return {"position": pos, "type_": kind, "title": title, **extra}

    # Vinyl: sequential across sides, original position kept.
    lp: DiscogsRelease = {"tracklist": [track("A1", "a"), track("A2", "b"), track("", "Side B", "heading"), track("B1", "c"), track("B2", "d")]}
    tracks = flatten_tracklist(lp, "X")
    assert [(t.number, t.position) for t in tracks] == [(1, "A1"), (2, "A2"), (3, "B1"), (4, "B2")], tracks
    assert tracks[2].legacy_number == 21
    assert tracks[2].side == "B1"
    assert track_filename(track_prefix(tracks[2], side_names=True), "c", ".flac") == "B1. c.flac"
    assert track_filename(track_prefix(tracks[2], side_names=False), "c", ".flac") == "3. c.flac"

    # Index tracks: sub-tracks included, numbered in order.
    work: DiscogsRelease = {"tracklist": [
        track("1", "Intro"),
        track("", "Suite", "index", sub_tracks=[track("2a", "Part 1"), track("2b", "Part 2")]),
        track("3", "Outro"),
    ]}
    assert [(t.number, t.title) for t in flatten_tracklist(work, "X")] == [
        (1, "Intro"), (2, "Suite: Part 1"), (3, "Suite: Part 2"), (4, "Outro")]

    # Unicode similarity.
    assert similar("荒井由実", "荒井由実") == 1.0
    assert similar("Кино", "кино") == 1.0
    assert similar("Björk", "Bjork") == 1.0
    assert similar("ひこうき雲", "ミスリム") < 0.5
    assert rank_by_year([{"year": "2000"}, {"year": "1990"}], "1990")[0]["year"] == "1990"
    assert clean_date("1999-00-00") == "1999" and clean_date("1999-05-00") == "1999-05"

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        # Folder skip: "Art"/"Covers" are artwork only inside an album folder.
        for rel in ("Art/Album/1. a.flac", "Artist/Covers/1. b.flac", "Artist/Album/1. c.flac",
                    "Artist/Album/Scans/1. not-a-track.flac"):
            (root / "walk" / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / "walk" / rel).write_bytes(b"x")
        found = sorted(str(a.path.relative_to(root / "walk")) for a in collect_albums(root / "walk"))
        assert found == ["Art/Album", "Artist/Album", "Artist/Covers"], found

        # Playlists: generated vs hand-made.
        album = root / "pl" / "Album"
        album.mkdir(parents=True)
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

        # Tag writing: ID3v2.4 in WAV and AIFF, ID3 in MP3, nothing Vorbis-style.
        meta: TrackMeta = {
            "title": "Ünïcode", "artist": "A", "albumartist": "A", "album": "B", "track": 2, "tracktotal": 9,
            "disc": 1, "disctotal": 1, "date": "2001-00-00", "originaldate": "1990", "label": "L",
            "catalognumber": "C1", "genre": "G", "release_id": 77, "master_id": 0, "position": "A2",
        }
        for name, make in (("t.wav", _tiny_wav), ("t.aiff", _tiny_aiff)):
            path = root / name
            make(path)
            write_tags(path, meta)
            tags = MutagenFile(path).tags
            assert isinstance(tags, ID3) and tags.version[:2] == (2, 4), name
            assert str(tags["TIT2"]) == "Ünïcode" and str(tags["TRCK"]) == "2/9" and str(tags["TDRC"]) == "2001"
            assert already_tagged(path) == "77"
        mp3 = root / "t.mp3"
        mp3.write_bytes(b"")
        old = ID3()
        old.add(TXXX(encoding=3, desc="DISCOGS_MASTER_ID", text="5"))
        old.add(TXXX(encoding=3, desc="REPLAYGAIN_TRACK_GAIN", text="-1 dB"))
        old.save(mp3)
        write_tags(mp3, meta)
        tags = ID3(mp3)
        assert "TXXX:DISCOGS_MASTER_ID" not in tags  # stale id from another release
        assert "TXXX:REPLAYGAIN_TRACK_GAIN" in tags  # never touched by default
        assert str(tags["TXXX:DISCOGS_POSITION"]) == "A2"

        # Token file is private from the start.
        token = root / "cfg" / "token"
        save_token("abc", token)
        assert stat.S_IMODE(token.stat().st_mode) == 0o600 and token.read_text() == "abc\n"

        # Empty-folder cleanup: only truly empty folders, never past the limit.
        arg = root / "bf-test" / "Black Flag"
        (arg / "Album" / "CD1").mkdir(parents=True)
        (arg / "Kept").mkdir()
        (arg / "Kept" / ".hidden").write_text("x")
        _prune_empty(arg / "Album", arg)
        assert not (arg / "Album").exists() and (arg / "Kept" / ".hidden").exists()
        (arg / "Kept" / ".hidden").unlink()
        _prune_empty(arg / "Kept", arg)
        assert not arg.exists() and (root / "bf-test").is_dir()

        # Cross-drive move: verified copy, then the source is deleted.
        src = root / "xdev" / "a.flac"
        src.parent.mkdir()
        src.write_bytes(b"audio" * 1000)
        dest = root / "xdev-out" / "a.flac"
        dest.parent.mkdir()
        _move_across_devices(src, dest)
        assert not src.exists() and dest.read_bytes() == b"audio" * 1000


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="discogs-tag",
        description="Tag and rename albums from Discogs using this library's folder rules.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  discogs-tag --save-token
  discogs-tag --dry-run "/path/to/album"
  discogs-tag --auto --yes "/path/to/Featuring"
  discogs-tag --release-id 249504 --dest "$DEFAULT_MUSIC" "/path/to/album"
  discogs-tag --normalize-folders "/path/to/Music"           (preview)
  discogs-tag --normalize-folders --apply "/path/to/Music"
  discogs-tag --undo-journal ~/.local/state/discogs-tag/journal/FILE.jsonl --apply
  discogs-tag --check-disc-swaps "/path/to/Music"             (read-only report)
  discogs-tag --check-disc-swaps --report ~/disc-swaps.txt "/path/to/Music"

credentials:
  Create a personal token at
  https://www.discogs.com/settings/developers
  then run --save-token. The token is stored at
  ~/.config/discogs-tag/token (mode 600).
  DISCOGS_TOKEN overrides that file.
  DISCOGS_KEY and DISCOGS_SECRET are the consumer-key alternative.

where files go:
  DESTINATION/AlbumArtist/[LEADYEAR]  Album [RELEASEYEAR] [CATNO]/N. Title.ext
  LEADYEAR is the master's year for a reissue (master year earlier than
  the release year), otherwise the release's own year, so albums sort
  chronologically. [RELEASEYEAR] and the ORIGINALDATE tags only appear for
  a reissue. Unknown parts are left out with their brackets:
    [1984]  My War [SST023]                    not a reissue
    [1979]  Nervous Breakdown [1988] [SSTCD001] reissue
    [2013]  What The…                          no catalog number
  After a move, the old album folder and every parent that became empty
  are removed, up to and including the folder passed on the command line
  (never the library root, never a folder still holding any file).
  Moves to another drive copy, verify (size + SHA-256), then delete.
  DESTINATION is --dest, else the Featuring directory when the path is
  under one, else $DEFAULT_MUSIC if you set it. There is no built-in
  library. With none of these, discogs-tag stops before touching anything.
  Extra files and folders (Scans, Covers, logs, cues, …) move with the
  album. Vinyl/cassette sides are numbered
  by side (A1. Title, A2. Title, B1. Title); the track-number tag counts
  straight through (1 2 3) for players and the side is kept in
  DISCOGS_POSITION. --sequential-sides names them 1. 2. 3. instead.

safety:
  All files of an album are tagged before anything moves. Only the tags
  this tool writes are replaced. Comments, MusicBrainz ids and other text
  tags are kept unless --clear-other-tags; ReplayGain, AccurateRip,
  encoder info, art, lyrics and ISRC are always kept. One album failing
  does not stop the run.

library cleanup (no Discogs, no tag changes):
  --normalize-folders ROOT lists every album folder under ROOT whose name
  follows an older rule ("[1984]  My War [1984] [SST023]") and the new
  name ("[1984]  My War [SST023]"), plus everything skipped and why and
  every conflict. Nothing changes without --apply. Equal years are a
  non-reissue unless the album's tags have ORIGINALDATE/ORIGINALYEAR
  earlier than DATE (then the folder is skipped). Existing targets are
  never overwritten or merged. A generated album playlist named after the
  folder is renamed with it. Every rename is recorded in a JSON-lines
  journal in ~/.local/state/discogs-tag/journal/ ($XDG_STATE_HOME);
  --undo-journal FILE --apply reverses it newest first, skipping anything
  that changed since. Without --apply, --undo-journal only previews.

Undo:
  Every real apply also writes a journal (tags before and after, moves,
  folders, playlists). Its path is printed at the end of the run, and
  --undo-journal FILE undoes the whole apply the same way.
""".strip(),
    )
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=None,
        help="album folder or tree to process",
    )
    parser.add_argument("--dest", type=Path, help="library root to write into (AlbumArtist/Album/...)")
    parser.add_argument("--dry-run", action="store_true", help="search and show plan, write nothing")
    parser.add_argument(
        "--sequential-sides",
        action="store_true",
        help="name vinyl/cassette tracks 1. 2. 3. instead of A1. A2. B1. (tags are the same either way)",
    )
    parser.add_argument("--auto", action="store_true", help="apply only high-confidence matches")
    parser.add_argument("-y", "--yes", action="store_true", help="do not ask before writing")
    parser.add_argument(
        "--allow-track-mismatch",
        action="store_true",
        help="with --auto, apply a match even when its track count differs from the local files",
    )
    parser.add_argument("--skip-tagged", action="store_true", help="skip albums whose files all carry the same Discogs release id")
    parser.add_argument("--release-id", type=int, help="use this Discogs release id (single album)")
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="process at most N albums (0 = all); albums skipped by --skip-tagged do not count",
    )
    parser.add_argument(
        "--clear-other-tags",
        action="store_true",
        help="also remove text tags this tool does not write (comments, MusicBrainz ids, ...); "
        "ReplayGain, AccurateRip, encoder info, art, lyrics and ISRC are always kept",
    )
    parser.add_argument(
        "--check-disc-swaps",
        action="store_true",
        help="read-only: report multi-disc albums under PATH whose disc-1/disc-2 tracks look swapped "
        "(file lengths against Discogs) or whose CDn folders disagree with the disc tags (cue/m3u break "
        "the tie). Writes nothing but the --report file",
    )
    parser.add_argument("--report", type=Path, metavar="FILE",
                        help="with --check-disc-swaps: also write the report to FILE (must not exist yet)")
    parser.add_argument("--allow-cache-write", action="store_true",
                        help="with --check-disc-swaps: let Discogs responses go into the cache (default: read only)")
    parser.add_argument("--journal-dir", type=Path, metavar="DIR",
                        help="with --check-disc-swaps: apply journals to read (default: the discogs-tag journal folder)")
    parser.add_argument("--save-token", action="store_true", help="store a Discogs user token and exit")
    parser.add_argument("--self-test", action="store_true", help="run built-in naming tests and exit")
    parser.add_argument(
        "--normalize-folders",
        action="store_true",
        help="rename album folders under PATH to the current naming rule (preview unless --apply)",
    )
    parser.add_argument(
        "--undo-journal",
        type=Path,
        metavar="FILE",
        help="reverse everything recorded in a journal from an apply or --normalize-folders (preview unless --apply)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="really make the changes for --normalize-folders / --undo-journal (default is a preview)",
    )
    parser.add_argument(
        "--trust-legacy-journal",
        action="store_true",
        help="with --undo-journal: also restore tag snapshots from journals written before the JSON format "
        "(they are pickled; only use this on journals discogs-tag itself wrote)",
    )
    return parser.parse_args(argv)


def check_disc_swaps_cli(root: Path, report: Path | None, allow_cache_write: bool,
                         journal_dir: Path | None) -> int:
    """--check-disc-swaps: 0 = nothing flagged, 3 = album(s) flagged (likely swapped, mixed or
    disagree: a disc folder contradicted by the disc tag or cue/m3u), 1 = could not run. Read-only."""
    from ..journal import state_dir
    from ..swapcheck import CacheOnlySource, check_disc_swaps, format_report

    if not root.is_dir():
        print(f"error: not a folder: {root}", file=sys.stderr)
        return 1
    if report is not None and (report.expanduser().exists() or report.expanduser().is_symlink()):
        print(f"error: {report} already exists; pick a new report file", file=sys.stderr)
        return 1
    token, key, secret = load_token()
    if token or (key and secret):
        source = Discogs(token, key, secret)
        source.cache_write = allow_cache_write
        source.on_wait = lambda s: print(f"  (Discogs rate limit: waiting {s:.0f} s)", file=sys.stderr)
    else:
        print("note: no Discogs token: using cached releases only", file=sys.stderr)
        source = CacheOnlySource()
    journals = journal_dir.expanduser() if journal_dir is not None else state_dir() / "journal"
    checks = check_disc_swaps(root.resolve(), source, journals)
    text = format_report(checks, cache_written=allow_cache_write and not isinstance(source, CacheOnlySource))
    print(text, end="")
    if report is not None:
        with open(report.expanduser(), "x", encoding="utf-8") as fh:
            fh.write(text)
        print(f"report written to {report}")
    return 3 if any(c.flagged for c in checks) else 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.self_test:
        return self_test()
    naming.SIDE_NAMES = not args.sequential_sides

    if args.save_token:
        token = prompt("Paste Discogs user token: ")
        if not token:
            print("no token entered", file=sys.stderr)
            return 1
        save_token(token)
        print(f"saved {token_path()}")
        return 0

    if args.undo_journal is not None:
        apply = args.apply and not args.dry_run
        return undo_journal(args.undo_journal.expanduser(), apply=apply, trust_legacy=args.trust_legacy_journal)

    if args.path is None:
        print("error: pass an album folder or tree", file=sys.stderr)
        return 2

    if args.check_disc_swaps:
        return check_disc_swaps_cli(args.path.expanduser(), args.report, args.allow_cache_write, args.journal_dir)

    if args.normalize_folders:
        apply = args.apply and not args.dry_run
        return normalize_folders(args.path.expanduser(), apply=apply)
    if args.apply:
        print("error: --apply only goes with --normalize-folders or --undo-journal", file=sys.stderr)
        return 2

    root = args.path.expanduser()
    try:
        root = root.resolve()
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not root.exists():
        print(f"error: not found: {root}", file=sys.stderr)
        return 1

    dest = args.dest or default_dest(root)
    if dest is None:
        print(
            "error: no destination library. Pass --dest DIR, or set DEFAULT_MUSIC to your library root.",
            file=sys.stderr,
        )
        return 2
    dest = dest.expanduser().resolve()
    albums = collect_albums(root)
    if not albums:
        print(f"No album folders with audio under {root}")
        return 0
    if args.release_id and len(albums) > 1 and args.limit != 1:
        print("error: --release-id only works on a single album folder", file=sys.stderr)
        return 2

    token, key, secret = load_token()
    try:
        api = Discogs(token, key, secret)
    except DiscogsError as exc:
        print(exc, file=sys.stderr)
        return 1

    print(
        f"albums {len(albums)}   dest {dest}"
        + (f"   limit {args.limit}" if args.limit else "")
        + ("   DRY RUN" if args.dry_run else "")
    )
    applied = skipped = failed = 0
    processed = 0
    failures: list[tuple[Path, str]] = []
    try:
        for album in albums:
            if args.limit and processed >= args.limit:
                break
            try:
                result = process_album(
                    api,
                    album,
                    dest,
                    dry_run=args.dry_run,
                    auto=args.auto,
                    yes=args.yes,
                    skip_tagged=args.skip_tagged,
                    release_id=args.release_id,
                    clear_other=args.clear_other_tags,
                    scan_root=root,
                    allow_track_mismatch=args.allow_track_mismatch,
                )
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 - one album must not stop the batch
                processed += 1
                failed += 1
                detail = str(exc) if isinstance(exc, (DiscogsError, AlbumApplyError)) else f"{type(exc).__name__}: {exc}"
                print(f"  error: {album.path}: {detail}", file=sys.stderr)
                failures.append((album.path, detail.splitlines()[0] if detail else type(exc).__name__))
                continue
            if result == "skipped-tagged":
                skipped += 1
                continue
            processed += 1
            if result == "applied":
                applied += 1
            else:
                skipped += 1
    except KeyboardInterrupt:
        print("\nstopped")
    print(f"\ndone  applied={applied}  skipped={skipped}  failed={failed}")
    for path, reason in failures:
        print(f"  failed  {path}: {reason}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
