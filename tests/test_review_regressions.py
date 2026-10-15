"""Regression tests for confirmed points of the external code review (Oct 2026).

Every test here FAILS on main (ac04a6d / tree 740b7d1) and should pass once
the matching fix lands. Everything runs inside tmp_path; no real library.
"""
import base64
import errno
import json
import os
import pickle
from pathlib import Path
import pytest
from conftest import release
from mutagen.flac import FLAC
from test_apply import make_album, run_apply


@pytest.fixture
def exdev(dt, monkeypatch):
    """Every plain rename fails with EXDEV, like a move to another drive."""
    real = os.rename

    def fake(src, dst, *a, **k):
        if ".discogs-tag-partial-" in str(src):
            return real(src, dst, *a, **k)
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(dt.os, "rename", fake)


def _tree(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()) if root.exists() else []


class _Evil:
    def __init__(self, marker):
        self.marker = marker

    def __reduce__(self):
        return (os.mkdir, (self.marker,))


def test_review8_restore_never_unpickles_journal_data(dt, sample, tmp_path):
    path = sample("flac")
    marker = tmp_path / "pwned"
    evil = {"kind": "map", "data": base64.b64encode(pickle.dumps([_Evil(str(marker))])).decode("ascii")}
    try:
        dt.restore_tags(path, evil)
    except Exception:  # noqa: BLE001 - rejecting the snapshot is fine
        pass
    assert not marker.exists(), "journal snapshot data was unpickled (arbitrary code ran)"


def test_review8_snapshot_is_plain_json_not_pickle(dt, sample, meta):
    path = sample("flac")
    dt.write_tags(path, meta)
    snap = dt.capture_tags(path)
    data = snap.get("data")
    if isinstance(data, str):
        try:
            raw = base64.b64decode(data, validate=True)
        except Exception:  # noqa: BLE001
            raw = b""
        assert not raw.startswith(b"\x80"), "snapshot payload is a pickle stream"



FORMATS = ["flac", "mp3", "m4a", "ogg", "opus", "wma", "wv", "aiff", "wav"]


def _dump(path):
    from mutagen import File as MutagenFile
    from mutagen.id3 import ID3
    if path.suffix == ".mp3":
        return ID3(path).pprint()
    return MutagenFile(path).tags.pprint()


@pytest.mark.parametrize("ext", FORMATS)
def test_review8_json_snapshot_keeps_binary_art(dt, sample, meta, ext):
    from mutagen import File as MutagenFile
    from mutagen.apev2 import APEValue, BINARY
    from mutagen.asf import ASFByteArrayAttribute
    from mutagen.flac import Picture
    from mutagen.id3 import APIC, ID3
    from mutagen.mp4 import MP4Cover
    path = sample(ext)
    dt.write_tags(path, meta)
    art = b"\x89PNG\r\n\x1a\n\x00\xff" * 4
    if ext == "mp3":
        tags = ID3(path)
        tags.add(APIC(encoding=0, mime="image/png", type=3, desc="", data=art))
        tags.save(path, v2_version=3)
    else:
        audio = MutagenFile(path)
        if ext in ("aiff", "wav"):
            audio.tags.add(APIC(encoding=0, mime="image/png", type=3, desc="", data=art))
        elif ext == "m4a":
            audio.tags["covr"] = [MP4Cover(art, imageformat=MP4Cover.FORMAT_PNG)]
        elif ext == "wma":
            audio.tags["WM/Picture"] = [ASFByteArrayAttribute(art)]
        elif ext == "wv":
            audio.tags["Cover Art (Front)"] = APEValue(b"cover.png\x00" + art, BINARY)
        elif ext == "flac":
            pic = Picture()
            pic.data, pic.mime, pic.type = art, "image/png", 3
            audio.add_picture(pic)
        else:
            audio.tags["METADATA_BLOCK_PICTURE"] = [base64.b64encode(art).decode()]
        audio.save()
    before, raw = _dump(path), path.read_bytes()
    snap = json.loads(json.dumps(dt.capture_tags(path)))
    assert "data" not in snap and snap.get("v") == 2
    dt.write_tags(path, {**meta, "title": "Changed"}, clear_other=True)
    if ext == "m4a":
        MutagenFile(path).delete()
    dt.restore_tags(path, snap)
    assert _dump(path) == before
    if ext == "mp3":
        assert ID3(path).version[:2] == (2, 3)  # v2.3 file stays v2.3
        assert path.read_bytes() == raw


def test_review8_legacy_pickle_journal_needs_explicit_trust(dt, sample, meta, tmp_path):
    path = sample("flac")
    dt.write_tags(path, {**meta, "title": "Old"})
    from mutagen.flac import FLAC
    legacy = {"kind": "list", "data": base64.b64encode(pickle.dumps(list(list.__iter__(FLAC(path).tags)))).decode()}
    with pytest.raises(dt.LegacySnapshotError):
        dt.restore_tags(path, legacy)
    dt.write_tags(path, {**meta, "title": "New"})
    sha = dt.file_sha256(path)
    journal = tmp_path / "old.jsonl"
    journal.write_text(
        json.dumps({"action": "tags", "phase": "before", "path": str(path), "snapshot": legacy}) + "\n"
        + json.dumps({"action": "tags", "phase": "after", "path": str(path), "sha256": sha}) + "\n")
    assert dt.undo_journal(journal, apply=True) == 1  # skipped, not loaded
    assert FLAC(path)["TITLE"] == ["New"]
    assert dt.undo_journal(journal, apply=True, trust_legacy=True) == 0
    assert FLAC(path)["TITLE"] == ["Old"]
