"""MP4/M4A atoms.
"""

from __future__ import annotations

from mutagen.mp4 import MP4FreeForm

from .fields import TrackMeta, _apply_fields, tag_values

FF = "----:com.apple.iTunes:"
MP4_FIELDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "artist": ("\xa9ART", ()),
    "albumartist": ("aART", ()),
    "album": ("\xa9alb", ()),
    "title": ("\xa9nam", ()),
    "tracknumber": ("trkn", ()),
    "discnumber": ("disk", ()),
    "date": ("\xa9day", ()),
    "originaldate": (FF + "ORIGINALDATE", ()),
    "originalyear": (FF + "ORIGINALYEAR", ()),
    "label": (FF + "LABEL", (FF + "publisher",)),
    "catalognumber": (FF + "CATALOGNUMBER", ()),
    "genre": ("\xa9gen", ("gnre",)),
    "discogs_release_id": (FF + "DISCOGS_RELEASE_ID", ()),
    "discogs_master_id": (FF + "DISCOGS_MASTER_ID", ()),
    "discogs_position": (FF + "DISCOGS_POSITION", ()),
}
MP4_TEXT_ATOMS = {"aART", "soal", "soar", "soaa", "sonm", "soco", "desc", "ldes", "cprt", "gnre", "cpil", "tmpo", "purl", "egid"}


def _write_mp4_tags(audio, meta: TrackMeta, clear_other: bool, replace: set[str]) -> list[str]:
    if audio.tags is None:
        audio.add_tags()
    tv = tag_values(meta)
    values: dict = {}
    for name, value in tv.items():
        if name not in MP4_FIELDS:
            continue
        key = MP4_FIELDS[name][0]
        if value is None:
            values[name] = None
        elif key.startswith("----:"):
            values[name] = [MP4FreeForm(value.encode("utf-8"))]
        else:
            values[name] = [value]
    values["tracknumber"] = [(int(meta["track"]), int(meta.get("tracktotal") or 0))]
    values["discnumber"] = [(int(meta["disc"]), int(meta.get("disctotal") or 0))]

    def clearable(_tags, key):
        return key.startswith("\xa9") or key.startswith("----:") or key in MP4_TEXT_ATOMS

    return _apply_fields(audio.tags, MP4_FIELDS, values, clear_other, clearable, replace)
