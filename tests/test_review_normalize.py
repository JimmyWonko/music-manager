"""Review item 4: --normalize-folders journals each rename before doing it, so a crash
between the rename and the journal line still leaves an undo that puts it back."""

import json
import os

import pytest
from test_apply import make_album
from test_normalize import generated_playlist, snapshot

from music_manager import journal as journal_mod
from music_manager import normalize
from music_manager.cli.discogs_tag import main

REVIEW4 = pytest.mark.xfail(strict=True, reason="review item 4: the journal line is written after os.rename")


class Crash(BaseException):
    """Stands in for a power cut or kill right after a rename."""


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setattr(journal_mod, "STATE_DIR", tmp_path / "state")
    lib = tmp_path / "Music"
    mywar = make_album(lib / "Black Flag" / "[1984]  My War [1984] [SST023]", ["1. My War.flac", "2. Scream.flac"])
    generated_playlist(mywar)
    make_album(lib / "Black Flag" / "[2013]  What The… [2013]", ["1. Hollywood.flac"])
    return lib


def crash_after_rename(monkeypatch, n):
    """Let the nth os.rename in normalize happen, then die before anything else runs."""
    real, calls = os.rename, []

    def rename(a, b, *args, **kwargs):
        real(a, b, *args, **kwargs)
        calls.append((a, b))
        if len(calls) == n:
            raise Crash

    monkeypatch.setattr(normalize.os, "rename", rename)
    return calls


@REVIEW4
@pytest.mark.parametrize("n", [1, 2, 3], ids=["first-folder", "its-playlist", "second-folder"])
def test_a_crash_right_after_a_rename_can_still_be_undone(library, tmp_path, monkeypatch, n):
    before = snapshot(library)
    calls = crash_after_rename(monkeypatch, n)
    with pytest.raises(Crash):
        main(["--normalize-folders", "--apply", str(library)])
    monkeypatch.undo()
    monkeypatch.setattr(journal_mod, "STATE_DIR", tmp_path / "state")
    assert len(calls) == n and snapshot(library) != before

    journal = next((tmp_path / "state" / "journal").glob("normalize-folders-*.jsonl"))
    entries = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    renamed = [(e["old"], e["new"]) for e in entries if e["action"] == "rename"]
    assert [(str(a), str(b)) for a, b in calls] == renamed[:n]  # every rename that happened is on record
    assert main(["--undo-journal", str(journal), "--apply"]) == 0
    assert snapshot(library) == before


@REVIEW4
def test_rename_entries_are_written_before_the_rename(library, tmp_path, monkeypatch):
    seen = []
    real = os.rename

    def rename(a, b, *args, **kwargs):
        journal = next((tmp_path / "state" / "journal").glob("normalize-folders-*.jsonl"))
        lines = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        seen.append(lines[-1]["action"] == "rename" and lines[-1]["old"] == str(a) and lines[-1]["new"] == str(b))
        return real(a, b, *args, **kwargs)

    monkeypatch.setattr(normalize.os, "rename", rename)
    assert main(["--normalize-folders", "--apply", str(library)]) == 0
    assert seen == [True, True, True]

