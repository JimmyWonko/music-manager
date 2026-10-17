"""Shared fixtures. No test touches the network or a real library."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from music_manager import journal as journal_mod
from music_manager.providers import discogs as discogs_mod

REPO = Path(__file__).resolve().parent.parent
SAMPLES = Path(__file__).resolve().parent / "samples"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Journals and the trash live in the test's temp folder, never in ~."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setattr(journal_mod, "STATE_DIR", tmp_path / "xdg-state" / "discogs-tag")
    monkeypatch.setattr(discogs_mod, "CACHE_DIR", tmp_path / "xdg-cache" / "discogs-tag")


def release(name: str) -> dict:
    return json.loads((FIXTURES / f"release_{name}.json").read_text(encoding="utf-8"))


@pytest.fixture
def sample(tmp_path):
    """Copy a silent sample (tests/samples/silent.EXT) to tmp_path/NAME."""

    def make(ext: str, name: str | None = None, folder: Path | None = None) -> Path:
        src = SAMPLES / f"silent.{ext}"
        dest = (folder or tmp_path) / (name or f"silent.{ext}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        return dest

    return make


@pytest.fixture
def meta():
    return {
        "title": "Söng",
        "artist": "Ärtist",
        "albumartist": "Ärtist",
        "album": "Albüm",
        "track": 3,
        "tracktotal": 10,
        "disc": 1,
        "disctotal": 2,
        "date": "1999-04-00",
        "originaldate": "1981",
        "label": "Label",
        "catalognumber": "CAT1",
        "genre": "Rock",
        "release_id": 123,
        "master_id": 45,
        "position": "B1",
    }
