from music_manager.fs import collect_albums, is_artwork_dir

"""Bug 8: artwork folder names only skipped inside an album folder."""


def touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")


def test_real_artist_and_album_named_like_artwork_folders(tmp_path):
    touch(tmp_path / "Art" / "Some Album" / "1. a.flac")          # artist called "Art"
    touch(tmp_path / "Artist" / "Covers" / "1. b.flac")           # album called "Covers"
    touch(tmp_path / "Pics" / "Booklet" / "1. c.flac")            # both
    touch(tmp_path / "Artist" / "Album" / "1. d.flac")
    touch(tmp_path / "Artist" / "Album" / "Scans" / "front.jpg")  # artwork inside album
    touch(tmp_path / "Artist" / "Album" / "Art" / "1. hidden.flac")  # audio in album's Art: still extras
    albums = collect_albums(tmp_path)
    rel = sorted(str(a.path.relative_to(tmp_path)) for a in albums)
    assert rel == ["Art/Some Album", "Artist/Album", "Artist/Covers", "Pics/Booklet"]
    album = next(a for a in albums if a.path.name == "Album")
    assert sorted(d.name for d in album.extra_dirs) == ["Art", "Scans"]


def test_multi_disc_album_artwork(tmp_path):
    touch(tmp_path / "Artist" / "Box" / "CD1" / "1. a.flac")
    touch(tmp_path / "Artist" / "Box" / "CD2" / "1. b.flac")
    touch(tmp_path / "Artist" / "Box" / "Covers" / "1. not-a-track.flac")
    touch(tmp_path / "Artist" / "Box" / "CD1" / "Art" / "x.jpg")
    albums = collect_albums(tmp_path)
    assert [a.path.name for a in albums] == ["Box"]
    assert [t.parent.name for t in albums[0].tracks] == ["CD1", "CD2"]
    assert is_artwork_dir(tmp_path / "Artist" / "Box" / "Covers")
    assert is_artwork_dir(tmp_path / "Artist" / "Box" / "CD1" / "Art")
    assert not is_artwork_dir(tmp_path / "Artist")


def test_root_named_covers_is_processed(tmp_path):
    root = tmp_path / "Covers"
    touch(root / "1. a.flac")
    assert [a.path for a in collect_albums(root)] == [root]
