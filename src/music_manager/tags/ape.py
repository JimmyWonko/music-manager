"""APEv2 tags (Monkey's Audio, WavPack, Musepack).
"""

from __future__ import annotations

from .fields import TrackMeta, _apply_fields, tag_values
from .vorbis import VORBIS_FIELDS

# APEv2 (Monkey's Audio, WavPack, Musepack). Keys are case-insensitive.
APE_FIELDS: dict[str, tuple[str, tuple[str, ...]]] = {
    **VORBIS_FIELDS,
    "artist": ("Artist", ()),
    "albumartist": ("Album Artist", ("ALBUMARTIST", "ALBUM_ARTIST")),
    "album": ("Album", ()),
    "title": ("Title", ()),
    "tracknumber": ("Track", ("TRACKNUMBER", "TRACKTOTAL", "TOTALTRACKS")),  # "3/12"
    "tracktotal": ("Track", ()),
    "discnumber": ("Disc", ("DISCNUMBER", "DISCTOTAL", "TOTALDISCS")),  # "1/2"
    "disctotal": ("Disc", ()),
    "date": ("Year", ("DATE",)),
    "year": ("Year", ()),
    "label": ("Label", ("ORGANIZATION", "PUBLISHER")),
    "catalognumber": ("CatalogNumber", ("CATALOG",)),
    "genre": ("Genre", ()),
}


def _write_ape(audio, meta: TrackMeta, clear_other: bool, replace: set[str]) -> list[str]:
    if audio.tags is None:
        audio.add_tags()
    tv = tag_values(meta)
    values = dict(tv)
    values["tracknumber"] = f"{tv['tracknumber']}/{tv['tracktotal']}" if tv["tracktotal"] else tv["tracknumber"]
    values["discnumber"] = f"{tv['discnumber']}/{tv['disctotal']}" if tv["disctotal"] else tv["discnumber"]
    values["tracktotal"] = values["disctotal"] = values["year"] = None

    def clearable(tags, key):
        return getattr(tags[key], "kind", 0) == 0  # text items only, never binary (art)

    return _apply_fields(audio.tags, APE_FIELDS, values, clear_other, clearable, replace)
