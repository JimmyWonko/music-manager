"""Both discs of a CD1/CD2 rip have a file with the same name ("05 - Blue Umbrella.flac").

Each disc folder's .cue/.m3u lists its own "05 - Blue Umbrella.flac". A cue's FILE entries and an
m3u's lines are relative to the sidecar's own folder, so each file is listed by its own folder's
sidecar only; it used to count both sidecars (discs 1 and 2) and hold both files as
sources_disagree.
"""

from __future__ import annotations

import pytest
from obr_fixtures import make_album, release

from music_manager.discsources import disagreements
from music_manager.fs import collect_albums
from music_manager.match import align_tracks
from music_manager.providers.discogs import flatten_tracklist, release_artists


@pytest.mark.xfail(strict=True, reason="sidecar names are matched without their folder")
def test_same_file_name_on_both_discs_is_listed_by_its_own_discs_sidecars(tmp_path):
    make_album(tmp_path / "Album")
    [album] = collect_albums(tmp_path)
    assert disagreements(album) == []
    rel = release("match")
    triples, _ = align_tracks(album, flatten_tracklist(rel, release_artists(rel)))
    blue = [(p.parent.name, dt.disc, dt.number) for p, dt, _m in triples if dt and "Blue Umbrella" in p.name]
    assert sorted(blue) == [("CD1", 1, 5), ("CD2", 2, 5)]
