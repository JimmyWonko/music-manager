"""Bugs 2, 3, 5: per-format tag writing and stale tags, on real tiny files."""

import pytest
from mutagen import File as MutagenFile
from mutagen import MutagenError
from mutagen.apev2 import APEv2
from mutagen.asf import ASF
from mutagen.id3 import APIC, COMM, ID3, TSSE, TXXX, UFID
from mutagen.mp4 import MP4, MP4FreeForm

ALL = ["mp3", "flac", "m4a", "wav", "aiff", "wma", "ogg", "opus", "wv"]
ID3_FORMATS = ["mp3", "wav", "aiff"]
VORBIS_FORMATS = ["flac", "ogg", "opus"]


def text_of(path, key):
    """Read one field back with the format's own field name."""
    audio = MutagenFile(path)
    if path.suffix == ".mp3":
        tags = ID3(path)
    else:
        tags = audio.tags
    value = tags[key]
    if isinstance(value, list):
        value = value[0]
    if isinstance(value, bytes):
        return value.decode()
    if hasattr(value, "text"):
        return str(value.text[0])
    return str(value)


@pytest.mark.parametrize("ext", ALL)
def test_round_trip(dt, sample, meta, ext):
    path = sample(ext)
    dt.write_tags(path, meta)
    assert dt.already_tagged(path) == "123"
    # the file is still valid audio afterwards
    assert MutagenFile(path) is not None


@pytest.mark.parametrize("ext", ID3_FORMATS)
def test_id3_formats_get_id3v24_frames(dt, sample, meta, ext):
    path = sample(ext)
    dt.write_tags(path, meta)
    tags = ID3(path) if ext == "mp3" else MutagenFile(path).tags
    assert isinstance(tags, ID3)
    assert tags.version[:2] == (2, 4)
    assert str(tags["TIT2"]) == "Söng"
    assert str(tags["TRCK"]) == "3/10"
    assert str(tags["TPOS"]) == "1/2"
    assert str(tags["TDRC"]) == "1999-04"  # Discogs "-00" day dropped
    assert str(tags["TDOR"]) == "1981"
    assert str(tags["TPUB"]) == "Label"
    assert str(tags["TXXX:CATALOGNUMBER"]) == "CAT1"
    assert str(tags["TXXX:DISCOGS_RELEASE_ID"]) == "123"
    assert str(tags["TXXX:DISCOGS_MASTER_ID"]) == "45"
    assert str(tags["TXXX:DISCOGS_POSITION"]) == "B1"
    # no Vorbis-style keys anywhere
    assert not [k for k in tags.keys() if k in ("TITLE", "ARTIST", "ALBUM")]


@pytest.mark.parametrize("ext", VORBIS_FORMATS)
def test_vorbis_formats_still_use_vorbis_comments(dt, sample, meta, ext):
    path = sample(ext)
    dt.write_tags(path, meta)
    audio = MutagenFile(path)
    assert not isinstance(audio.tags, ID3)
    for key, want in [("TITLE", "Söng"), ("TRACKNUMBER", "3"), ("TRACKTOTAL", "10"), ("DISCNUMBER", "1"),
                      ("DISCTOTAL", "2"), ("DATE", "1999-04"), ("ORIGINALDATE", "1981"), ("LABEL", "Label"),
                      ("CATALOGNUMBER", "CAT1"), ("DISCOGS_RELEASE_ID", "123"), ("DISCOGS_POSITION", "B1")]:
        assert audio.tags[key] == [want], key
    if ext == "flac":
        with open(path, "rb") as handle:
            assert handle.read(3) != b"ID3"


def test_wma_uses_asf_attribute_names(dt, sample, meta):
    path = sample("wma")
    dt.write_tags(path, meta)
    tags = ASF(path).tags
    want = {
        "Title": "Söng", "Author": "Ärtist", "WM/AlbumArtist": "Ärtist", "WM/AlbumTitle": "Albüm",
        "WM/TrackNumber": "3", "TotalTracks": "10", "WM/PartOfSet": "1/2", "TotalDiscs": "2",
        "WM/Year": "1999-04", "WM/OriginalReleaseTime": "1981", "WM/OriginalReleaseYear": "1981",
        "WM/Publisher": "Label", "WM/CatalogNo": "CAT1", "WM/Genre": "Rock", "DISCOGS_RELEASE_ID": "123",
    }
    for key, value in want.items():
        assert str(tags[key][0]) == value, key
    assert "TITLE" not in tags.keys() and "TRACKNUMBER" not in tags.keys()


def test_mp4_writes_original_date_label_catalog_and_ids(dt, sample, meta):
    path = sample("m4a")
    dt.write_tags(path, meta)
    tags = MP4(path).tags
    ff = "----:com.apple.iTunes:"
    assert tags[ff + "ORIGINALDATE"][0] == b"1981"
    assert tags[ff + "LABEL"][0] == b"Label"
    assert tags[ff + "CATALOGNUMBER"][0] == b"CAT1"
    assert tags[ff + "DISCOGS_RELEASE_ID"][0] == b"123"
    assert tags[ff + "DISCOGS_MASTER_ID"][0] == b"45"
    assert tags["trkn"] == [(3, 10)] and tags["disk"] == [(1, 2)]
    assert tags["\xa9day"] == ["1999-04"]


def test_ape_wavpack(dt, sample, meta):
    path = sample("wv")
    dt.write_tags(path, meta)
    tags = APEv2(path)
    assert str(tags["Track"]) == "3/10" and str(tags["Disc"]) == "1/2"
    assert str(tags["Album Artist"]) == "Ärtist" and str(tags["Year"]) == "1999-04"


# --- stale tags (bug 5) -----------------------------------------------------

def test_flac_stale_aliases_cleared_but_replaygain_kept(dt, sample, meta):
    path = sample("flac")
    audio = MutagenFile(path)
    if audio.tags is None:
        audio.add_tags()
    audio.tags["TOTALTRACKS"] = ["99"]
    audio.tags["ALBUM ARTIST"] = ["Old Artist"]
    audio.tags["ORGANIZATION"] = ["Old Label"]
    audio.tags["DISCOGS_MASTER_ID"] = ["777"]
    audio.tags["REPLAYGAIN_TRACK_GAIN"] = ["-6.00 dB"]
    audio.tags["ACCURATERIPRESULT"] = ["AR ok"]
    audio.tags["ENCODER"] = ["flac 1.4.3"]
    audio.tags["COMMENT"] = ["ripped by me"]
    audio.tags["MUSICBRAINZ_ALBUMID"] = ["abc"]
    audio.save()

    removed = dt.write_tags(path, dict(meta, master_id=0))
    tags = MutagenFile(path).tags
    assert "TOTALTRACKS" not in tags and tags["TRACKTOTAL"] == ["10"]
    assert "ALBUM ARTIST" not in tags and tags["ALBUMARTIST"] == ["Ärtist"]
    assert "ORGANIZATION" not in tags and tags["LABEL"] == ["Label"]
    assert "DISCOGS_MASTER_ID" not in tags  # release has no master: stale id removed
    # kept by default
    for key in ("REPLAYGAIN_TRACK_GAIN", "ACCURATERIPRESULT", "ENCODER", "COMMENT", "MUSICBRAINZ_ALBUMID"):
        assert key in tags, key
    assert {k.upper() for k in removed} >= {"TOTALTRACKS", "ALBUM ARTIST", "ORGANIZATION", "DISCOGS_MASTER_ID"}


def test_clear_other_tags_flag(dt, sample, meta):
    path = sample("flac")
    audio = MutagenFile(path)
    if audio.tags is None:
        audio.add_tags()
    for key, value in {"REPLAYGAIN_ALBUM_GAIN": "-5 dB", "R128_TRACK_GAIN": "0", "ACCURATERIPDISCID": "x",
                       "ENCODER": "flac", "LYRICS": "la", "ISRC": "USXXX", "COMMENT": "old",
                       "MUSICBRAINZ_ALBUMID": "abc", "COMPOSER": "someone"}.items():
        audio.tags[key] = [value]
    audio.save()
    dt.write_tags(path, meta, clear_other=True)
    tags = MutagenFile(path).tags
    for key in ("REPLAYGAIN_ALBUM_GAIN", "R128_TRACK_GAIN", "ACCURATERIPDISCID", "ENCODER", "LYRICS", "ISRC"):
        assert key in tags, key
    for key in ("COMMENT", "MUSICBRAINZ_ALBUMID", "COMPOSER"):
        assert key not in tags, key
    assert tags["TITLE"] == ["Söng"]


def test_mp3_stale_frames_and_clear_other(dt, sample, meta):
    path = sample("mp3")
    tags = ID3()
    tags.add(TXXX(encoding=3, desc="discogs_release_id", text="1"))  # other case
    tags.add(TXXX(encoding=3, desc="CATALOG", text="OLD-1"))
    tags.add(TXXX(encoding=3, desc="REPLAYGAIN_TRACK_GAIN", text="-3 dB"))
    tags.add(TXXX(encoding=3, desc="MusicBrainz Album Id", text="abc"))
    tags.add(COMM(encoding=3, lang="eng", desc="", text="old comment"))
    tags.add(COMM(encoding=3, lang="eng", desc="iTunSMPB", text="gapless"))
    tags.add(TSSE(encoding=3, text="LAME"))
    tags.add(UFID(owner="http://musicbrainz.org", data=b"id"))
    tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="", data=b"\xff\xd8"))
    tags.save(path)

    dt.write_tags(path, meta)
    after = ID3(path)
    assert [str(f) for f in after.getall("TXXX") if f.desc.casefold() == "discogs_release_id"] == ["123"]
    assert "TXXX:CATALOG" not in after and str(after["TXXX:CATALOGNUMBER"]) == "CAT1"
    assert "TXXX:MusicBrainz Album Id" in after and "COMM::eng" in after  # kept by default

    dt.write_tags(path, meta, clear_other=True)
    after = ID3(path)
    assert "TXXX:MusicBrainz Album Id" not in after and "COMM::eng" not in after
    assert "UFID:http://musicbrainz.org" not in after
    for key in ("TXXX:REPLAYGAIN_TRACK_GAIN", "COMM:iTunSMPB:eng", "TSSE", "APIC:"):
        assert key in after, key
    assert str(after["TIT2"]) == "Söng"


def test_mp4_clear_other_keeps_art_and_encoder(dt, sample, meta):
    path = sample("m4a")
    audio = MP4(path)
    if audio.tags is None:
        audio.add_tags()
    audio.tags["\xa9cmt"] = ["old"]
    audio.tags["\xa9too"] = ["Lavf"]
    audio.tags["----:com.apple.iTunes:replaygain_track_gain"] = [MP4FreeForm(b"-1 dB")]
    audio.tags["----:com.apple.iTunes:MusicBrainz Album Id"] = [MP4FreeForm(b"abc")]
    audio.save()
    dt.write_tags(path, meta, clear_other=True)
    tags = MP4(path).tags
    assert "\xa9cmt" not in tags and "----:com.apple.iTunes:MusicBrainz Album Id" not in tags
    assert tags["\xa9gen"] == ["Rock"]
    assert "\xa9too" in tags and "----:com.apple.iTunes:replaygain_track_gain" in tags


def test_retag_to_other_release_drops_release_specific_values(dt, sample, meta):
    path = sample("flac")
    dt.write_tags(path, meta)
    dt.write_tags(path, dict(meta, release_id=999, label="", catalognumber="", master_id=0))
    tags = MutagenFile(path).tags
    assert tags["DISCOGS_RELEASE_ID"] == ["999"]
    assert "LABEL" not in tags and "CATALOGNUMBER" not in tags and "DISCOGS_MASTER_ID" not in tags


def test_same_release_without_label_keeps_existing_label(dt, sample, meta):
    path = sample("flac")
    dt.write_tags(path, meta)
    dt.write_tags(path, dict(meta, label=""))
    assert MutagenFile(path).tags["LABEL"] == ["Label"]


@pytest.mark.parametrize("ext", ALL)
def test_dry_run_does_not_touch_file(dt, sample, meta, ext):
    path = sample(ext)
    before = path.read_bytes()
    dt.write_tags(path, meta, dry_run=True)
    assert path.read_bytes() == before


def test_unsupported_file_raises(dt, tmp_path, meta):
    path = tmp_path / "broken.flac"
    path.write_bytes(b"not audio at all")
    with pytest.raises((RuntimeError, MutagenError)):
        dt.write_tags(path, meta)
