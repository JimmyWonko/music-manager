"""coverart engine: sniffing, folder art, embedding per format, journal snapshots of embedded art."""

from __future__ import annotations

import pytest
from coverart_fixtures import jpeg, png

FORMATS = ["mp3", "flac", "ogg", "opus", "m4a"]


def ca():
    from music_manager import coverart

    return coverart


def test_sniff_reads_mime_and_dimensions():
    c = ca()
    p = c.sniff(png(7, 9))
    assert (p.mime, p.width, p.height) == ("image/png", 7, 9)
    j = c.sniff(jpeg(640, 480))
    assert (j.mime, j.width, j.height) == ("image/jpeg", 640, 480)
    assert c.sniff(b"\xff\xd8\xff fake jpeg") is None
    assert c.sniff(b"") is None


def test_folder_art_prefers_cover_then_folder_then_front(tmp_path):
    c = ca()
    assert c.folder_art(tmp_path) is None
    (tmp_path / "FRONT.PNG").write_bytes(png())
    (tmp_path / "back.jpg").write_bytes(jpeg())
    assert c.folder_art(tmp_path) == tmp_path / "FRONT.PNG"
    (tmp_path / "Folder.jpg").write_bytes(jpeg())
    assert c.folder_art(tmp_path) == tmp_path / "Folder.jpg"
    (tmp_path / "cover.jpeg").write_bytes(jpeg())
    assert c.folder_art(tmp_path) == tmp_path / "cover.jpeg"


@pytest.mark.parametrize("ext", FORMATS)
def test_embed_keep_and_replace(sample, ext):
    c = ca()
    f = sample(ext)
    assert c.embedded_front(f) is None
    first, second = c.sniff(jpeg(tag=b"one")), c.sniff(png(3, 3))
    assert c.embed_front(f, first, replace=False) is True
    got = c.embedded_front(f)
    assert got.data == first.data and got.mime == "image/jpeg"
    assert c.embed_front(f, first, replace=True) is False  # same picture: nothing to do
    assert c.embed_front(f, second, replace=False) is False  # keep existing art
    assert c.embedded_front(f).data == first.data
    assert c.embed_front(f, second, replace=True) is True
    assert c.embedded_front(f).data == second.data


@pytest.mark.parametrize("ext", FORMATS)
def test_preview_matches_embed(sample, ext):
    c = ca()
    f = sample(ext)
    img = c.sniff(png(5, 6))
    before, after = c.preview_embed(f, img, replace=False)
    assert before is None and "image/png" in after and "5x6" in after
    c.embed_front(f, img, replace=False)
    assert c.preview_embed(f, c.sniff(jpeg()), replace=False) is None


def test_unsupported_formats_are_left_alone(sample):
    c = ca()
    f = sample("wv")
    assert c.embed_front(f, c.sniff(png()), replace=True) is False
    assert c.preview_embed(f, c.sniff(png()), replace=True) is None


def test_flac_replace_keeps_other_picture_types(sample):
    from mutagen.flac import FLAC, Picture

    c = ca()
    f = sample("flac")
    audio = FLAC(f)
    back = Picture()
    back.type, back.mime, back.data = 4, "image/png", png(1, 1)
    audio.add_picture(back)
    audio.save()
    c.embed_front(f, c.sniff(jpeg()), replace=True)
    types = sorted(p.type for p in FLAC(f).pictures)
    assert types == [3, 4]


@pytest.mark.parametrize("ext", FORMATS)
@pytest.mark.parametrize("had_art", [False, True])
def test_journal_snapshot_restores_original_art_or_absence(sample, ext, had_art):
    from music_manager.tags.snapshot import capture_tags, preview_restore, restore_tags

    c = ca()
    f = sample(ext)
    original = c.sniff(jpeg(tag=b"orig"))
    if had_art:
        c.embed_front(f, original, replace=False)
    snap = capture_tags(f)
    c.embed_front(f, c.sniff(png(8, 8)), replace=True)
    changes = {k: (b, a) for k, b, a in preview_restore(f, snap)}
    assert "cover_art" in changes and "8x8" in changes["cover_art"][0]
    restore_tags(f, snap)
    got = c.embedded_front(f)
    if had_art:
        assert got is not None and got.data == original.data
    else:
        assert got is None


@pytest.mark.parametrize("ext", FORMATS)
def test_write_tags_embeds_cover_in_the_same_atomic_write(sample, meta, ext):
    from music_manager.tags.write import write_tags

    c = ca()
    f = sample(ext)
    img = c.sniff(png(4, 4))
    write_tags(f, meta, cover=c.EmbedCover(img, replace=False))
    assert c.embedded_front(f).data == img.data
    write_tags(f, meta, clear_other=True)  # retagging never drops the art
    assert c.embedded_front(f).data == img.data
