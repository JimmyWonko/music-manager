"""Vorbis comments (FLAC, Ogg Vorbis/Opus/FLAC/Speex).
"""

from __future__ import annotations

from .fields import TrackMeta, _always_clearable, _apply_fields, legacy_year, tag_values

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
    "style": ("STYLE", ()),
    "barcode": ("BARCODE", ()),
    "media": ("MEDIA", ()),
    "releasecountry": ("RELEASECOUNTRY", ()),
    "releasetype": ("RELEASETYPE", ()),
    "releasestatus": ("RELEASESTATUS", ()),
    "discsubtitle": ("DISCSUBTITLE", ()),
    "artistsort": ("ARTISTSORT", ()),
    "albumartistsort": ("ALBUMARTISTSORT", ()),
    "discogs_release_id": ("DISCOGS_RELEASE_ID", ()),
    "discogs_master_id": ("DISCOGS_MASTER_ID", ()),
    "discogs_artist_id": ("DISCOGS_ARTIST_ID", ()),
    "discogs_position": ("DISCOGS_POSITION", ()),
}


def _write_vorbis(audio, meta: TrackMeta, clear_other: bool, replace: set[str]) -> list[str]:
    if audio.tags is None:
        audio.add_tags()
    values = {name: (v if isinstance(v, list) else [v]) if v is not None else None
              for name, v in tag_values(meta).items()}
    keep_year, drop_year = legacy_year(audio.tags, meta)
    if not keep_year:
        values["year"] = None
    replace = (replace | {"year"}) if drop_year else (replace - {"year"} if not keep_year else replace)
    return _apply_fields(audio.tags, VORBIS_FIELDS, values, clear_other, _always_clearable, replace)
