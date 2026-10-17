"""Tag snapshots for the journal (JSON, format v2) and restoring them on undo.
"""

from __future__ import annotations

import base64
import io
import pickle
from pathlib import Path
from typing import Any, TypedDict

from mutagen import File as MutagenFile
from mutagen.id3 import ID3, ID3NoHeaderError
from mutagen.mp4 import MP4FreeForm

from .write import _atomic_tag_update


class EncodedValue(TypedDict, total=False):
    """One JSON-encoded tag value inside a TagSnapshot ("t" says which kind)."""
    t: str
    v: Any
    fmt: int
    ver: int
    k: int
    lang: str | None
    stream: int | None


class TagSnapshot(TypedDict, total=False):
    """capture_tags() output, stored in the journal. v1 (pickle) snapshots have "data" and no "v"."""
    kind: str
    v: int
    version: list[int]
    id3: str
    pairs: list[list[Any]]
    vendor: str
    data: str


def _tag_container(path: Path):
    """(audio or None, tags or None) opened the same way write_tags opens them."""
    if path.suffix.lower() == ".mp3":
        try:
            return None, ID3(path)
        except ID3NoHeaderError:
            return None, None
    audio = MutagenFile(path)
    if audio is None:
        raise OSError(f"unrecognised audio file: {path}")
    return audio, audio.tags


SNAPSHOT_VERSION = 2


class LegacySnapshotError(ValueError):
    """A journal tag snapshot from before v2 (pickle); only restored on explicit trust."""


def _b64(data: bytes) -> str:
    return base64.b64encode(bytes(data)).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


def _encode_value(value) -> EncodedValue:
    """One tag value as plain JSON, keeping its exact type. Raises TypeError for unknown types."""
    from mutagen.apev2 import APEBinaryValue, APEExtValue, APETextValue
    from mutagen.asf import ASFBaseAttribute
    from mutagen.mp4 import MP4Cover

    if isinstance(value, MP4Cover):
        return {"t": "mp4cover", "v": _b64(value), "fmt": int(value.imageformat)}
    if isinstance(value, MP4FreeForm):
        return {"t": "mp4ff", "v": _b64(value), "fmt": int(value.dataformat), "ver": int(value.version)}
    if isinstance(value, ASFBaseAttribute):
        out: EncodedValue = {"t": "asf", "k": int(value.TYPE), "v": _encode_value(value.value)}
        if getattr(value, "language", None) is not None:
            out["lang"] = value.language
        if getattr(value, "stream", None) is not None:
            out["stream"] = value.stream
        return out
    if isinstance(value, (APETextValue, APEBinaryValue, APEExtValue)):
        return {"t": "ape", "k": int(value.kind), "v": _encode_value(value.value)}
    if isinstance(value, bool):
        return {"t": "bool", "v": value}
    if isinstance(value, int):
        return {"t": "int", "v": int(value)}
    if isinstance(value, str):
        return {"t": "str", "v": str(value)}
    if isinstance(value, (bytes, bytearray)):
        return {"t": "bytes", "v": _b64(value)}
    if isinstance(value, tuple):
        return {"t": "tuple", "v": [_encode_value(v) for v in value]}
    if isinstance(value, list):
        return {"t": "list", "v": [_encode_value(v) for v in value]}
    raise TypeError(f"cannot snapshot tag value of type {type(value).__name__}")


def _decode_value(obj: EncodedValue):
    """Inverse of _encode_value. Only builds the known, inert value types."""
    from mutagen.apev2 import APEValue
    from mutagen.asf import ASFValue
    from mutagen.mp4 import MP4Cover

    if not isinstance(obj, dict):
        raise ValueError("bad snapshot value")
    t, v = obj.get("t"), obj.get("v")
    if t == "str" and isinstance(v, str):
        return v
    if t == "bool" and isinstance(v, bool):
        return v
    if t == "int" and isinstance(v, int) and not isinstance(v, bool):
        return v
    if t == "bytes" and isinstance(v, str):
        return _unb64(v)
    if t == "tuple" and isinstance(v, list):
        return tuple(_decode_value(x) for x in v)
    if t == "list" and isinstance(v, list):
        return [_decode_value(x) for x in v]
    if t == "mp4cover" and isinstance(v, str):
        return MP4Cover(_unb64(v), imageformat=int(obj.get("fmt", MP4Cover.FORMAT_JPEG)))
    if t == "mp4ff" and isinstance(v, str):
        return MP4FreeForm(_unb64(v), dataformat=int(obj.get("fmt", 1)), version=int(obj.get("ver", 0)))
    if t == "asf":
        kwargs = {k: obj[k] for k in ("lang", "stream") if obj.get(k) is not None}
        if "lang" in kwargs:
            kwargs["language"] = kwargs.pop("lang")
        return ASFValue(_decode_value(v), int(obj["k"]), **kwargs)
    if t == "ape":
        return APEValue(_decode_value(v), int(obj["k"]))
    raise ValueError(f"unknown snapshot value type {t!r}")


def _id3_to_bytes(tags: ID3) -> str:
    """A complete ID3v2.4 tag (header + frames) as base64; mutagen re-parses it on restore.

    Saved as v2.4 because mutagen holds frames upgraded to v2.4 in memory;
    the file's own version is stored separately and used when restoring.
    """
    copy = ID3()
    for frame in tags.values():
        copy.add(frame)
    buf = io.BytesIO()
    copy.save(buf, v2_version=4, padding=lambda info: 0)
    return _b64(buf.getvalue())


def _id3_from_bytes(data: str) -> ID3:
    raw = _unb64(data)
    if not raw.startswith(b"ID3"):
        raise ValueError("bad ID3 snapshot")
    return ID3(io.BytesIO(raw))


def capture_tags(path: Path) -> TagSnapshot:
    """Snapshot of every tag in path, enough for restore_tags to put them all back.

    Plain JSON (snapshot format v2), never pickle. ID3 keeps its whole tag as
    raw bytes (re-parsed by mutagen on restore) and its version (2.3/2.4);
    other formats keep their key/value pairs with typed values, binary data
    (cover art, MP4 free-form atoms, APE/ASF binary fields) base64-encoded.
    Audio data is never copied.
    """
    _audio, tags = _tag_container(path)
    if tags is None:
        return {"kind": "none"}
    if isinstance(tags, ID3):
        return {"kind": "id3", "v": SNAPSHOT_VERSION, "version": list(tags.version), "id3": _id3_to_bytes(tags)}
    if isinstance(tags, list):
        # Vorbis comments and ASF are ordered lists of pairs; keep order and case.
        pairs = [[k, _encode_value(v)] for k, v in list.__iter__(tags)]
        snap: TagSnapshot = {"kind": "list", "v": SNAPSHOT_VERSION, "pairs": pairs}
        if hasattr(tags, "vendor"):
            snap["vendor"] = tags.vendor
        return snap
    pairs = [[k, _encode_value(v)] for k, v in tags.items()]
    return {"kind": "map", "v": SNAPSHOT_VERSION, "pairs": pairs}


def _snapshot_payload(snapshot: TagSnapshot, trust_legacy: bool):
    """The snapshot's frames / pairs, decoded. Old pickle snapshots only with trust_legacy."""
    kind = snapshot.get("kind")
    if kind == "none":
        return []
    version = snapshot.get("v")
    if version is None and "data" in snapshot:
        if not trust_legacy:
            raise LegacySnapshotError(
                "old journal snapshot (pickle, v1): refusing to load it because a crafted journal could run code; "
                "re-run with --trust-legacy-journal only if you are sure this journal was written by discogs-tag")
        return pickle.loads(base64.b64decode(snapshot["data"]))  # noqa: S301 - explicitly trusted legacy journal
    if version != SNAPSHOT_VERSION:
        raise ValueError(f"unsupported tag snapshot version {version!r}")
    if kind == "id3":
        return list(_id3_from_bytes(snapshot["id3"]).values())
    if kind in ("list", "map"):
        pairs = snapshot.get("pairs")
        if not isinstance(pairs, list):
            raise ValueError("bad tag snapshot")
        out = []
        for pair in pairs:
            if not (isinstance(pair, list) and len(pair) == 2 and isinstance(pair[0], str)):
                raise ValueError("bad tag snapshot pair")
            out.append((pair[0], _decode_value(pair[1])))
        return out
    raise ValueError(f"unknown tag snapshot kind {kind!r}")


def restore_tags(path: Path, snapshot: TagSnapshot, trust_legacy: bool = False) -> None:
    """Replace every tag in path with a capture_tags() snapshot.

    The snapshot is fully decoded and validated before the file is touched.
    """
    payload = _snapshot_payload(snapshot, trust_legacy)  # validated before the file is touched
    _atomic_tag_update(path, lambda tmp: _restore_tags_file(tmp, snapshot, payload))


def _restore_tags_file(path: Path, snapshot: TagSnapshot, payload: list) -> None:
    kind = snapshot.get("kind")
    if path.suffix.lower() == ".mp3":
        if kind == "none":
            try:
                ID3(path).delete(path)
            except ID3NoHeaderError:
                pass
            return
        tags = ID3()
        for frame in payload:
            tags.add(frame)
        version = (snapshot.get("version") or [2, 4, 0])[1]
        tags.save(path, v2_version=3 if version == 3 else 4)
        return
    audio = MutagenFile(path)
    if audio is None:
        raise OSError(f"unrecognised audio file: {path}")
    if kind == "none":
        if audio.tags is not None:
            audio.delete()
        return
    if audio.tags is None:
        audio.add_tags()
    if kind == "id3":
        audio.tags.clear()
        for frame in payload:
            audio.tags.add(frame)
        version = (snapshot.get("version") or [2, 4, 0])[1]
        audio.save(v2_version=3 if version == 3 else 4)
        return
    if kind == "list" and isinstance(audio.tags, list):
        list.__delitem__(audio.tags, slice(None))
        list.extend(audio.tags, payload)
        if "vendor" in snapshot and hasattr(audio.tags, "vendor"):
            audio.tags.vendor = snapshot["vendor"]
        audio.save()
        return
    for key in list(audio.tags.keys()):
        del audio.tags[key]
    for key, value in payload:
        audio.tags[key] = value
    audio.save()
