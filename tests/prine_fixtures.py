"""A synthetic copy of a 2-CD rip whose CD1/ and CD2/ folders were sorted by hand.

Mirrors the reported layout (nothing read from a real library): 21 tracks on disc 1, 20 on disc
2, every file tagged correctly (DISCNUMBER, TRACKNUMBER, TITLE), but the files for tracks
13, 15, 17, 18 and 19 sit in the other disc's folder. Each folder holds the rip's own .cue and
.m3u. Each file's length is its track's length, so its identity survives any renaming.
"""

from __future__ import annotations

import copy
import json

from disc_fixtures import FIXTURES, silent_flac
from mutagen.flac import FLAC

PRINE_ID = 7133997
SWAPPED = (13, 15, 17, 18, 19)
_D1 = ["Amber Lane", "Blue Ridge Rain", "Copper Wire", "Dusty Boots", "Empty Porch", "Far From Memphis",
       "Grandpa's Fiddle", "Hello Old Friend", "Iron Bridge", "June Bug", "Kentucky Moon", "Lonesome Mile",
       "Mason Jar", "Nickel Song", "Old Paradise", "Paper Heart", "Quiet Kitchen", "River Town",
       "Sunday Mule", "Tin Roof", "Upper Room"]
_D2 = ["Velvet Morning", "Whiskey Creek", "Yellow Truck", "Zinc Bucket", "Apple Orchard", "Barn Swallow",
       "Cider Press", "Dandelion", "Ember Glow", "Fencepost", "Green River Road", "Hayloft",
       "Indigo Night", "Jasper Hill", "Kerosene", "Lilac Bush", "Mockingbird", "Northbound",
       "Owl Hollow", "Pine Needle"]
TRACKS = [(1, n, t, 120 + n) for n, t in enumerate(_D1, 1)] + [(2, n, t, 200 + n) for n, t in enumerate(_D2, 1)]
TRUTH = {s: (d, n, t) for d, n, t, s in TRACKS}
ARTIST = "John Prine"
FOLDER = (ARTIST, "[1993]  Anthology [R2 71400]")


def release() -> dict:
    rel = copy.deepcopy(json.loads((FIXTURES / "release_multi_cd.json").read_text(encoding="utf-8")))
    rel.update(id=PRINE_ID, title="Anthology", year=1993, released="1993", master_id=0)
    rel["artists"] = [{"name": ARTIST}]
    rel["labels"] = [{"name": "Rhino", "catno": "R2 71400"}]
    rel["tracklist"] = [{"position": f"{d}-{n}", "type_": "track", "title": t, "duration": f"{s // 60}:{s % 60:02d}"}
                        for d, n, t, s in TRACKS]
    return rel


def name_of(n: int, t: str) -> str:
    return f"{n:02d} {t}.flac"


def _tagged_flac(dest, d, n, t, s):
    silent_flac(dest, s)
    audio = FLAC(dest)
    audio["discnumber"] = [f"{d}/2"]
    audio["tracknumber"] = [str(n)]
    audio["title"] = [t]
    audio["artist"] = [ARTIST]
    audio["album"] = ["Anthology"]
    audio["discogs_release_id"] = [str(PRINE_ID)]
    audio.save()
    return dest


def folder_of(d: int, n: int, swapped=SWAPPED) -> int:
    return 3 - d if n in swapped else d


def _sidecars(album, listing: dict[int, list[tuple[int, str, int]]]):
    for d, rows in listing.items():
        cue = [f'PERFORMER "{ARTIST}"', f'TITLE "Anthology (Disc {d})"']
        m3u = ["#EXTM3U"]
        for n, t, s in rows:
            cue += [f'FILE "{name_of(n, t)}" WAVE', f"  TRACK {n:02d} AUDIO", f'    TITLE "{t}"',
                    "    INDEX 01 00:00:00"]
            m3u += [f"#EXTINF:{s},{ARTIST} - {t}", name_of(n, t)]
        (album / f"CD{d}" / f"{ARTIST} - Anthology CD{d}.cue").write_text("\n".join(cue) + "\n", encoding="utf-8")
        (album / f"CD{d}" / f"{ARTIST} - Anthology CD{d}.m3u").write_text("\n".join(m3u) + "\n", encoding="utf-8")


def hand_sorted(album, cue_follows="tags", swapped=SWAPPED):
    """CD1/ and CD2/ with the swapped numbers in the wrong folder.

    cue_follows="tags": each folder's .cue/.m3u lists its disc's real tracks (the rip's own files).
    cue_follows="folders": they list what the folder holds now (they disagree with the tags).
    """
    for d, n, t, s in TRACKS:
        _tagged_flac(album / f"CD{folder_of(d, n, swapped)}" / name_of(n, t), d, n, t, s)
    listing: dict[int, list[tuple[int, str, int]]] = {1: [], 2: []}
    for d, n, t, s in TRACKS:
        listing[d if cue_follows == "tags" else folder_of(d, n, swapped)].append((n, t, s))
    _sidecars(album, listing)
    return album


def flat(album):
    """The original rip: 41 loose files, numbers restarting per disc, DISCNUMBER 1/2, no CD folders."""
    for d, n, t, s in TRACKS:
        _tagged_flac(album / name_of(n, t), d, n, t, s)
    return album


def identity(path):
    return TRUTH[round(FLAC(path).info.length)]


def tagged(path):
    tags = FLAC(path)
    return int(tags["discnumber"][0].split("/")[0]), int(tags["tracknumber"][0].split("/")[0]), tags["title"][0]


def wrong_folder(album):
    """Audio files whose CDn folder is not the disc their audio is on."""
    out = []
    for p in album.rglob("*.flac"):
        if p.parent.name.startswith("CD") and int(p.parent.name[2:]) != identity(p)[0]:
            out.append(str(p.relative_to(album)))
    return sorted(out)
