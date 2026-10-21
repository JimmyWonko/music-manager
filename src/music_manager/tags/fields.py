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
    label: str | list[str]  # several: parallel multi-values with catalognumber (tagmap)
    catalognumber: str | list[str]
    genre: str | list[str]
    release_id: int | None
    master_id: int
    position: NotRequired[str]
    # Picard tag names (2026-10-21); missing or None: not written (an existing value is kept)
    style: NotRequired[list[str] | str | None]
    barcode: NotRequired[str | None]
    media: NotRequired[str | None]
    releasecountry: NotRequired[str | None]
    releasetype: NotRequired[list[str] | str | None]
    releasestatus: NotRequired[str | None]
    discsubtitle: NotRequired[str | None]
    artistsort: NotRequired[str | None]
    albumartistsort: NotRequired[str | None]
    artist_id: NotRequired[list[str] | str | None]  # the release artists' Discogs ids
    write_styles: NotRequired[bool]  # write STYLE (Discogs styles); off: STYLE is neither written nor cleared
    styles_in_genre: NotRequired[bool]  # also append the styles to GENRE
    keep_fields: NotRequired[list[str]]  # canonical fields not to write or clear (plan: Latin-1 "dropped")
    migrate_legacy_keys: NotRequired[bool]  # also drop keys earlier versions wrote that Picard doesn't (YEAR)


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

ALWAYS_REPLACED = {"discogs_release_id", "discogs_master_id", "discogs_position", "discogs_artist_id"}

# Fields written as multi-values (one value per entry: Vorbis comments, ID3v2.4 lists, MP4 lists, APE
# null-separated, ASF repeated attributes), as Picard writes them.
MULTI_FIELDS = {"label", "catalognumber", "genre", "style", "releasetype", "discogs_artist_id"}


# When a file already carries a different DISCOGS_RELEASE_ID, these values
# came from that other release, so they are replaced even by "no value".
RELEASE_FIELDS = {"date", "year", "originaldate", "originalyear", "label", "catalognumber", "genre", "style",
                  "barcode", "media", "releasecountry", "releasetype", "releasestatus", "discsubtitle"}
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


def _multi(value) -> list[str] | None:
    items = value if isinstance(value, (list, tuple)) else [value]
    out = [str(v).strip() for v in items if v is not None and str(v).strip()]
    return out or None


def tag_values(meta: TrackMeta) -> dict:
    """meta -> canonical field values as text (a list for MULTI_FIELDS); None means 'no value'."""

    def text(value) -> str | None:
        if value is None:
            return None
        value = str(value).strip()
        return value if value and value != "0" else None

    genre, style = _multi(meta.get("genre")), _multi(meta.get("style"))
    if meta.get("styles_in_genre") and style:
        genre = list(dict.fromkeys([*(genre or []), *style]))
    if not meta.get("write_styles"):
        style = None
    date = clean_date(meta.get("date"))
    original = clean_date(meta.get("originaldate"))
    out = {
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
        "label": _multi(meta.get("label")),
        "catalognumber": _multi(meta.get("catalognumber")),
        "genre": genre,
        "style": style,
        "barcode": text(meta.get("barcode")),
        "media": text(meta.get("media")),
        "releasecountry": text(meta.get("releasecountry")),
        "releasetype": _multi(meta.get("releasetype")),
        "releasestatus": text(meta.get("releasestatus")),
        "discsubtitle": text(meta.get("discsubtitle")),
        "artistsort": text(meta.get("artistsort")),
        "albumartistsort": text(meta.get("albumartistsort")),
        "discogs_release_id": text(meta.get("release_id")),
        "discogs_master_id": text(meta.get("master_id")),
        "discogs_artist_id": _multi(meta.get("artist_id")),
        "discogs_position": text(meta.get("position")),
    }
    for name in meta.get("keep_fields") or ():  # not written: the file keeps its value (Latin-1 "dropped")
        out[name] = None
    return out


def legacy_year(tags, meta: TrackMeta, key: str = "YEAR") -> tuple[bool, bool]:
    """(write YEAR, clear YEAR): Picard writes no YEAR. Earlier versions did: one already there is kept up to
    date, unless meta asks to migrate (then it is removed); none is created."""
    present = any(k.casefold() == key.casefold() for k in tags.keys())  # noqa: SIM118 - VComment iterates pairs
    if meta.get("migrate_legacy_keys"):
        return False, present
    return present, False


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
