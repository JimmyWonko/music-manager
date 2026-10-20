"""Which disc each file of an album is on, by three independent sources. Read-only.

- folder: its CD1 / Disc 2 folder inside the album;
- tag: its disc number tag (DISCNUMBER, TPOS, ...);
- cue/m3u: the album's .cue and .m3u files that list it (by FILE entry or file name first,
  else by track number and title), each on the disc given by its REM DISCNUMBER line, else
  the CDn folder it sits in, else a "CD2" / "Disc 2" in its own name.

A file whose sources don't all agree (ignoring the ones that say nothing) is what plan/apply
hold and --check-disc-swaps reports. Nothing here writes anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .fs import DISC_DIR, AlbumFolder
from .tags.read import read_disc_number, read_easy_tags

SIDECAR_LIST_EXTS = {".cue", ".m3u", ".m3u8"}
TITLE_MIN = 0.80
_REM_DISC = re.compile(r"^\s*REM\s+DISC(?:NUMBER)?\s+(\d+)", re.IGNORECASE)
_FILE = re.compile(r'^\s*FILE\s+(?:"([^"]+)"|(\S+))', re.IGNORECASE)
_TRACK = re.compile(r"^\s*TRACK\s+(\d+)\s+AUDIO", re.IGNORECASE)
_TITLE = re.compile(r'^\s*TITLE\s+"(.*)"\s*$', re.IGNORECASE)
_NAME_DISC = re.compile(r"(?i)(?:^|[^a-z])(?:cd|disc|disk)\s*(\d{1,2})(?!\d)")
_LEAD = re.compile(r"^(\d{1,3})(?=[\s._-])")


@dataclass(frozen=True)
class SidecarListing:
    path: Path
    disc: int | None
    names: frozenset[str]  # casefolded stems of the files it lists
    tracks: tuple[tuple[int, str], ...]  # (track number, title) it lists


@dataclass(frozen=True)
class DiscSources:
    path: Path
    folder_disc: int | None
    tag_disc: int | None
    sidecar_discs: tuple[int, ...] = ()  # the discs of the cue/m3u files that list it (sorted)
    sidecars: tuple[Path, ...] = field(default_factory=tuple)

    @property
    def sidecar_disc(self) -> int | None:
        return self.sidecar_discs[0] if len(self.sidecar_discs) == 1 else None

    @property
    def agree(self) -> bool:
        said = {d for d in (self.folder_disc, self.tag_disc) if d is not None} | set(self.sidecar_discs)
        return len(said) <= 1

    @property
    def odd_one_out(self) -> str:
        """"folder", "tag" or "sidecar" when the other two agree against it; "" when that can't be told."""
        f, t, s = self.folder_disc, self.tag_disc, self.sidecar_disc
        if None in (f, t, s) or len(self.sidecar_discs) != 1:
            return ""
        if t == s != f:
            return "folder"
        if f == s != t:
            return "tag"
        if f == t != s:
            return "sidecar"
        return ""


def _folder_disc(path: Path, album_path: Path) -> int | None:
    parent = path.parent
    if parent == album_path or not DISC_DIR.match(parent.name):
        return None
    m = re.search(r"\d+", parent.name)
    return int(m.group(0)) if m else None


def _sidecar_disc(path: Path, album_path: Path, text: str) -> int | None:
    m = _REM_DISC.search(text) if path.suffix.lower() == ".cue" else None
    if m:
        return int(m.group(1))
    folder = _folder_disc(path, album_path)
    if folder is not None:
        return folder
    m = _NAME_DISC.search(path.stem)
    return int(m.group(1)) if m else None


def _parse(path: Path, album_path: Path) -> SidecarListing | None:
    try:
        raw = path.read_bytes()[:1_000_000]
    except OSError:
        return None
    text = raw.decode("utf-8", errors="replace").lstrip("\ufeff")
    names: set[str] = set()
    tracks: list[tuple[int, str]] = []
    if path.suffix.lower() == ".cue":
        number: int | None = None
        for line in text.splitlines():
            if m := _FILE.match(line):
                names.add(Path((m.group(1) or m.group(2)).replace("\\", "/")).stem.casefold())
            elif m := _TRACK.match(line):
                number = int(m.group(1))
            elif (m := _TITLE.match(line)) and number is not None:
                tracks.append((number, m.group(1)))
                number = None
    else:
        title = ""
        index = 0
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.upper().startswith("#EXTINF:"):
                info = line.split(",", 1)[1] if "," in line else ""
                title = info.split(" - ", 1)[1] if " - " in info else info
                continue
            if line.startswith("#"):
                continue
            index += 1
            names.add(Path(line.replace("\\", "/")).stem.casefold())
            if title:
                tracks.append((index, title.strip()))
            title = ""
    if not names and not tracks:
        return None
    return SidecarListing(path, _sidecar_disc(path, album_path, text), frozenset(names), tuple(tracks))


def sidecar_listings(album: AlbumFolder) -> list[SidecarListing]:
    """The album's .cue/.m3u files (its own and those in its extra folders) that list tracks."""
    candidates = [p for p in album.sidecars if p.suffix.lower() in SIDECAR_LIST_EXTS]
    for folder in album.extra_dirs:
        try:
            candidates += sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in SIDECAR_LIST_EXTS)
        except OSError:
            continue
    out = []
    for path in candidates:
        listing = _parse(path, album.path)
        if listing is not None and listing.disc is not None:
            out.append(listing)
    return out


def _number_and_title(path: Path) -> tuple[int | None, str]:
    from .match import _title_from_stem  # match imports this module

    try:
        tags = read_easy_tags(path)
    except Exception:  # noqa: BLE001 - unreadable: fall back to the name
        tags = {}
    number = None
    raw = tags.get("tracknumber", "").split("/")[0].strip()
    if raw.isdigit():
        number = int(raw)
    elif m := _LEAD.match(path.stem):
        number = int(m.group(1))
    title = tags.get("title", "").strip() or _title_from_stem(path.stem)
    return number, title


def disc_sources(album: AlbumFolder, listings: list[SidecarListing] | None = None) -> list[DiscSources]:
    """Folder, tag and cue/m3u disc of every track file, in album.tracks order."""
    from .match import similar  # match imports this module

    listings = sidecar_listings(album) if listings is None else listings
    out = []
    for path in album.tracks:
        stem = path.stem.casefold()
        hits = [s for s in listings if stem in s.names]
        if not hits and listings:
            number, title = _number_and_title(path)
            if number is not None and title:
                hits = [s for s in listings
                        if any(n == number and similar(title, t) >= TITLE_MIN for n, t in s.tracks)]
        # A cue's FILE entries and an m3u's lines are relative to the sidecar's folder: when the
        # sidecar beside the file lists it, the same name in another disc folder's sidecar is
        # that disc's own file ("05 - Blue Umbrella.flac" on both discs), not this one.
        beside = [s for s in hits if s.path.parent == path.parent]
        if beside:
            hits = beside
        discs = tuple(sorted({s.disc for s in hits if s.disc is not None}))
        hits.sort(key=lambda s: (s.path.suffix.lower() != ".cue", str(s.path)))
        out.append(DiscSources(path, _folder_disc(path, album.path), read_disc_number(path), discs,
                               tuple(s.path for s in hits)))
    return out


def disagreements(album: AlbumFolder) -> list[DiscSources]:
    """The track files whose folder, disc tag and cue/m3u don't all agree."""
    return [s for s in disc_sources(album) if not s.agree]
