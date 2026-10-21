"""Windows Media (ASF) attributes.
"""

from __future__ import annotations

from .fields import TrackMeta, _apply_fields, tag_values

# Windows Media (ASF) attribute names, as Picard writes them.
ASF_FIELDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "artist": ("Author", ()),
    "albumartist": ("WM/AlbumArtist", ()),
    "album": ("WM/AlbumTitle", ()),
    "title": ("Title", ()),
    "tracknumber": ("WM/TrackNumber", ("WM/Track",)),
    "tracktotal": ("TotalTracks", ("WM/TrackTotal",)),
    "discnumber": ("WM/PartOfSet", ()),
    "disctotal": ("TotalDiscs", ("WM/DiscTotal",)),
    "date": ("WM/Year", ()),
    "originaldate": ("WM/OriginalReleaseTime", ()),
    "originalyear": ("WM/OriginalReleaseYear", ()),
    "label": ("WM/Publisher", ()),
    "catalognumber": ("WM/CatalogNo", ()),
    "genre": ("WM/Genre", ()),
    "style": ("STYLE", ()),
    "barcode": ("WM/Barcode", ()),
    "media": ("WM/Media", ()),
    "releasecountry": ("MusicBrainz/Album Release Country", ()),
    "releasetype": ("MusicBrainz/Album Type", ()),
    "releasestatus": ("MusicBrainz/Album Status", ()),
    "discsubtitle": ("WM/SetSubTitle", ()),
    "artistsort": ("WM/ArtistSortOrder", ()),
    "albumartistsort": ("WM/AlbumArtistSortOrder", ()),
    "discogs_release_id": ("DISCOGS_RELEASE_ID", ()),
    "discogs_master_id": ("DISCOGS_MASTER_ID", ()),
    "discogs_artist_id": ("DISCOGS_ARTIST_ID", ()),
    "discogs_position": ("DISCOGS_POSITION", ()),
}


def _write_asf(audio, meta: TrackMeta, clear_other: bool, replace: set[str]) -> list[str]:
    from mutagen.asf import ASFUnicodeAttribute

    if audio.tags is None:
        audio.add_tags()
    tv = tag_values(meta)
    values: dict = {name: tv.get(name) for name in ASF_FIELDS}
    if tv["disctotal"] and tv["discnumber"]:
        values["discnumber"] = f"{tv['discnumber']}/{tv['disctotal']}"

    def clearable(tags, key):
        return all(isinstance(v, ASFUnicodeAttribute) for v in tags[key])

    return _apply_fields(audio.tags, ASF_FIELDS, values, clear_other, clearable, replace)
