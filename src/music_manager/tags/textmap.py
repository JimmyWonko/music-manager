"""Tag containers as plain text maps, for previews (before/after tag diffs).
"""

from __future__ import annotations

from .ape import APE_FIELDS
from .asf import ASF_FIELDS
from .fields import is_protected_key
from .id3 import ID3_TEXT_FRAMES, ID3_TXXX_FIELDS
from .mp4 import MP4_FIELDS
from .vorbis import VORBIS_FIELDS


def _canonical_names() -> dict[str, str]:
    names: dict[str, str] = {}
    for table in (VORBIS_FIELDS, APE_FIELDS, ASF_FIELDS, MP4_FIELDS):
        for name, (key, aliases) in table.items():
            for spelling in (key, *aliases):
                names.setdefault(spelling.casefold(), name)
    for name, (frame, _cls) in ID3_TEXT_FRAMES.items():
        names.setdefault(frame.casefold(), name)
    names.setdefault("trck", "tracknumber")
    names.setdefault("tpos", "discnumber")
    for name, (desc, aliases) in ID3_TXXX_FIELDS.items():
        for spelling in (desc, *aliases):
            names.setdefault(f"txxx:{spelling}".casefold(), name)
    return names


CANONICAL_NAMES = _canonical_names()


def _value_text(value) -> str:
    if hasattr(value, "text"):  # ID3 frame
        return " / ".join(str(v) for v in value.text)
    if isinstance(value, (list, tuple)) and not (len(value) == 2 and all(isinstance(v, int) for v in value)):
        return " / ".join(_value_text(v) for v in value)
    if isinstance(value, tuple):  # MP4 trkn/disk pair
        number, total = value
        return f"{number}/{total}" if total else str(number)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    if hasattr(value, "value"):  # ASF / APE value objects
        return str(value.value) if not isinstance(value.value, bytes) else value.value.decode("utf-8", "replace")
    return str(value)


def tag_text_map(tags) -> dict[str, str]:
    """{key: text} for every text field in a mutagen tag container (protected/binary fields skipped).

    Keys are canonical field names where known (title, tracknumber, ...),
    else the container's own key.
    """
    out: dict[str, str] = {}
    if tags is None:
        return out
    for key in list(tags.keys()):
        if is_protected_key(key) or key.upper().startswith(("APIC", "COVR", "PRIV", "GEOB", "MCDI", "UFID")):
            continue
        try:
            text = _value_text(tags[key])
        except Exception:  # noqa: BLE001 - an odd frame must not break a preview
            continue
        name = CANONICAL_NAMES.get(key.casefold(), key)
        if name in out and out[name] != text:
            name = key
        out[name] = text
    return out
