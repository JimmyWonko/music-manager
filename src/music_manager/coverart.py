"""Front cover art: sniff images, find folder art, read and embed the front cover per format.

Formats: MP3/WAV/AIFF (ID3 APIC, picture type 3), FLAC (PICTURE metadata
block), Ogg Vorbis/Opus/FLAC (METADATA_BLOCK_PICTURE comment) and MP4/M4A
(covr). Other formats are left alone. Only JPEG and PNG are embedded; images
are never resized (Pillow is not a dependency).

Every function here edits the file it is given in place: callers that write
a real track go through tags.write.write_tags(cover=...), which edits a
temporary copy and swaps it in atomically, after the journal took a snapshot.
"""

from __future__ import annotations

import base64
import contextlib
import struct
from dataclasses import dataclass, field
from pathlib import Path

FRONT = 3  # ID3 / FLAC picture type "Cover (front)"
FOLDER_ART_STEMS = ("cover", "folder", "front")  # preference order
FOLDER_ART_EXTS = (".jpg", ".jpeg", ".png")


@dataclass(frozen=True)
class Image:
    data: bytes = field(repr=False)
    mime: str  # "image/jpeg" or "image/png"
    width: int  # 0 when unknown (an embedded picture we could not parse)
    height: int

    def describe(self) -> str:
        size = len(self.data)
        human = f"{size / 1024:.1f} KB" if size >= 1024 else f"{size} bytes"
        dims = f" {self.width}x{self.height}" if self.width and self.height else ""
        return f"front cover {self.mime}{dims} ({human})"


@dataclass(frozen=True)
class EmbedCover:
    """What write_tags(cover=...) embeds: the image, and whether existing front art is replaced."""
    image: Image
    replace: bool = False


# --- sniffing ---------------------------------------------------------------------------

def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        (length,) = struct.unpack(">H", data[i + 2:i + 4])
        if length < 2:
            return None
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if i + 9 > len(data):
                return None
            height, width = struct.unpack(">HH", data[i + 5:i + 9])
            return (width, height) if width and height else None
        if marker in (0xD9, 0xDA):  # end of image / start of scan before any frame header
            return None
        i += 2 + length
    return None


def sniff(data: bytes | None) -> Image | None:
    """The image's MIME type and size when data is a JPEG or PNG we can read the header of, else None."""
    if not data:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24 and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
        return Image(bytes(data), "image/png", width, height) if width and height else None
    if data.startswith(b"\xff\xd8"):
        size = _jpeg_size(data)
        if size:
            return Image(bytes(data), "image/jpeg", *size)
    return None


def _loose(data: bytes, mime: str) -> Image:
    """An embedded picture as found: sniffed when possible, else kept with its declared type."""
    return sniff(data) or Image(bytes(data), mime or "application/octet-stream", 0, 0)


# --- folder art -------------------------------------------------------------------------

def folder_art(folder: Path) -> Path | None:
    """cover.*, else folder.*, else front.* (jpg/jpeg/png, any case) directly in folder."""
    try:
        files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in FOLDER_ART_EXTS]
    except OSError:
        return None
    for stem in FOLDER_ART_STEMS:
        found = sorted(p for p in files if p.stem.casefold() == stem)
        if found:
            return found[0]
    return None


# --- per format -------------------------------------------------------------------------

def _open(path: Path):
    """(kind, container) or (None, None) for formats we do not embed into."""
    from mutagen import File as MutagenFile
    from mutagen.aiff import AIFF
    from mutagen.flac import FLAC
    from mutagen.id3 import ID3, ID3NoHeaderError
    from mutagen.mp4 import MP4
    from mutagen.oggflac import OggFLAC
    from mutagen.oggopus import OggOpus
    from mutagen.oggvorbis import OggVorbis
    from mutagen.wave import WAVE

    if path.suffix.lower() == ".mp3":
        try:
            return "id3file", ID3(path)
        except ID3NoHeaderError:
            return "id3file", ID3()
    audio = MutagenFile(path)
    if isinstance(audio, (WAVE, AIFF)):
        if audio.tags is None:
            audio.add_tags()
        return "id3audio", audio
    if isinstance(audio, FLAC):
        return "flac", audio
    if isinstance(audio, (OggVorbis, OggOpus, OggFLAC)):
        if audio.tags is None:
            audio.add_tags()
        return "ogg", audio
    if isinstance(audio, MP4):
        if audio.tags is None:
            audio.add_tags()
        return "mp4", audio
    return None, None


def _id3_tags(kind, obj):
    return obj if kind == "id3file" else obj.tags


def _ogg_pictures(tags) -> list:
    from mutagen.flac import Picture

    out = []
    for value in tags.get("metadata_block_picture", []):
        with contextlib.suppress(Exception):  # a broken picture comment is ignored
            out.append(Picture(base64.b64decode(value)))
    return out


def _front(kind, obj) -> Image | None:
    if kind in ("id3file", "id3audio"):
        tags = _id3_tags(kind, obj)
        frames = [f for f in tags.getall("APIC") if f.type == FRONT] if tags is not None else []
        return _loose(frames[0].data, frames[0].mime) if frames else None
    if kind == "flac":
        pics = [p for p in obj.pictures if p.type == FRONT]
        return _loose(pics[0].data, pics[0].mime) if pics else None
    if kind == "ogg":
        return _ogg_front(obj.tags)
    if kind == "mp4":
        return _mp4_front((obj.tags or {}).get("covr") or [])
    return None


def _ogg_front(tags) -> Image | None:
    pics = [p for p in _ogg_pictures(tags) if p.type == FRONT]
    if pics:
        return _loose(pics[0].data, pics[0].mime)
    legacy = tags.get("coverart")
    if legacy:
        try:
            return _loose(base64.b64decode(legacy[0]), (tags.get("coverartmime") or [""])[0])
        except Exception:  # noqa: BLE001 - a broken legacy picture is ignored
            return None
    return None


def _mp4_front(covers) -> Image | None:
    if not covers:
        return None
    from mutagen.mp4 import MP4Cover

    mime = "image/png" if getattr(covers[0], "imageformat", None) == MP4Cover.FORMAT_PNG else "image/jpeg"
    return _loose(bytes(covers[0]), mime)


def _picture(image: Image):
    from mutagen.flac import Picture

    pic = Picture()
    pic.type, pic.mime, pic.desc, pic.data = FRONT, image.mime, "", image.data
    pic.width, pic.height, pic.depth = image.width, image.height, 24
    return pic


def _set_front(kind, obj, image: Image) -> None:
    if kind in ("id3file", "id3audio"):
        from mutagen.id3 import APIC

        tags = _id3_tags(kind, obj)
        keep = [f for f in tags.getall("APIC") if f.type != FRONT]
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime=image.mime, type=FRONT, desc="", data=image.data))
        for f in keep:
            tags.add(f)
    elif kind == "flac":
        keep = [p for p in obj.pictures if p.type != FRONT]
        obj.clear_pictures()
        obj.add_picture(_picture(image))
        for p in keep:
            obj.add_picture(p)
    elif kind == "ogg":
        keep = [p for p in _ogg_pictures(obj.tags) if p.type != FRONT]
        for key in ("metadata_block_picture", "coverart", "coverartmime"):
            if key in obj.tags:
                del obj.tags[key]
        obj.tags["metadata_block_picture"] = [base64.b64encode(p.write()).decode("ascii")
                                              for p in [_picture(image), *keep]]
    elif kind == "mp4":
        from mutagen.mp4 import MP4Cover

        fmt = MP4Cover.FORMAT_PNG if image.mime == "image/png" else MP4Cover.FORMAT_JPEG
        rest = list((obj.tags.get("covr") or [])[1:])
        obj.tags["covr"] = [MP4Cover(image.data, imageformat=fmt), *rest]


def _save(kind, obj, path: Path) -> None:
    if kind == "id3file":
        obj.save(path, v2_version=3 if obj.version[1] == 3 else 4)
    elif kind == "id3audio":
        obj.save(v2_version=3 if obj.tags.version[1] == 3 else 4)
    else:
        obj.save()


def embedded_front(path: Path) -> Image | None:
    """The front cover embedded in path (first one), or None. Unsupported formats: None."""
    kind, obj = _open(path)
    return _front(kind, obj) if kind else None


def _decide(kind, obj, image: Image, replace: bool) -> tuple[Image | None, bool]:
    current = _front(kind, obj)
    if current is not None and (not replace or current.data == image.data):
        return current, False
    return current, True


def preview_embed(path: Path, image: Image, replace: bool) -> tuple[str | None, str] | None:
    """(before, after) descriptions when embed_front would change path, else None. Never writes."""
    kind, obj = _open(path)
    if kind is None:
        return None
    current, change = _decide(kind, obj, image, replace)
    if not change:
        return None
    return (current.describe() if current else None), image.describe()


def embed_front(path: Path, image: Image, replace: bool = False) -> bool:
    """Embed image as path's front cover, in place. False (nothing written) when path keeps its art.

    With replace=False a file that has front art keeps it. Other picture
    types (back cover, booklet ...) are always kept.
    """
    kind, obj = _open(path)
    if kind is None:
        return False
    _current, change = _decide(kind, obj, image, replace)
    if not change:
        return False
    _set_front(kind, obj, image)
    _save(kind, obj, path)
    return True


# --- journal snapshots (FLAC pictures live outside the tag container) ---------------------

def capture_flac_pictures(path: Path) -> list[str] | None:
    """FLAC: every PICTURE block, base64 of its raw bytes; None for other formats."""
    if path.suffix.lower() != ".flac":
        return None
    from mutagen.flac import FLAC

    return [base64.b64encode(p.write()).decode("ascii") for p in FLAC(path).pictures]


def flac_pictures(encoded: list[str]) -> list:
    from mutagen.flac import Picture

    return [Picture(base64.b64decode(e)) for e in encoded]


def snapshot_front(snapshot: dict) -> Image | None:
    """The front cover a journal tag snapshot (tags.snapshot.capture_tags) would restore."""
    from .tags.snapshot import _id3_from_bytes, _snapshot_payload

    pics = snapshot.get("flac_pictures")
    if pics is not None:
        fronts = [p for p in flac_pictures(pics) if p.type == FRONT]
        return _loose(fronts[0].data, fronts[0].mime) if fronts else None
    kind = snapshot.get("kind")
    try:
        if kind == "id3":
            return _front("id3file", _id3_from_bytes(snapshot["id3"]))
        if kind in ("list", "map"):
            pairs = _snapshot_payload(snapshot, trust_legacy=False)
            if kind == "list":
                tags: dict[str, list] = {}
                for k, v in pairs:
                    tags.setdefault(k.casefold(), []).append(v)
                return _ogg_front(tags)
            covers = [v for k, v in pairs if k == "covr"]
            return _mp4_front(covers[0] if covers else [])
    except Exception:  # noqa: BLE001 - an undecodable snapshot shows no art
        return None
    return None
