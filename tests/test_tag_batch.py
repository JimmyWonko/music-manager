"""Tag batch (2026-10-21): ID3v2.3 for WAV/AIFF (opt-in for MP3), UTF-16 vs opt-in Latin-1 with a
structured list of lost characters, disc-swap verdicts that need lengths AND titles, junk-file
cleanup through the trash, and the cross-drive verify hash (SHA-256 default, xxHash optional).
Synthetic files and fixtures only."""

from __future__ import annotations

import dataclasses
import importlib.util

import pytest


def meta(**extra):
    out = {"title": "Söng 日本", "artist": "Ärtist", "albumartist": "Ärtist", "album": "Albüm", "track": 1,
           "tracktotal": 2, "disc": 1, "disctotal": 1, "date": "1999", "release_id": 1, "master_id": 0}
    out.update(extra)
    return out


def id3_of(path):
    from mutagen import File

    return File(path).tags


# --- 1. ID3 version ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("ext", "extra", "version"), [
    ("wav", {}, 3), ("aiff", {}, 3), ("mp3", {}, 4),
    ("mp3", {"id3_v23": True}, 3)])
def test_id3_version_per_container(sample, ext, extra, version):
    from mutagen.id3 import ID3

    from music_manager.tags.write import write_tags

    f = sample(ext)
    write_tags(f, meta(**extra))
    tags = ID3(f) if ext == "mp3" else id3_of(f)
    assert tags.version[:2] == (2, version)
    assert str(tags["TIT2"]) == "Söng 日本"


# --- 2. v2.3 encodings ---------------------------------------------------------------------------------------------

def test_v23_defaults_to_utf16_and_latin1_is_opt_in(sample):
    from mutagen.id3 import Encoding

    from music_manager.tags.write import write_tags

    f = sample("wav")
    write_tags(f, meta())
    frame = id3_of(f)["TIT2"]
    assert frame.encoding == Encoding.UTF16 and str(frame) == "Söng 日本"
    write_tags(f, meta(id3_latin1=True, title="Söng"))
    frame = id3_of(f)["TIT2"]
    assert frame.encoding == Encoding.LATIN1 and str(frame) == "Söng"
    assert str(id3_of(f)["TPE1"]) == "Ärtist"  # Latin-1 characters survive
    write_tags(f, meta(id3_latin1=True, keep_fields=["title"]))  # a dropped field: not written, kept
    assert str(id3_of(f)["TIT2"]) == "Söng"


def test_latin1_losses_names_fields_and_characters():
    from music_manager.tags.id3 import latin1_losses

    assert latin1_losses({"title": "Söng 日本", "artist": "Ärtist", "genre": ["Rock", "J-ポップ"]}) == [
        ("title", "Söng 日本", "日本", "dropped", None), ("genre", "J-ポップ", "ポップ", "dropped", None)]


@pytest.mark.parametrize("name", ["Björk", "Motörhead", "Sigur Rós", "Ærøskøbing"])
def test_latin1_safe_values_are_untouched(name):
    from music_manager.tags.id3 import latin1_value

    assert latin1_value(name) == (None, name)


@pytest.mark.parametrize(("value", "proposed"), [("Łódź", "Lódz"), ("Dvořák “live”", 'Dvorák "live"'),
                                               ("Œuvre — 2", "OEuvre -- 2")])
def test_only_unencodable_latin_characters_are_transliterated(value, proposed):
    pytest.importorskip("unidecode")
    from music_manager.tags.id3 import latin1_value

    assert latin1_value(value) == ("transliterated", proposed)


@pytest.mark.parametrize("value", ["日本", "Кино", "Söng 日本"])
def test_characters_without_a_sensible_substitute_drop_the_field(value):
    from music_manager.tags.id3 import latin1_value

    assert latin1_value(value) == ("dropped", None)


def test_without_unidecode_every_unencodable_character_drops(monkeypatch):
    import builtins

    from music_manager.tags.id3 import latin1_value

    real = builtins.__import__

    def block(name, *a, **k):
        if name == "unidecode":
            raise ModuleNotFoundError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", block)
    assert latin1_value("Łódź") == ("dropped", None) and latin1_value("Björk") == (None, "Björk")


# --- 4. disc swaps need lengths and titles -------------------------------------------------------------------------

def test_jaro_winkler():
    from music_manager.similarity import jaro_winkler

    assert jaro_winkler("MARTHA", "MARHTA") == pytest.approx(0.9611, abs=1e-4)
    assert jaro_winkler("DIXON", "DICKSONX") == pytest.approx(0.8133, abs=1e-4)
    assert jaro_winkler("abc", "abc") == 1.0 and jaro_winkler("", "abc") == 0.0


def test_lengths_alone_are_unsure(tmp_path):
    from test_swapcheck import SWAP, only, tagged_album

    tagged_album(tmp_path, lambda pos: SWAP.get(pos, pos), titles_follow_audio=False)  # titles as the release
    [check] = only(tmp_path)
    assert check.status == "unsure" and not check.flagged
    assert [n.verdict for n in check.numbers] == ["unsure", "unsure"]
    from music_manager.swapcheck import format_report

    assert "UNSURE" in format_report([check])


# --- 5. junk files -------------------------------------------------------------------------------------------------

JUNK = (".DS_Store", "Thumbs.db", "desktop.ini", "._01 One.flac")


def test_junk_files_are_an_exact_allowlist(tmp_path):
    from music_manager.fs import junk_files

    for name in (*JUNK, "notes.txt", "thumbs.db.bak", "DS_Store"):
        (tmp_path / name).write_bytes(b"x")
    assert sorted(p.name for p in junk_files(tmp_path)) == sorted(JUNK)


def test_prune_trashes_junk_only_when_nothing_else_is_left(tmp_path):
    from music_manager.fs import trash_root
    from music_manager.moves import _prune_empty

    root = tmp_path / "lib"
    a, b = root / "A", root / "B"
    for d in (a, b):
        d.mkdir(parents=True)
        for name in JUNK:
            (d / name).write_bytes(b"junk")
    (b / "notes.txt").write_text("keep")
    assert _prune_empty(a, root) == [a] and not a.exists()
    trashed = sorted(p.name for p in (trash_root() / "files").iterdir())
    assert trashed == sorted(JUNK)
    assert _prune_empty(b, root) == [] and sorted(p.name for p in b.iterdir()) == sorted([*JUNK, "notes.txt"])


# --- 6. verify hash ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("algo", ["sha256", "xxhash"])
def test_copy_reads_the_source_once_and_reads_back_the_copy(tmp_path, monkeypatch, algo):
    import builtins

    from music_manager import moves

    if algo == "xxhash":
        pytest.importorskip("xxhash")
    src, dest = tmp_path / "a.bin", tmp_path / "d" / "a.bin"
    dest.parent.mkdir()
    src.write_bytes(b"x" * 100_000)
    opened = []
    real_open = builtins.open
    monkeypatch.setattr(builtins, "open", lambda p, *a, **k: (opened.append(str(p)), real_open(p, *a, **k))[1])
    moves._copy_verified(src, dest, algo=algo)
    assert opened.count(str(src)) == 1 and dest.read_bytes() == src.read_bytes()
    assert len(opened) == 2  # the source once, the partial copy once (read-back)


def test_unknown_hash_is_refused():
    from music_manager import moves

    with pytest.raises(ValueError):
        moves.hasher("md5")


# --- the service ---------------------------------------------------------------------------------------------------

def test_settings_are_appended_with_defaults(tmp_path):
    from service_fixtures import Library

    from music_manager import service

    Library(tmp_path).start(tmp_path)
    try:
        names = [f.name for f in dataclasses.fields(service.Settings)]
        assert names[-3:] == ["id3v23_mp3", "id3_latin1", "verify_hash"]
        s = service.settings.get()
        assert (s.id3v23_mp3, s.id3_latin1, s.verify_hash) == (False, False, "sha256")
        with pytest.raises(service.ValidationError):
            service.settings.set(id3_latin1="yes")
        with pytest.raises(service.ValidationError):
            service.settings.set(verify_hash="md5")
        names = [f.name for f in dataclasses.fields(service.AlbumPlan)]
        assert names[names.index("junk"):][:4] == ["junk", "encoding_losses", "junk_moving", "id3v23_files"]
        assert [f.name for f in dataclasses.fields(service.EncodingLoss)] == [
            "path", "field", "value", "lost", "severity", "proposed_value"]
        names = [f.name for f in dataclasses.fields(service.AlbumPlan)]
        assert names[-2:] == ["encoding_transliterated", "encoding_dropped"]
    finally:
        service.reset()


def test_plan_lists_latin1_losses_and_junk(tmp_path):
    from service_fixtures import Library, make_album
    pytest.importorskip("unidecode")   # the transliterated catno; the no-unidecode drop path is covered separately

    from music_manager import service

    lib = Library(tmp_path)
    for f in lib.double.glob("*.flac"):
        f.unlink()
    make_album(lib.double, ["1-01 One", "1-02 Two", "2-01 Three", "2-02 Four"], ext="wav")
    (lib.double.parent / ".DS_Store").write_bytes(b"junk")  # the new folder is in this artist folder too
    lib.multi["tracklist"][0]["title"] = "One 日本"  # dropped: the album is held
    lib.multi["tracklist"][1]["title"] = "Łódź"  # transliterated
    lib.multi["labels"] = [{"name": "Björk Records", "catno": "Ł-1"}]  # not a core field
    lib.start(tmp_path)
    try:
        aid = lib.album_id(lib.double)
        lib.find(aid)
        service.match.choose(aid, lib.multi["id"])
        [ap] = service.plan.build([aid]).albums
        assert ap.encoding_losses == [] and ap.junk == []  # UTF-16: nothing lost
        service.settings.set(id3_latin1=True)
        plan = service.plan.build([aid])
        [ap] = plan.albums
        got = {(loss.path.name, loss.field, loss.value, loss.severity, loss.proposed_value)
               for loss in ap.encoding_losses}
        want = {("1-01 One.wav", "title", "One 日本", "dropped", None)}
        if importlib.util.find_spec("unidecode") is not None:
            want |= {("1-02 Two.wav", "title", "Łódź", "transliterated", "Lódz")}
            want |= {(n, "catalognumber", "Ł-1", "transliterated", "L-1") for n in
                     ("1-01 One.wav", "1-02 Two.wav", "2-01 Three.wav", "2-02 Four.wav")}
        assert got == want
        assert (ap.encoding_dropped, ap.encoding_transliterated) == (1, len(want) - 1)
        assert [p.kind for p in ap.problems] == [service.ProblemKind.ENCODING_DROPPED]
        assert ap.encoding_losses  # a held album still lists its losses and totals
        n = len(want) - 1
        line = (f"{lib.double.name}: Latin-1 (Settings.id3_latin1): {n} tag value(s) transliterated, 1 dropped and "
                "not written (existing value kept)")
        assert f"{line} (album held: allow_encoding_drop to apply): see AlbumPlan.encoding_losses" in plan.warnings
        assert not any('"?"' in w for w in plan.warnings)
        plan = service.plan.build([aid], allow_encoding_drop={aid})  # the manual option
        [ap] = plan.albums
        assert ap.problems == [] and ap.encoding_dropped == 1
        assert f"{line}: see AlbumPlan.encoding_losses" in plan.warnings
        one = next(c for c in ap.files if c.path_before.name == "1-01 One.wav")
        assert "title" not in {t.key for t in one.tags}  # the dropped field is not written
    finally:
        service.reset()


def test_junk_left_behind_stops_at_a_folder_with_other_files(tmp_path):
    from music_manager.service.plan import _junk_left_behind

    root = tmp_path / "lib"
    album = root / "Artist" / "Album"
    album.mkdir(parents=True)
    for d in (album, album.parent):
        (d / ".DS_Store").write_bytes(b"j")
    (album / "Thumbs.db").write_bytes(b"j")
    target = root / "New" / "Album"
    assert _junk_left_behind(album, root, target, set()) == [
        album / ".DS_Store", album / "Thumbs.db", album.parent / ".DS_Store"]
    (album.parent / "notes.txt").write_text("x")
    assert _junk_left_behind(album, root, target, {album / "Thumbs.db"}) == [album / ".DS_Store"]
    assert _junk_left_behind(album, root, root / "Artist" / "Other", set())[-1] == album / "Thumbs.db"


def test_misfiled_folders_with_correct_tags_are_disagree_even_without_lengths(tmp_path):
    """John Prine style: every tag right, files in the wrong CD folder. DISAGREE comes from the folder,
    tag and cue/m3u contradicting each other, not from lengths: no Discogs durations, still flagged."""
    from prine_fixtures import FOLDER, PRINE_ID, hand_sorted, release

    from music_manager.swapcheck import check_disc_swaps

    rel = release()
    for item in rel["tracklist"]:
        item["duration"] = ""

    class Source:
        def release(self, release_id):
            assert release_id == PRINE_ID
            return rel

    hand_sorted(tmp_path.joinpath(*FOLDER))
    [check] = check_disc_swaps(tmp_path, Source())
    assert check.status == "disagree" and check.flagged and len(check.disagreements) == 10


def test_plan_lists_junk_that_moves_with_the_album(tmp_path):
    import dataclasses

    from service_fixtures import Library

    from music_manager import service

    lib = Library(tmp_path)
    (lib.double / ".DS_Store").write_bytes(b"junk")
    (lib.double / "scans" / "Thumbs.db").write_bytes(b"junk")
    (lib.double / "notes.txt").write_text("not junk")
    lib.start(tmp_path)
    try:
        assert "junk_moving" in [f.name for f in dataclasses.fields(service.AlbumPlan)]
        aid = lib.album_id(lib.double)
        lib.find(aid)
        service.match.choose(aid, lib.multi["id"])
        [ap] = service.plan.build([aid]).albums
        assert ap.junk_moving == [lib.double / ".DS_Store", lib.double / "scans" / "Thumbs.db"]
        assert lib.double / ".DS_Store" in {c.path_before for c in ap.files}  # it still moves with the album
        assert ap.junk == []
    finally:
        service.reset()



def test_xxhash_setting_when_installed(tmp_path):
    pytest.importorskip("xxhash")
    from service_fixtures import Library

    from music_manager import service

    Library(tmp_path).start(tmp_path)
    try:
        assert service.settings.set(verify_hash="xxhash").verify_hash == "xxhash"
    finally:
        service.reset()


def test_verify_hashes_available(tmp_path, monkeypatch):
    import builtins

    from service_fixtures import Library

    from music_manager import service

    Library(tmp_path).start(tmp_path)
    try:
        try:
            import xxhash  # noqa: F401
            have = ["sha256", "xxhash"]
        except ImportError:
            have = ["sha256"]
        assert service.settings.verify_hashes_available() == have
        real = builtins.__import__

        def block(name, *a, **k):
            if name == "xxhash":
                raise ImportError("blocked")
            return real(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", block)
        assert service.settings.verify_hashes_available() == ["sha256"]
        with pytest.raises(service.ValidationError):
            service.settings.set(verify_hash="xxhash")
    finally:
        service.reset()


def test_plan_counts_id3v23_files(tmp_path):
    from service_fixtures import Library, make_album

    from music_manager import service

    lib = Library(tmp_path)
    flacs = sorted(lib.double.glob("*.flac"))
    for f in flacs[:2]:
        f.unlink()
    make_album(lib.double, ["1-01 One"], ext="wav")
    make_album(lib.double, ["1-02 Two"], ext="mp3")
    lib.start(tmp_path)
    try:
        aid = lib.album_id(lib.double)
        lib.find(aid)
        service.match.choose(aid, lib.multi["id"])
        [ap] = service.plan.build([aid]).albums
        assert ap.id3v23_files == 1  # the WAV
        service.settings.set(id3v23_mp3=True)
        [ap] = service.plan.build([aid]).albums
        assert ap.id3v23_files == 2  # and the MP3
        import dataclasses

        assert "id3v23_files" in [f.name for f in dataclasses.fields(service.AlbumPlan)]
    finally:
        service.reset()

