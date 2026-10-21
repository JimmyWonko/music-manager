"""A temp library, a fake Discogs and a configured service for the service tests. No network."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

from music_manager import service
from music_manager.service import _events as service_events
from music_manager.tags.write import write_tags

SAMPLES = Path(__file__).resolve().parent / "samples"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture_release(name: str) -> dict:
    return json.loads((FIXTURES / f"release_{name}.json").read_text(encoding="utf-8"))


def with_durations(release: dict, seconds: list[int]) -> dict:
    release = copy.deepcopy(release)
    playable = [t for t in release["tracklist"] if (t.get("type_") or "track") == "track"]
    for item, s in zip(playable, seconds, strict=False):
        item["duration"] = f"{s // 60}:{s % 60:02d}"
    return release


class FakeDiscogs:
    """Stands in for providers.discogs.Discogs: canned searches, releases, masters and images."""

    def __init__(self):
        self.searches: dict[str, list[dict]] = {}
        self.barcode_searches: dict[str, list[dict]] = {}  # digits -> results (search_barcode)
        self.text_searches: dict[str, list[dict]] = {}  # casefolded q -> results (search_text)
        self.catno_searches: dict[str, list[dict]] = {}  # catno -> results (search_releases with only catno)
        self.releases: dict[int, dict] = {}
        self.masters: dict[int, dict] = {}
        self.images: dict[str, bytes] = {}
        self.calls: list[tuple] = []
        self.on_wait = None
        self.fail_release: set[int] = set()

    def search_releases(self, artist, album, catno="", year=""):
        self.calls.append(("search", artist, album, catno, year))
        if not artist and not album and catno:
            return copy.deepcopy(self.catno_searches.get(catno, []))
        return copy.deepcopy(self.searches.get((album or "").casefold(), []))

    def search_barcode(self, barcode):
        self.calls.append(("barcode", barcode))
        return copy.deepcopy(self.barcode_searches.get(barcode, []))

    def search_text(self, query, year=""):
        self.calls.append(("text", query))
        return copy.deepcopy(self.text_searches.get(query.casefold(), []))

    def release(self, release_id):
        self.calls.append(("release", release_id))
        if release_id in self.fail_release or release_id not in self.releases:
            from music_manager.providers.discogs import DiscogsError

            raise DiscogsError(f"Discogs HTTP 404 for release {release_id}")
        return copy.deepcopy(self.releases[release_id])

    def master(self, master_id):
        self.calls.append(("master", master_id))
        return copy.deepcopy(self.masters.get(master_id, {}))

    def image(self, url):
        self.calls.append(("image", url))
        return self.images[url]


def search_result(release: dict, **extra) -> dict:
    artists = " & ".join(a["name"] for a in release.get("artists", []))
    label = release.get("labels", [{}])[0]
    out = {
        "id": release["id"], "title": f"{artists} - {release['title']}", "year": str(release.get("year") or ""),
        "catno": label.get("catno", ""), "label": [label.get("name", "")],
        "format": [f["name"] for f in release.get("formats", [])], "country": "UK",
        "master_id": release.get("master_id") or 0, "thumb": f"https://img.example/{release['id']}-150.jpg",
        "cover_image": f"https://img.example/{release['id']}.jpg",
    }
    out.update(extra)
    return out


def make_album(folder: Path, names: list[str], ext: str = "flac", extras: tuple[str, ...] = ()) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        dest = folder / f"{name}.{ext}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SAMPLES / f"silent.{ext}", dest)
    for extra in extras:
        path = folder / extra
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"extra " + extra.encode())
    return folder


def tree(root: Path) -> dict:
    """Every path under root with its size and mtime: a read-only check compares two of these."""
    out = {}
    for p in sorted(root.rglob("*")):
        st = p.lstat()
        out[str(p)] = (p.is_dir(), st.st_size, st.st_mtime_ns)
    return out


class Library:
    """Builds the test library and its Discogs data."""

    def __init__(self, tmp: Path):
        self.root = tmp / "lib"
        self.root.mkdir()
        self.discogs = FakeDiscogs()
        multi = with_durations(fixture_release("multi_cd"), [181, 202, 213, 244])
        multi["images"] = [{"type": "primary", "uri": "https://img.example/1000004.jpg",
                            "uri150": "https://img.example/1000004-150.jpg"}]
        self.multi = multi
        self.vinyl = fixture_release("vinyl_lp")
        self.cassette = fixture_release("cassette")
        other = copy.deepcopy(self.multi)
        other.update(id=7777, title="Double Feature (Live)", master_id=0)
        other["labels"] = [{"name": "Bootleg", "catno": "BOOT 1"}]
        self.other = other
        for rel in (multi, self.vinyl, self.cassette, other):
            self.discogs.releases[rel["id"]] = rel
        self.discogs.masters[2000004] = {"year": 1998}
        self.discogs.masters[2000001] = {"year": 1985}
        self.discogs.images["https://img.example/1000004.jpg"] = b"\xff\xd8\xff fake jpeg"
        self.discogs.searches["double feature"] = [search_result(multi), search_result(other, year="2005")]
        self.discogs.searches["side story"] = [search_result(self.vinyl)]
        self.discogs.searches["tape loops"] = []

        # A confident match: folder name gives artist, title, year and catno.
        self.double = make_album(self.root / "Band A & Band B" / "[1998]  Double Feature [LT-2CD]",
                                 ["1-01 One", "1-02 Two", "2-01 Three", "2-02 Four"],
                                 extras=("cover.jpg", "scans/back.png", "rip.log"))
        # Needs review: artist and title only.
        self.side = make_album(self.root / "incoming" / "The Testers - Side Story",
                               [f"{n:02d} {t}" for n, t in enumerate(
                                   ["Opening", "Second", "Third", "Fourth", "Fifth", "Flip", "Seventh", "Eighth",
                                    "Ninth", "Closing"], 1)], ext="mp3")
        # No Discogs results.
        self.tape = make_album(self.root / "incoming" / "Kino - Tape Loops", ["01 One", "02 Two"], ext="ogg")
        # Already tagged by an earlier apply: APPLIED on scan.
        self.done = make_album(self.root / "Done" / "Already Tagged", ["01 A", "02 B"])
        for i, f in enumerate(sorted(self.done.glob("*.flac")), 1):
            write_tags(f, {"title": f.stem, "artist": "Done", "albumartist": "Done", "album": "Already Tagged",
                           "track": i, "tracktotal": 2, "disc": 1, "disctotal": 1, "date": "2001",
                           "originaldate": "", "label": "", "catalognumber": "", "genre": "",
                           "release_id": 4242, "master_id": 0})

    def start(self, tmp: Path):
        service.configure(data_dir=tmp / "data", config_dir=tmp / "config", discogs=self.discogs)
        service.settings.set(library_roots=[self.root])
        job = service.library.scan()
        assert service.jobs.wait(job.id).state is service.JobState.DONE, service.jobs.get(job.id).message
        return self

    def album_id(self, folder: Path) -> int:
        rows = service.library.albums(service.AlbumFilter(), service.AlbumSort.FOLDER, 0, 100).items
        return next(r.id for r in rows if r.folder == folder)

    def find(self, *album_ids: int) -> None:
        job = service.match.find(list(album_ids))
        done = service.jobs.wait(job.id)
        assert done.state is service.JobState.DONE, done.message


def direct(signal, slot):
    """Connect so the slot runs on the emitting thread (no Qt event loop in tests)."""
    if getattr(service_events, "QT", False):
        from PySide6.QtCore import Qt

        signal.connect(slot, Qt.ConnectionType.DirectConnection)
    else:
        signal.connect(slot)
