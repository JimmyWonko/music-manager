"""ID3v2.4 frames (MP3, and ID3 chunks inside WAV and AIFF).
"""

from __future__ import annotations

from mutagen.id3 import ID3, TALB, TCON, TDOR, TDRC, TIT2, TMED, TPE1, TPE2, TPOS, TPUB, TRCK, TSO2, TSOP, TSST, TXXX

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
    "media": ("TMED", TMED),
    "discsubtitle": ("TSST", TSST),
    "artistsort": ("TSOP", TSOP),
    "albumartistsort": ("TSO2", TSO2),
}


# TXXX descriptions that are other spellings of a field written as a frame
ID3_TXXX_ALIASES = {
    "albumartist": ("ALBUM ARTIST", "ALBUMARTIST"),
    "date": ("DATE", "YEAR"),
    "originaldate": ("ORIGINALDATE", "ORIGINALYEAR", "ORIGINAL YEAR"),
    "label": ("LABEL", "PUBLISHER", "ORGANIZATION"),
    "tracknumber": ("TRACKTOTAL", "TOTALTRACKS"),
    "discnumber": ("DISCTOTAL", "TOTALDISCS"),
    "albumartistsort": ("ALBUMARTISTSORT",),  # Picard moves it to TSO2 too
}
ID3_TXXX_FIELDS = {
    "catalognumber": ("CATALOGNUMBER", ("CATALOG",)),
    "barcode": ("BARCODE", ()),
    "releasecountry": ("MusicBrainz Album Release Country", ()),
    "releasetype": ("MusicBrainz Album Type", ()),
    "releasestatus": ("MusicBrainz Album Status", ()),
    "style": ("STYLE", ()),
    "discogs_release_id": ("DISCOGS_RELEASE_ID", ()),
    "discogs_master_id": ("DISCOGS_MASTER_ID", ()),
    "discogs_artist_id": ("DISCOGS_ARTIST_ID", ()),
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


# --- ID3v2.3 (2026-10-21) --------------------------------------------------------------------------------------------
# WAV and AIFF are written as ID3v2.3 (v2.4 in those containers breaks older players); MP3 stays v2.4 unless
# meta["id3_v23"]. v2.3 has no UTF-8: text is UTF-16 by default, or Latin-1 with meta["id3_latin1"], where the
# fields we write lose what Latin-1 can't hold (see latin1_losses, listed by the plan before anything is written).
# Frames we don't manage become Latin-1 only when that loses nothing; otherwise UTF-16.

_MANAGED_FRAMES = {fid for fid, _cls in ID3_TEXT_FRAMES.values()} | {"TRCK", "TPOS"}
_MANAGED_TXXX = {desc.casefold() for desc, _aliases in ID3_TXXX_FIELDS.values()}


def _lost(text: str) -> str:
    return "".join(ch for ch in text if ord(ch) > 0xFF)


def latin1_losses(values: dict) -> list[tuple[str, str, str]]:
    """(field, value, the characters Latin-1 can't hold) for each value that would lose some, in field order."""
    out = []
    for name, value in values.items():
        for item in value if isinstance(value, (list, tuple)) else [value]:
            if isinstance(item, str) and _lost(item):
                out.append((name, item, _lost(item)))
    return out


def prepare_v23(tags: ID3, latin1: bool) -> None:
    """Set every text frame's encoding for a v2.3 save (UTF-16, or Latin-1 as described above)."""
    for frame in tags.values():
        if not hasattr(frame, "encoding") or not isinstance(getattr(frame, "text", None), list):
            continue
        managed = frame.FrameID in _MANAGED_FRAMES or (
            frame.FrameID == "TXXX" and (frame.desc or "").casefold() in _MANAGED_TXXX)
        texts = [str(t) for t in frame.text]
        if latin1 and (managed or not any(_lost(t) for t in texts)):
            frame.encoding = 0
            if frame.FrameID not in ("TDRC", "TDOR"):  # timestamps are ASCII
                frame.text = [t.encode("latin-1", "replace").decode("latin-1") for t in texts]
        else:
            frame.encoding = 1
