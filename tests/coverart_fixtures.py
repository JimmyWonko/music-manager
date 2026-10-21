"""Tiny synthetic cover images for the cover-art tests (no real artwork)."""

from __future__ import annotations

import struct
import zlib


def png(width: int = 2, height: int = 3, shade: int = 0) -> bytes:
    """A valid RGB PNG, every pixel the same grey."""
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    row = b"\x00" + bytes([shade, shade, shade]) * width
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(row * height)) + chunk(b"IEND", b""))


def jpeg(width: int = 4, height: int = 5, tag: bytes = b"") -> bytes:
    """A JPEG header (SOI, APP0, SOF0, EOI) that sniffers read dimensions from; tag makes it unique."""
    app0 = b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + tag
    sof = struct.pack(">BHHB", 8, height, width, 3) + b"\x01\x11\x00\x02\x11\x00\x03\x11\x00"
    return (b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", len(app0) + 2) + app0
            + b"\xff\xc0" + struct.pack(">H", len(sof) + 2) + sof + b"\xff\xd9")
