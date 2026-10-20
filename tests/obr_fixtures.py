"""A synthetic copy of a 2-CD rip laid out as CD1/ and CD2/ folders with the rip's sidecars.

Mirrors the reported layout (John Prine – The Singing Mailman Delivers, OBR-040; nothing read from
a real library, most titles invented): CD1/ holds 11 tracks, CD2/ 13, each folder its own .cue,
.log, .m3u, .toc and .accurip, and Covers/ the scans. Every file is tagged with its DISCNUMBER,
TRACKNUMBER and TITLE. Both discs have a "Blue Umbrella" at track 5 (studio and live), so a flat
layout collides.

Discogs lists CD2 track 9 as two tracks ("Hey Good Lookin'" and "Jambalaya (On The Bayou)"),
while the rip has one file for the medley: 14 Discogs tracks against 13 files on CD2. Each file's
length is the length of the (first) Discogs track it stands for, so its identity survives renaming.
"""

from __future__ import annotations

import copy
import json

from disc_fixtures import FIXTURES, silent_flac
from mutagen.flac import FLAC

OBR_ID = 40040
ARTIST = "John Prine"
TITLE = "The Singing Mailman Delivers"
SIDECARS = ("cue", "log", "m3u", "toc", "accurip")
D1 = ["Hello in There", "Quiet Kitchen", "Paper Heart", "Iron Bridge", "Blue Umbrella", "Copper Wire",
      "Dusty Boots", "Empty Porch", "Far From Memphis", "Grandpa's Fiddle", "Kentucky Moon"]
MEDLEY = ("Hey Good Lookin'", "Jambalaya (On The Bayou)")
# The rip's CD2: 13 files; file 9 is the medley.
D2_FILES = ["Velvet Morning", "Whiskey Creek", "Yellow Truck", "Zinc Bucket", "Blue Umbrella", "Barn Swallow",
            "Cider Press", "Dandelion", "Hey Good Lookin',Jambalaya (On the Bayou)", "Ember Glow",
            "Fencepost", "Great Society Conflict Veteran's Blues", "Spanish Pipedream"]
# What Discogs lists for CD2: 14 tracks.
D2_DISCOGS = D2_FILES[:8] + list(MEDLEY) + D2_FILES[9:]


def seconds(d: int, n: int) -> int:
    return (100 if d == 1 else 200) + n


def release(shape: str = "plain") -> dict:
    """shape "match": CD2 as the rip has it; "plain": the medley is tracks 2-9 and 2-10; "index": one index track with two sub_tracks."""
    rel = copy.deepcopy(json.loads((FIXTURES / "release_multi_cd.json").read_text(encoding="utf-8")))
    rel.update(id=OBR_ID, title=TITLE, year=2016, released="2016", master_id=0)
    rel["artists"] = [{"name": ARTIST}]
    rel["labels"] = [{"name": "Oh Boy Records", "catno": "OBR-040"}]
    rel["formats"] = [{"name": "CD", "qty": "2"}]
    tl = [{"position": f"1-{n}", "type_": "track", "title": t, "duration": "1:00"} for n, t in enumerate(D1, 1)]
    if shape == "match":  # Discogs lists CD2 as the rip has it (13 tracks)
        tl += [{"position": f"2-{n}", "type_": "track", "title": t, "duration": "1:00"}
               for n, t in enumerate(D2_FILES, 1)]
    elif shape == "plain":
        tl += [{"position": f"2-{n}", "type_": "track", "title": t, "duration": "1:00"}
               for n, t in enumerate(D2_DISCOGS, 1)]
    else:
        tl += [{"position": f"2-{n}", "type_": "track", "title": t, "duration": "1:00"}
               for n, t in enumerate(D2_FILES[:8], 1)]
        tl.append({"position": "", "type_": "index", "title": " / ".join(MEDLEY), "sub_tracks": [
            {"position": "2-9a", "type_": "track", "title": MEDLEY[0], "duration": "1:00"},
            {"position": "2-9b", "type_": "track", "title": MEDLEY[1], "duration": "1:00"}]})
        tl += [{"position": f"2-{n}", "type_": "track", "title": t, "duration": "1:00"}
               for n, t in enumerate(D2_FILES[9:], 10)]
    rel["tracklist"] = tl
    return rel


def single_disc_release() -> dict:
    rel = release()
    rel["formats"] = [{"name": "CD", "qty": "1"}]
    rel["tracklist"] = [{"position": str(n), "type_": "track", "title": t, "duration": "1:00"}
                        for n, t in enumerate(D1, 1)]
    return rel


def file_name(n: int, t: str) -> str:
    return f"{n:02d} - {t}.flac"


def _tagged(dest, d, n, t):
    silent_flac(dest, seconds(d, n))
    audio = FLAC(dest)
    audio["discnumber"] = [str(d)]
    audio["tracknumber"] = [str(n)]
    audio["title"] = [t]
    audio["artist"] = [ARTIST]
    audio.save()


def make_album(album, discs: dict[int, list[str]] | None = None, disc_dir: str = "CD{d}"):
    """The rip: one folder per disc (disc_dir), sidecars in each, Covers/ beside them."""
    discs = discs or {1: D1, 2: D2_FILES}
    for d, titles in discs.items():
        folder = album / disc_dir.format(d=d)
        for n, t in enumerate(titles, 1):
            _tagged(folder / file_name(n, t), d, n, t)
        for ext in SIDECARS:
            body = (f'REM DISCNUMBER {d}\n' + "".join(f'FILE "{file_name(n, t)}" WAVE\n  TRACK {n:02d} AUDIO\n'
                                                       for n, t in enumerate(titles, 1))) if ext == "cue" else f"{ext} {d}\n"
            if ext == "m3u":
                body = "".join(file_name(n, t) + "\n" for n, t in enumerate(titles, 1))
            (folder / f"{ARTIST} - {TITLE} CD{d}.{ext}").write_text(body, encoding="utf-8")
    covers = album / "Covers"
    covers.mkdir(parents=True, exist_ok=True)
    (covers / "front.jpg").write_bytes(b"jpg")
    return album


def tags(path):
    t = FLAC(path)
    return int(t["discnumber"][0].split("/")[0]), int(t["tracknumber"][0].split("/")[0]), t["title"][0]
