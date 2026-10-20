"""A two-disc release whose discs share track numbers, and synthetic files of known lengths.

Each file's length (the FLAC STREAMINFO sample count of the 0.05 s silent sample) is the
length of the Discogs track whose audio it stands for, so a file's identity survives any
re-tagging or renaming: tests read the length back to see which track it really is.
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

from mutagen.flac import FLAC

SAMPLES = Path(__file__).resolve().parent / "samples"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

OVERLAP_ID = 5150
# disc, number, title, seconds: disc 1 has tracks 1-3, disc 2 tracks 1-2 (numbers 1 and 2 on both)
OVERLAP_TRACKS = [(1, 1, "One", 181), (1, 2, "Two", 202), (1, 3, "Three", 213), (2, 1, "Four", 244),
                  (2, 2, "Five", 265)]
TRUTH = {seconds: (disc, number, title) for disc, number, title, seconds in OVERLAP_TRACKS}

LAYOUTS = {
    "flat-disc-prefix": ["1-01 One", "1-02 Two", "1-03 Three", "2-01 Four", "2-02 Five"],
    "cd-subfolders": ["CD1/01 One", "CD1/02 Two", "CD1/03 Three", "CD2/01 Four", "CD2/02 Five"],
    "flat-restarting": ["01 One", "02 Two", "03 Three", "01 Four", "02 Five"],
}
# Restarting numbers and titles that say nothing: the disc cannot be told apart.
NO_TITLES = ["01 a", "02 b", "03 c", "01 d", "02 e"]


def overlap_release() -> dict:
    rel = json.loads((FIXTURES / "release_multi_cd.json").read_text(encoding="utf-8"))
    rel = copy.deepcopy(rel)
    rel["id"] = OVERLAP_ID
    rel["master_id"] = 0
    rel["tracklist"] = [{"position": f"{d}-{n}", "type_": "track", "title": t, "duration": f"{s // 60}:{s % 60:02d}"}
                        for d, n, t, s in OVERLAP_TRACKS]
    return rel


def silent_flac(dest: Path, seconds: float) -> Path:
    """A silent FLAC that reports `seconds` as its length."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SAMPLES / "silent.flac", dest)
    audio = FLAC(dest)
    audio.info.total_samples = round(seconds * audio.info.sample_rate)
    audio.save()
    return dest


def make_overlap_album(folder: Path, names: list[str]) -> Path:
    """names in OVERLAP_TRACKS order; each file gets that track's length."""
    for name, (_d, _n, _t, seconds) in zip(names, OVERLAP_TRACKS, strict=True):
        silent_flac(folder / f"{name}.flac", seconds)
    return folder


def identity(path: Path) -> tuple[int, int, str]:
    """(disc, number, title) of the track this file's audio is."""
    return TRUTH[round(FLAC(path).info.length)]


def tagged(path: Path) -> tuple[int, int, str]:
    tags = FLAC(path)
    disc = int(tags["discnumber"][0].split("/")[0])
    number = int(tags["tracknumber"][0].split("/")[0])
    return disc, number, tags["title"][0]


def swapped_files(root: Path) -> list[str]:
    """Files whose tags name another track than their audio is."""
    return sorted(str(p.relative_to(root)) for p in root.rglob("*.flac") if tagged(p) != identity(p))


class FakeCliDiscogs:
    """Stands in for providers.discogs.Discogs in the CLI: one release, no network."""

    def __init__(self, *_args, **_kwargs):
        self.on_wait = None

    def search_releases(self, *_args, **_kwargs):
        return [{"id": OVERLAP_ID, "title": "Band A & Band B - Double Feature", "year": "1998",
                 "catno": "LT-2CD", "label": ["Label Two"], "format": ["CD"], "master_id": 0}]

    def release(self, release_id):
        return overlap_release()

    def master(self, master_id):
        return {}

    def image(self, url):
        from music_manager.providers.discogs import DiscogsError

        raise DiscogsError("no images in tests")
