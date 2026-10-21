"""Reading tags: easy tags for guessing, Discogs release ids, and tag years.
"""

from __future__ import annotations

import re
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen import MutagenError
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3, ID3NoHeaderError
from mutagen.mp4 import MP4

from ..naming import year4


def _read_tag_file(path: Path, easy: bool = False):
    """Return (audio, tags). audio is None when only an ID3 tag was read.

    mutagen's MP3 opener also scans for an MPEG frame and raises
    HeaderNotFoundError when the file is tag-only or corrupt. ID3 and
    EasyID3 read the tag without that scan.
    """
    try:
        if path.suffix.lower() == ".mp3":
            return None, (EasyID3(path) if easy else ID3(path))
        audio = MutagenFile(path, easy=easy)
    except ID3NoHeaderError:
        return None, None
    except MutagenError as exc:
        print(f"  warning: cannot read tags: {path}: {exc}")
        return None, None
    if audio is None or not audio.tags:
        return audio, None
    return audio, audio.tags


def read_easy_tags(path: Path) -> dict[str, str]:
    _audio, tags = _read_tag_file(path, easy=True)
    if not tags:
        return {}
    out: dict[str, str] = {}
    for key in ("artist", "albumartist", "album", "title", "date", "originaldate", "tracknumber"):
        value = tags.get(key)
        if value:
            out[key] = str(value[0])
    return out


BARCODE_KEYS = ("barcode", "upc", "ean", "txxx:barcode", "txxx:upc", "txxx:ean",
                "----:com.apple.itunes:barcode", "wm/barcode", "wm/upc")


def normalize_barcode(text: str | None) -> str:
    """Digits of a UPC/EAN barcode ("5 012345-678900" -> "5012345678900"); "" unless it has 8 to 14 digits."""
    digits = re.sub(r"[\s.-]", "", str(text or ""))
    return digits if digits.isdigit() and 8 <= len(digits) <= 14 else ""


def read_barcode(path: Path) -> str:
    """The file's barcode tag (BARCODE, UPC or EAN, any format), as digits; "" if none."""
    from .textmap import tag_text_map

    _audio, tags = _read_tag_file(path)
    if not tags:
        return ""
    try:
        text = tag_text_map(tags)
    except Exception:  # noqa: BLE001 - odd tags: no barcode
        return ""
    for key in BARCODE_KEYS:
        found = normalize_barcode(text.get(key))
        if found:
            return found
    return ""


def already_tagged(path: Path) -> str | None:
    audio, tags = _read_tag_file(path)
    if not tags:
        return None
    for key in ("DISCOGS_RELEASE_ID", "discogs_release_id"):
        if key in tags:
            value = tags[key]
            return str(value[0] if isinstance(value, list) else value)
    if isinstance(tags, ID3):
        for frame in tags.getall("TXXX"):
            if (frame.desc or "").casefold() == "discogs_release_id":
                return str(frame.text[0]) if frame.text else None
    if isinstance(audio, MP4) and "----:com.apple.iTunes:DISCOGS_RELEASE_ID" in tags:
        raw = tags["----:com.apple.iTunes:DISCOGS_RELEASE_ID"][0]
        return raw.decode() if isinstance(raw, bytes) else str(raw)
    return None


def read_disc_number(path: Path) -> int | None:
    """The file's disc number tag (DISCNUMBER, ID3 TPOS, MP4 disk, ...), None when absent or unreadable.

    Read-only and quiet: a file that cannot be read simply has no disc number.
    """
    from .textmap import tag_text_map

    try:
        if path.suffix.lower() == ".mp3":
            tags = ID3(path)
        else:
            audio = MutagenFile(path)
            tags = audio.tags if audio is not None else None
    except Exception:  # noqa: BLE001 - unreadable or untagged: no disc number
        return None
    text = tag_text_map(tags).get("discnumber", "").strip()
    digits = text.split("/")[0].strip()
    return int(digits) if digits.isdigit() and int(digits) > 0 else None


def album_release_ids(tracks: list[Path]) -> dict[Path, str | None]:
    """Discogs release id of every track (None when a track has none or can't be read)."""
    out: dict[Path, str | None] = {}
    for track in tracks:
        try:
            out[track] = already_tagged(track)
        except Exception:  # noqa: BLE001 - unreadable counts as untagged
            out[track] = None
    return out


DATE_KEYS = ("date", "year", "tdrc", "tyer", "©day", "wm/year")
ORIGINAL_KEYS = (
    "originaldate", "originalyear", "tdor", "tory",
    "txxx:originaldate", "txxx:originalyear",
    "----:com.apple.itunes:originaldate", "----:com.apple.itunes:originalyear",
    "wm/originalreleasetime", "wm/originalreleaseyear",
)


def _tag_text(value) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    if hasattr(value, "text"):  # ID3 frame
        value = value.text[0] if value.text else ""
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8", "replace")
    return str(value)


def read_tag_years(path: Path) -> tuple[str, str]:
    """(DATE year, original year) from one file, any format; "" when absent."""
    _audio, tags = _read_tag_file(path)
    if not tags:
        return "", ""
    values: dict[str, str] = {}
    try:
        for key in tags.keys():
            values.setdefault(str(key).casefold(), _tag_text(tags[key]))
    except Exception:  # noqa: BLE001 - odd tag types: treat as unknown
        return "", ""

    def first(keys: tuple[str, ...]) -> str:
        for key in keys:
            year = year4(values.get(key, ""))
            if year:
                return year
        return ""

    return first(DATE_KEYS), first(ORIGINAL_KEYS)
