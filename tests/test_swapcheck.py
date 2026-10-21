"""The read-only disc-swap check (swapcheck, discogs-tag --check-disc-swaps) on synthetic albums.

Each file's length is the Discogs length of the track whose audio it is (disc_fixtures), so a
"swapped" album is one whose tags name the same-number track of the other disc.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from disc_fixtures import OVERLAP_ID, OVERLAP_TRACKS, overlap_release, silent_flac

from music_manager import journal as journal_mod
from music_manager.cli import discogs_tag as cli
from music_manager.fs import collect_albums
from music_manager.journal import Journal
from music_manager.providers import discogs as discogs_mod
from music_manager.providers.discogs import Discogs
from music_manager.swapcheck import (
    CacheOnlySource,
    check_album,
    check_disc_swaps,
    format_report,
    read_journals,
)
from music_manager.tags.snapshot import capture_tags
from music_manager.tags.write import write_tags

ALBUM = ("Band A & Band B", "[1998]  Double Feature 2CD [LT-2CD]")
BY_POS = {(d, n): (t, s) for d, n, t, s in OVERLAP_TRACKS}
SWAP = {(1, 1): (2, 1), (2, 1): (1, 1), (1, 2): (2, 2), (2, 2): (1, 2)}  # what gap 18 did


def meta_for(disc, number, title):
    return {"title": title, "artist": "Band A & Band B", "albumartist": "Band A & Band B",
            "album": "Double Feature", "track": number, "tracktotal": 3 if disc == 1 else 2, "disc": disc,
            "disctotal": 2, "date": "1998", "originaldate": "", "label": "Label Two", "catalognumber": "LT-2CD",
            "genre": "", "release_id": OVERLAP_ID, "master_id": 0, "position": f"{disc}-{number}"}


def tagged_album(root: Path, audio_of=lambda pos: pos, lengths=None, titles_follow_audio=True) -> Path:
    """The flat 2CD folder an apply writes; the file tagged (d, n) holds the audio of audio_of((d, n)).

    Its title tag names that audio (titles_follow_audio, as a rip's own tags do), or the tagged position: a
    swap is only reported when the lengths and the titles both favour the other disc (2026-10-21)."""
    folder = root.joinpath(*ALBUM)
    for d, n, title, _s in OVERLAP_TRACKS:
        seconds = (lengths or {}).get(audio_of((d, n)), BY_POS[audio_of((d, n))][1])
        path = silent_flac(folder / f"{n}. {title}.flac", seconds)
        write_tags(path, meta_for(d, n, BY_POS[audio_of((d, n))][0] if titles_follow_audio else title))
    return folder


class Source:
    def __init__(self, release=None):
        self.rel = release or overlap_release()
        self.calls = []

    def release(self, release_id):
        self.calls.append(release_id)
        return json.loads(json.dumps(self.rel))


def only(root):
    return check_disc_swaps(root, Source())


def test_a_swapped_album_is_flagged_with_per_track_evidence(tmp_path):
    tagged_album(tmp_path, lambda pos: SWAP.get(pos, pos))
    [check] = only(tmp_path)
    assert check.status == "likely swapped" and check.flagged and check.release_id == OVERLAP_ID
    assert [(n.number, n.discs, n.verdict) for n in check.numbers] == [(1, [1, 2], "swap"), (2, [1, 2], "swap")]
    one = check.numbers[0]
    assert one.current_error_s == pytest.approx(2 * (244 - 181)) and one.best_other_error_s == pytest.approx(0)
    t = {e.tagged: e for e in one.tracks}
    assert (t["1-1"].path.name, t["1-1"].length_s, t["1-1"].discogs_s, t["1-1"].better, t["1-1"].better_s) == (
        "1. One.flac", 244.0, 181.0, "2-1", 244.0)
    report = format_report([check])
    assert "LIKELY SWAPPED" in report and "-> the audio is 2-1" in report
    assert "1 flagged (1 likely swapped)" in report


def test_an_album_in_order_is_ok(tmp_path):
    tagged_album(tmp_path)
    [check] = only(tmp_path)
    assert check.status == "ok" and not check.flagged
    assert [n.verdict for n in check.numbers] == ["keep", "keep"]
    assert all(not t.better for n in check.numbers for t in n.tracks)


def test_one_number_swapped_and_one_not_is_mixed(tmp_path):
    tagged_album(tmp_path, lambda pos: {(1, 1): (2, 1), (2, 1): (1, 1)}.get(pos, pos))
    [check] = only(tmp_path)
    assert check.status == "mixed" and check.flagged
    assert [n.verdict for n in check.numbers] == ["swap", "keep"]


def test_lengths_too_close_or_missing_are_inconclusive(tmp_path):
    close = {(1, 1): 200, (2, 1): 202, (1, 2): 300, (2, 2): 303}
    rel = overlap_release()
    for item in rel["tracklist"]:
        d, n = map(int, item["position"].split("-"))
        if (d, n) in close:
            s = close[(d, n)]
            item["duration"] = f"{s // 60}:{s % 60:02d}"
    tagged_album(tmp_path / "a", lambda pos: SWAP.get(pos, pos), lengths=close)
    [check] = check_disc_swaps(tmp_path / "a", Source(rel))
    assert check.status == "inconclusive" and [n.verdict for n in check.numbers] == ["unclear", "unclear"]
    for item in rel["tracklist"]:
        item["duration"] = ""
    tagged_album(tmp_path / "b", lambda pos: SWAP.get(pos, pos))
    [check] = check_disc_swaps(tmp_path / "b", Source(rel))
    assert check.status == "inconclusive" and "cannot tell" in check.reason


def test_single_disc_and_untagged_albums_are_not_judged(tmp_path):
    rel = overlap_release()
    for item in rel["tracklist"]:
        item["position"] = item["position"].split("-")[1]
    tagged_album(tmp_path / "single")
    [check] = check_disc_swaps(tmp_path / "single", Source(rel))
    assert check.status == "single disc"
    silent_flac(tmp_path / "bare" / "x" / "01 a.flac", 100)
    [check] = check_disc_swaps(tmp_path / "bare", Source())
    assert check.status == "skipped" and "no Discogs release id" in check.reason


def test_title_tags_that_disagree_with_the_release_are_noted(tmp_path):
    folder = tagged_album(tmp_path)
    path = folder / "1. One.flac"
    write_tags(path, {**meta_for(1, 1, "Four")})
    [check] = only(tmp_path)
    assert any("title tag 'Four' is not the release's 1-1 'One' (that is the title of 2-1)" in n for n in check.notes)


def test_the_last_apply_journal_is_secondary_evidence(tmp_path):
    """The journal of a re-apply that swapped: the tags before it say the file was 1-1 "One"."""
    folder = tagged_album(tmp_path / "lib")
    journals = tmp_path / "journals"
    j = Journal("apply", journals)
    j.write(action="start", command="apply", album=str(folder), dest=str(folder), release_id=OVERLAP_ID,
            artist="Band A & Band B", title="Double Feature")
    for (d, n), (d2, n2) in SWAP.items():  # re-tag as the bad re-apply did, journaling first
        title = BY_POS[(d, n)][0]
        path = folder / f"{n}. {title}.flac"
        j.write(action="tags", phase="before", path=str(path), snapshot=capture_tags(path))
    for (d, n), (d2, n2) in SWAP.items():
        path = folder / f"{n}. {BY_POS[(d, n)][0]}.flac"
        write_tags(path, meta_for(d2, n2, BY_POS[(d2, n2)][0]))
    j.close()
    # the bad re-apply then renamed the files to their new titles, through stage names
    with open(j.path, "a", encoding="utf-8") as fh:
        for step in ("stage", "final"):
            for (d, n), (d2, n2) in SWAP.items():
                old = folder / f"{n}. {BY_POS[(d, n)][0]}.flac"
                stage = old.with_name(old.name + ".stage")
                new = folder / f"{n2}. {BY_POS[(d2, n2)][0]}.flac"
                src, dest = (old, stage) if step == "stage" else (stage, new)
                src.rename(dest)
                fh.write(json.dumps({"action": "rename", "kind": "file", "old": str(src), "new": str(dest)}) + "\n")
    facts = read_journals(journals)
    assert facts.release_ids == {str(folder): OVERLAP_ID}
    [check] = check_disc_swaps(tmp_path / "lib", Source(), journals)
    assert check.status == "likely swapped"
    four = next(n for n in check.notes if n.startswith("1. Four.flac"))
    assert "moved it from 1-1 'One' to 2-1 'Four'" in four and "what a swap does" in four


def test_the_journal_gives_the_release_when_tags_lack_it(tmp_path):
    folder = tagged_album(tmp_path / "lib", lambda pos: SWAP.get(pos, pos))
    from mutagen.flac import FLAC

    for path in folder.glob("*.flac"):
        audio = FLAC(path)
        del audio["discogs_release_id"]
        audio.save()
    journals = tmp_path / "journals"
    j = Journal("apply", journals)
    j.write(action="start", command="apply", album="/old", dest=str(folder), release_id=OVERLAP_ID)
    j.close()
    [check] = check_disc_swaps(tmp_path / "lib", Source(), journals)
    assert check.status == "likely swapped" and check.reason == "release id from journal"


def test_cache_only_source_reads_fresh_cache_files(tmp_path, monkeypatch):
    monkeypatch.setattr(discogs_mod, "CACHE_DIR", tmp_path / "cache")
    path = tmp_path / "cache" / "release" / f"{hashlib.sha1(str(OVERLAP_ID).encode()).hexdigest()}.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(overlap_release()), encoding="utf-8")
    assert CacheOnlySource().release(OVERLAP_ID)["id"] == OVERLAP_ID
    os.utime(path, (1, 1))
    with pytest.raises(discogs_mod.DiscogsError, match="older than 6 hours"):
        CacheOnlySource().release(OVERLAP_ID)
    with pytest.raises(discogs_mod.DiscogsError, match="not in the Discogs cache"):
        CacheOnlySource().release(1)


# --- the CLI, and that it writes nothing -------------------------------------------------------

def snapshot(*roots):
    out = {}
    for root in roots:
        if not root.exists():
            out[str(root)] = None
            continue
        for p in sorted([root, *root.rglob("*")]):
            st = p.lstat()
            data = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
            out[str(p)] = (p.is_dir(), st.st_size, st.st_mtime_ns, data)
    return out


class FakeSession:
    """requests.Session stand-in for a real Discogs client: serves the release, counts requests."""

    def __init__(self):
        self.urls = []
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        self.urls.append(url)

        class R:
            status_code = 200
            text = ""

            def __init__(self):
                self.headers = {}

            @staticmethod
            def json():
                return overlap_release()

        return R()


@pytest.fixture
def clean_env(tmp_path, monkeypatch):
    """An empty HOME and empty XDG folders, and the library's default paths (None: under HOME)."""
    home = tmp_path / "home"
    home.mkdir()
    xdg = {name: tmp_path / name.lower() for name in ("XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME",
                                                       "XDG_CACHE_HOME")}
    monkeypatch.setenv("HOME", str(home))
    for name, path in xdg.items():
        path.mkdir()
        monkeypatch.setenv(name, str(path))
    monkeypatch.delenv("DISCOGS_TOKEN", raising=False)
    monkeypatch.setattr(journal_mod, "STATE_DIR", None)
    monkeypatch.setattr(discogs_mod, "CACHE_DIR", None)
    monkeypatch.setattr(discogs_mod, "TOKEN_PATH", None)
    sessions = []

    def client(token, key, secret):
        real = Discogs(token, key, secret)
        real.session = FakeSession()
        real.min_interval = 0
        sessions.append(real.session)
        return real

    monkeypatch.setattr(cli, "Discogs", client)
    monkeypatch.setattr(cli, "load_token", lambda: ("token", None, None))
    return home, list(xdg.values()), sessions


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main([str(a) for a in argv])
    return code, out.getvalue(), err.getvalue()


def test_cli_check_writes_nothing_at_all(tmp_path, clean_env):
    home, xdg, sessions = clean_env
    lib = tmp_path / "lib"
    tagged_album(lib / "swapped", lambda pos: SWAP.get(pos, pos))
    tagged_album(lib / "fine")
    journals = tmp_path / "journals"
    j = Journal("apply", journals)
    j.write(action="start", command="apply", album="/x", dest="/y", release_id=OVERLAP_ID)
    j.close()
    before = snapshot(lib, home, journals, *xdg)
    code, out, err = run_cli("--check-disc-swaps", lib, "--journal-dir", journals)
    assert code == 3, out + err
    assert out.count("LIKELY SWAPPED") == 1 and out.count("OK ") == 1
    assert "the Discogs cache was not written" in out
    assert sessions and sessions[0].urls == [f"{discogs_mod.API}/releases/{OVERLAP_ID}"]  # one fetch, two albums
    assert snapshot(lib, home, journals, *xdg) == before
    assert not any(home.iterdir()) and not any(any(p.iterdir()) for p in xdg)


def test_cli_check_default_journal_folder_is_only_read(tmp_path, clean_env):
    home, xdg, _sessions = clean_env
    lib = tagged_album(tmp_path / "lib").parent.parent
    before = snapshot(lib, home, *xdg)
    code, out, _err = run_cli("--check-disc-swaps", lib)
    assert code == 0 and "OK " in out and "0 flagged" in out
    assert snapshot(lib, home, *xdg) == before and not any(home.iterdir())


def test_cli_check_report_is_the_only_file_written(tmp_path, clean_env):
    home, xdg, _sessions = clean_env
    lib = tmp_path / "lib"
    tagged_album(lib, lambda pos: SWAP.get(pos, pos))
    report = tmp_path / "out" / "swaps.txt"
    report.parent.mkdir()
    before = snapshot(lib, home, *xdg)
    code, out, _err = run_cli("--check-disc-swaps", lib, "--report", report)
    assert code == 3 and report.read_text(encoding="utf-8") in out
    assert "LIKELY SWAPPED" in report.read_text(encoding="utf-8")
    assert snapshot(lib, home, *xdg) == before and sorted(report.parent.iterdir()) == [report]
    code, _out, err = run_cli("--check-disc-swaps", lib, "--report", report)  # never overwrites
    assert code == 1 and "already exists" in err


def test_cli_check_caches_only_when_allowed(tmp_path, clean_env):
    home, _xdg, _sessions = clean_env
    lib = tagged_album(tmp_path / "lib").parent.parent
    code, out, _err = run_cli("--check-disc-swaps", lib, "--allow-cache-write")
    assert code == 0 and "may have been cached" in out
    assert list((home / ".cache" / "discogs-tag" / "release").glob("*.json"))


def test_cli_check_without_a_token_uses_the_cache_only(tmp_path, clean_env, monkeypatch):
    home, xdg, sessions = clean_env
    monkeypatch.setattr(cli, "load_token", lambda: (None, None, None))
    lib = tagged_album(tmp_path / "lib", lambda pos: SWAP.get(pos, pos)).parent.parent
    before = snapshot(lib, home, *xdg)
    code, out, err = run_cli("--check-disc-swaps", lib)
    assert code == 0 and "using cached releases only" in err and "ERROR" in out and not sessions
    assert snapshot(lib, home, *xdg) == before


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg to make real silent audio")
def test_real_silent_audio_of_different_lengths(tmp_path):
    """Real FLAC audio (not just a STREAMINFO count): 5 s apart is enough to tell."""
    lengths = {(1, 1): 6, (1, 2): 13, (1, 3): 20, (2, 1): 27, (2, 2): 34}
    rel = overlap_release()
    for item in rel["tracklist"]:
        d, n = map(int, item["position"].split("-"))
        item["duration"] = f"0:{lengths[(d, n)]:02d}"
    folder = tmp_path.joinpath(*ALBUM)
    folder.mkdir(parents=True)
    for d, n, title, _s in OVERLAP_TRACKS:
        seconds = lengths[SWAP.get((d, n), (d, n))]
        path = folder / f"{n}. {title}.flac"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                        "anullsrc=r=8000:cl=mono", "-t", str(seconds), "-c:a", "flac", str(path)], check=True)
        write_tags(path, meta_for(d, n, BY_POS[SWAP.get((d, n), (d, n))][0]))  # the title of its audio
    album = next(a for a in collect_albums(tmp_path) if a.path == folder)
    check = check_album(album, Source(rel), read_journals(None))
    assert check.status == "likely swapped"
