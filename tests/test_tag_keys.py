"""Tag diff keys (plan and undo previews) are always lowercase."""

from __future__ import annotations

import shutil
from pathlib import Path

from mutagen.flac import FLAC
from mutagen.id3 import ID3, TPE1, TXXX

from music_manager.tags.textmap import tag_text_map
from music_manager.tags.write import preview_tags

SAMPLES = Path(__file__).resolve().parent / "samples"


def meta():
    from music_manager.tags.fields import TrackMeta

    return TrackMeta(title="T", artist="A", albumartist="A", album="B", track=1, tracktotal=1, disc=1, disctotal=1,
                     date="2000", originaldate="", label="", catalognumber="", genre="", release_id=1, master_id=0)


def test_unknown_and_known_keys_are_lowercase(tmp_path):
    flac = tmp_path / "a.flac"
    shutil.copyfile(SAMPLES / "silent.flac", flac)
    f = FLAC(flac)
    f["TITLE"], f["MyCustomField"], f["REPLAYGAIN_TRACK_GAIN"] = "Old", "x", "-1 dB"
    f.save()
    keys = set(tag_text_map(FLAC(flac).tags))
    assert keys and all(k == k.lower() for k in keys) and "title" in keys and "mycustomfield" in keys

    mp3 = tmp_path / "a.mp3"
    shutil.copyfile(SAMPLES / "silent.mp3", mp3)
    tags = ID3()
    tags.add(TXXX(encoding=3, desc="My Desc", text=["v"]))
    tags.add(TPE1(encoding=3, text=["Someone"]))
    tags.save(mp3)
    keys = set(tag_text_map(ID3(mp3)))
    assert keys == {"txxx:my desc", "artist"}

    for path in (flac, mp3):
        changes = preview_tags(path, meta(), clear_other=True)
        assert changes and all(k == k.lower() for k, _b, _a in changes), changes
