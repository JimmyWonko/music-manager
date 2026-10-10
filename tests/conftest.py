"""Shared fixtures. No test touches the network or a real library."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SAMPLES = Path(__file__).resolve().parent / "samples"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def dt():
    """discogs_tag.py as a module. discogs-tag.py is the checkout launcher."""
    return _load("discogs_tag", REPO / "discogs_tag.py")


@pytest.fixture(scope="session")
def m3u():
    """album_m3u.py as a module. album-m3u is the checkout launcher."""
    return _load("album_m3u", REPO / "album_m3u.py")


@pytest.fixture(scope="session")
def common():
    sys.path.insert(0, str(REPO))
    import mm_common

    return mm_common


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
