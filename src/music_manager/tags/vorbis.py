"""Vorbis comments (FLAC, Ogg Vorbis/Opus/FLAC/Speex).
"""

from __future__ import annotations

from .fields import TrackMeta, _always_clearable, _apply_fields, tag_values

# canonical field -> (key written, other spellings of the same field)
VORBIS_FIELDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "artist": ("ARTIST", ()),
    "albumartist": ("ALBUMARTIST", ("ALBUM ARTIST", "ALBUM_ARTIST")),
    "album": ("ALBUM", ()),
    "title": ("TITLE", ()),
    "tracknumber": ("TRACKNUMBER", ()),
    "tracktotal": ("TRACKTOTAL", ("TOTALTRACKS",)),
    "discnumber": ("DISCNUMBER", ()),
    "disctotal": ("DISCTOTAL", ("TOTALDISCS",)),
    "date": ("DATE", ()),
    "year": ("YEAR", ()),
    "originaldate": ("ORIGINALDATE", ()),
    "originalyear": ("ORIGINALYEAR", ()),
    "label": ("LABEL", ("ORGANIZATION", "PUBLISHER")),
    "catalognumber": ("CATALOGNUMBER", ("CATALOG",)),
    "genre": ("GENRE", ()),
    "discogs_release_id": ("DISCOGS_RELEASE_ID", ()),
    "discogs_master_id": ("DISCOGS_MASTER_ID", ()),
    "discogs_position": ("DISCOGS_POSITION", ()),
}


def _write_vorbis(audio, meta: TrackMeta, clear_other: bool, replace: set[str]) -> list[str]:
    if audio.tags is None:
        audio.add_tags()
    values = {name: ([v] if v is not None else None) for name, v in tag_values(meta).items()}
    return _apply_fields(audio.tags, VORBIS_FIELDS, values, clear_other, _always_clearable, replace)
