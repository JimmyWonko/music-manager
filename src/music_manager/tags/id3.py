"""ID3v2.4 frames (MP3, and ID3 chunks inside WAV and AIFF).
"""

from __future__ import annotations

from mutagen.id3 import ID3, TALB, TCON, TDOR, TDRC, TIT2, TPE1, TPE2, TPOS, TPUB, TRCK, TXXX

from .fields import TrackMeta, is_protected_key, tag_values

# ID3v2.4 (MP3, and ID3 chunks inside WAV and AIFF)
ID3_TEXT_FRAMES = {
    "artist": ("TPE1", TPE1),
    "albumartist": ("TPE2", TPE2),
    "album": ("TALB", TALB),
    "title": ("TIT2", TIT2),
    "date": ("TDRC", TDRC),
    "originaldate": ("TDOR", TDOR),
    "label": ("TPUB", TPUB),
    "genre": ("TCON", TCON),
}


# TXXX descriptions that are other spellings of a field written as a frame
ID3_TXXX_ALIASES = {
    "albumartist": ("ALBUM ARTIST", "ALBUMARTIST"),
    "date": ("DATE", "YEAR"),
    "originaldate": ("ORIGINALDATE", "ORIGINALYEAR", "ORIGINAL YEAR"),
    "label": ("LABEL", "PUBLISHER", "ORGANIZATION"),
    "tracknumber": ("TRACKTOTAL", "TOTALTRACKS"),
    "discnumber": ("DISCTOTAL", "TOTALDISCS"),
}
ID3_TXXX_FIELDS = {
    "catalognumber": ("CATALOGNUMBER", ("CATALOG",)),
    "discogs_release_id": ("DISCOGS_RELEASE_ID", ()),
    "discogs_master_id": ("DISCOGS_MASTER_ID", ()),
    "discogs_position": ("DISCOGS_POSITION", ()),
}
ID3_PROTECTED_FRAMES = {"TSSE", "TENC", "TLEN", "TFLT", "TSRC", "TDEN", "TOWN"}


def _del_txxx(tags: ID3, descs) -> list[str]:
    wanted = {d.casefold() for d in descs}
    removed = []
    for frame in list(tags.getall("TXXX")):
        if (frame.desc or "").casefold() in wanted:
            tags.delall(frame.HashKey)
            removed.append(frame.HashKey)
    return removed


def _apply_id3(tags: ID3, meta: TrackMeta, clear_other: bool, replace: set[str]) -> list[str]:
    tv = tag_values(meta)
    removed: list[str] = []

    def pair(n, total):
        return f"{n}/{total}" if total else n

    for name, (frame_id, cls) in ID3_TEXT_FRAMES.items():
        value = tv[name]
        if value is None:
            if name in replace:
                if tags.getall(frame_id):
                    tags.delall(frame_id)
                    removed.append(frame_id)
                removed += _del_txxx(tags, ID3_TXXX_ALIASES.get(name, ()))
            continue
        tags.delall(frame_id)
        removed += _del_txxx(tags, ID3_TXXX_ALIASES.get(name, ()))
        tags.add(cls(encoding=3, text=value))
    if tv["date"]:
        for old in ("TYER", "TDAT", "TIME"):  # v2.3 date frames; TDRC replaces them
            if tags.getall(old):
                tags.delall(old)
                removed.append(old)
    if (tv["originaldate"] or "originaldate" in replace) and tags.getall("TORY"):
        tags.delall("TORY")
        removed.append("TORY")
    for name, frame_id, cls, total in (
        ("tracknumber", "TRCK", TRCK, "tracktotal"),
        ("discnumber", "TPOS", TPOS, "disctotal"),
    ):
        if tv[name] is None:
            continue
        tags.delall(frame_id)
        removed += _del_txxx(tags, ID3_TXXX_ALIASES.get(name, ()))
        tags.add(cls(encoding=3, text=pair(tv[name], tv[total])))
    for name, (desc, aliases) in ID3_TXXX_FIELDS.items():
        value = tv[name]
        if value is None and name not in replace:
            continue
        gone = _del_txxx(tags, (desc, *aliases))
        if value is None:
            removed += gone
        else:
            removed += [k for k in gone if k.casefold() != f"txxx:{desc}".casefold()]
            tags.add(TXXX(encoding=3, desc=desc, text=value))

    if clear_other:
        owned_txxx = {d.casefold() for d, _ in ID3_TXXX_FIELDS.values()}
        for key in list(tags.keys()):
            frame = tags[key]
            fid = frame.FrameID
            if fid == "TXXX":
                desc = frame.desc or ""
                if desc.casefold() in owned_txxx or is_protected_key(desc):
                    continue
            elif fid == "COMM":
                if is_protected_key(frame.desc or ""):
                    continue
            elif fid.startswith("T"):
                if fid in ID3_PROTECTED_FRAMES or fid in {"TRCK", "TPOS"} or fid in {f for f, _ in ID3_TEXT_FRAMES.values()}:
                    continue
            elif not (fid.startswith("W") or fid == "UFID"):
                continue  # art, lyrics, ratings, private and binary frames stay
            tags.delall(key)
            removed.append(key)
    return removed
