"""A layout_ambiguous "album" made of loose files in an artist folder (layout_reason
"album_folders") is named after that folder, as loose tracks: artist = the folder's name,
title = "", AlbumRow.loose_tracks = True. Not after the grandparent ("lib – Black Flag")."""

from __future__ import annotations

import pytest
from disc_fixtures import silent_flac
from mutagen.flac import FLAC
from test_review_walk import (
    _held_problem,
    _rows,
    artist_with_stray,
    svc,  # noqa: F401  (fixture)
)

from music_manager import service
from music_manager.service._state import state

PENDING = pytest.mark.xfail(strict=True, reason="loose tracks are named after the grandparent folder")


def _tag(path, **tags):
    f = FLAC(path)
    for k, v in tags.items():
        f[k] = v
    f.save()


@PENDING
def test_loose_tracks_in_an_artist_folder_are_named_after_it(svc):  # noqa: F811
    artist = svc / "Black Flag"
    real = artist_with_stray(artist)
    rows = _rows()
    loose = rows[artist]
    assert (loose.artist, loose.title, loose.year, loose.catno) == ("Black Flag", "", "", "")
    assert loose.artist_sort == "Black Flag" and loose.track_count == 1
    assert loose.loose_tracks is True
    assert rows[real].loose_tracks is False and rows[real].title != ""
    full = service.library.album(loose.id)
    assert (full.artist, full.title, full.loose_tracks) == ("Black Flag", "", True)
    plan, [problem] = _held_problem(loose.id)
    assert problem.layout_reason == "album_folders"
    assert (plan.albums[0].artist, plan.albums[0].title) == ("Black Flag", "")


@PENDING
def test_stray_tags_do_not_name_the_group(svc):  # noqa: F811
    artist = svc / "Black Flag"
    artist_with_stray(artist)
    silent_flac(artist / "another stray.flac", 98)
    _tag(artist / "another stray.flac", albumartist="Henry Rollins", album="Hot Animal Machine", date="1987",
         albumartistsort="Rollins, Henry")
    _tag(artist / "stray.flac", albumartist="Henry Rollins", album="Hot Animal Machine", date="1987",
         albumartistsort="Rollins, Henry")
    loose = _rows()[artist]
    assert (loose.artist, loose.title, loose.year, loose.artist_sort) == ("Black Flag", "", "", "Black Flag")
    assert loose.track_count == 2 and loose.loose_tracks is True


@PENDING
def test_an_older_index_is_renamed_on_the_next_scan(svc):  # noqa: F811
    artist = svc / "Black Flag"
    artist_with_stray(artist)
    aid = _rows()[artist].id
    with state().index.tx() as conn:  # what scans before this change stored
        conn.execute("UPDATE albums SET artist='lib', title='Black Flag', guess='{}' WHERE id=?", (aid,))
    loose = _rows()[artist]  # nothing on disk changed
    assert (loose.artist, loose.title, loose.loose_tracks) == ("Black Flag", "", True)
