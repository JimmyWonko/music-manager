"""Canonical tag fields this tool owns, protected keys, and the generic field-replacement logic.
"""

from __future__ import annotations

import re
from typing import NotRequired, TypedDict

from ..naming import year4


class TrackMeta(TypedDict):
    """Everything write_tags() writes for one track."""
    title: str
    artist: str
    albumartist: str
    album: str
    track: int
    tracktotal: int
    disc: int
    disctotal: int
    date: str
    originaldate: str
    label: str
    catalognumber: str
    genre: str
    release_id: int | None
    master_id: int
    position: NotRequired[str]


# ---------------------------------------------------------------------------
# Tag writing
#
# Every format gets its own conventional field names (Picard/MusicBrainz
# style). Only the fields this tool owns are replaced: before one of them is
# written, every spelling of that field (aliases such as TOTALTRACKS next to
# TRACKTOTAL) is removed so no stale or contradictory copy survives. A field
# with no new value is left alone, except the DISCOGS_* ids, which describe
# the chosen release and are always replaced as a group.
#
# Everything else (ReplayGain, AccurateRip, encoder and gapless info,
# embedded art, lyrics, comments, MusicBrainz ids, ...) is kept. With
# --clear-other-tags the remaining text metadata is removed too, but
# PROTECTED fields (loudness, rip verification, encoder, art, lyrics, ISRC,
# ratings, embedded cue/log) are still kept.
# ---------------------------------------------------------------------------

ALWAYS_REPLACED = {"discogs_release_id", "discogs_master_id", "discogs_position"}


# When a file already carries a different DISCOGS_RELEASE_ID, these values
# came from that other release, so they are replaced even by "no value".
RELEASE_FIELDS = {"date", "year", "originaldate", "originalyear", "label", "catalognumber", "genre"}
ORIGINAL_FIELDS = {"originaldate", "originalyear"}

PROTECTED_SUBSTRINGS = (
    "replaygain",
    "r128",
    "accuraterip",
    "ctdb",
    "itunsmpb",
    "itunnorm",
    "itunpgap",
    "encod",  # ENCODER, ENCODED-BY, ENCODERSETTINGS, WM/EncodingSettings, ...
    "lyrics",
    "isrc",
    "cuesheet",
    "cdtoc",
    "picture",
    "coverart",
    "cover art",
    "rating",
    "playcount",
    "peakvalue",
    "averagelevel",
    "mediaclass",
    "toolname",
    "toolversion",
    "isvbr",
    "deviceconformance",
)
PROTECTED_EXACT = {"log", "eaclog", "crc", "crc32", "©too", "©lyr", "covr", "pgap"}


def is_protected_key(name: str) -> bool:
    key = name.casefold()
    if key.startswith("----:"):
        key = key.rsplit(":", 1)[-1]
    return key in PROTECTED_EXACT or any(part in key for part in PROTECTED_SUBSTRINGS)


def clean_date(value: str | int | None) -> str:
    """Discogs writes unknown parts as 00 ("1999-00-00"); drop them."""
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?", text)
    if not match:
        return year4(text)
    year, month, day = match.groups()
    if not month or month == "00":
        return year
    if not day or day == "00":
        return f"{year}-{month}"
    return f"{year}-{month}-{day}"


def tag_values(meta: TrackMeta) -> dict[str, str | None]:
    """meta -> canonical field values as text; None means 'no value'."""

    def text(value) -> str | None:
        if value is None:
            return None
        value = str(value).strip()
        return value if value and value != "0" else None

    date = clean_date(meta.get("date"))
    original = clean_date(meta.get("originaldate"))
    return {
        "artist": text(meta.get("artist")),
        "albumartist": text(meta.get("albumartist")),
        "album": text(meta.get("album")),
        "title": text(meta.get("title")),
        "tracknumber": text(meta.get("track")),
        "tracktotal": text(meta.get("tracktotal")),
        "discnumber": text(meta.get("disc")),
        "disctotal": text(meta.get("disctotal")),
        "date": text(date),
        "year": text(year4(date)),
        "originaldate": text(original),
        "originalyear": text(year4(original)),
        "label": text(meta.get("label")),
        "catalognumber": text(meta.get("catalognumber")),
        "genre": text(meta.get("genre")),
        "discogs_release_id": text(meta.get("release_id")),
        "discogs_master_id": text(meta.get("master_id")),
        "discogs_position": text(meta.get("position")),
    }


def _apply_fields(tags, fields, values: dict, clear_other: bool, clearable, replace: set[str]) -> list[str]:
    """Replace owned fields in a dict-like tag container.

    values maps canonical field -> value in the container's own format, or
    None for 'no new value'. Fields in replace are cleared even without a
    new value. Returns keys removed and not written again.
    """
    removed: list[str] = []
    owned: set[str] = set()
    written: set[str] = set()
    for name, (key, aliases) in fields.items():
        spellings = {key.casefold(), *(a.casefold() for a in aliases)}
        owned |= spellings
        value = values.get(name)
        if value is None and name not in replace:
            continue
        for existing in list(tags.keys()):
            if existing.casefold() in spellings and existing.casefold() not in written:
                try:
                    del tags[existing]
                except KeyError:
                    continue
                if value is None or existing.casefold() != key.casefold():
                    removed.append(existing)
        if value is not None:
            tags[key] = value
            written.add(key.casefold())
    if clear_other:
        for existing in list(tags.keys()):
            if existing.casefold() in owned or is_protected_key(existing) or not clearable(tags, existing):
                continue
            try:
                del tags[existing]
            except KeyError:
                continue
            removed.append(existing)
    return removed


def _always_clearable(_tags, _key) -> bool:
    return True
