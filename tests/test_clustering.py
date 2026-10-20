"""Picard-style clustering of loose files and the album-match view (service, backend side).

Clusters are computed from tags only and never touch the disk; an adopted cluster is an
index-only album that goes through match / plan / apply like any other, with every hold.
"""

from __future__ import annotations

import copy
import json

import pytest
from disc_fixtures import silent_flac
from mutagen.flac import FLAC
from service_fixtures import FIXTURES, FakeDiscogs, search_result, tree

from music_manager import service
from music_manager.service import JobState

HAM_ID = 7001
HAM = [("Black And White", 181), ("Followed Around", 202), ("Lost And Found", 213), ("There's A Man Outside", 244)]


def ham_release() -> dict:
    rel = copy.deepcopy(json.loads((FIXTURES / "release_multi_cd.json").read_text(encoding="utf-8")))
    rel.update(id=HAM_ID, master_id=0, title="Hot Animal Machine", artists=[{"name": "Henry Rollins"}])
    rel["tracklist"] = [{"position": str(i), "type_": "track", "title": t, "duration": f"{s // 60}:{s % 60:02d}"}
                        for i, (t, s) in enumerate(HAM, 1)]
    return rel


def _tagged(path, seconds, **tags):
    silent_flac(path, seconds)
    f = FLAC(path)
    for k, v in tags.items():
        f[k] = v
    f.save()
    return path


@pytest.fixture
def lib(tmp_path):
    discogs = FakeDiscogs()
    rel = ham_release()
    discogs.releases[HAM_ID] = rel
    discogs.searches["hot animal machine"] = [search_result(rel)]
    root = tmp_path / "lib"
    artist = root / "Henry Rollins"
    files = [
        _tagged(artist / "01 Black And White.flac", 181, albumartist="Henry Rollins", album="Hot Animal Machine",
                title="Black And White", discnumber="1"),
        _tagged(artist / "02 Followed Around.flac", 202, artist="Henry Rollins", album="Hot Animal Machine",
                title="Followed Around"),
        _tagged(artist / "03 Lost And Found.flac", 213, albumartist="HENRY ROLLINS", album="Hot  Animal Machine!",
                title="Lost And Found"),
        _tagged(artist / "zz other.flac", 99, artist="Rollins Band", album="Life Time", title="Burned Beyond"),
    ]
    silent_flac(artist / "[1987]  Big Ugly Mouth" / "01 Intro.flac", 100)
    service.configure(data_dir=tmp_path / "data", config_dir=tmp_path / "config", discogs=discogs)
    service.settings.set(library_roots=[root])
    assert service.jobs.wait(service.library.scan().id).state is JobState.DONE
    yield root, artist, files
    service.reset()


def _loose():
    [row] = [r for r in service.library.albums(service.AlbumFilter(), service.AlbumSort.FOLDER, 0, 100).items
             if r.loose_tracks]
    return row


def test_loose_files_cluster_by_album_tags_and_nothing_moves(lib):
    root, artist, files = lib
    before = tree(root)
    clusters = service.library.clusters()
    assert [(c.artist, c.title, [f.path for f in c.files]) for c in clusters] == [
        ("Henry Rollins", "Hot Animal Machine", files[:3]),
        ("Rollins Band", "Life Time", files[3:]),
    ]
    ham = clusters[0]
    assert ham.folder == artist and ham.disc_count == 1 and ham.album_id is None
    assert [(f.disc, f.title, f.album) for f in ham.files][2] == (1, "Lost And Found", "Hot  Animal Machine!")
    assert service.library.clusters() == clusters  # stable ids
    assert tree(root) == before


def test_clusters_of_chosen_paths(lib, tmp_path):
    root, artist, files = lib
    [c] = service.library.clusters(paths=[files[1], files[0]])
    assert [f.path for f in c.files] == files[:2] and c.id != service.library.clusters()[0].id
    with pytest.raises(service.ValidationError):
        service.library.clusters(paths=[tmp_path / "elsewhere.flac"])
    with pytest.raises(service.ValidationError):
        service.library.clusters(paths=[artist])  # a folder, not an audio file


def test_view_of_a_cluster_not_yet_adopted(lib):
    _root, _artist, files = lib
    ham = service.library.clusters()[0]
    view = service.match.view(ham.id)
    assert view.album_id is None and view.cluster_id == ham.id and view.release_id is None
    assert view.candidates == [] and view.pairs == [] and view.unmatched_tracks == []
    assert [t.path for t in view.unmatched_files] == files[:3]
    with pytest.raises(service.NotFound):
        service.match.view("cl-nope")


def test_adopted_cluster_matches_plans_applies_and_undoes(lib):
    root, artist, files = lib
    before = tree(root)
    ham = service.library.clusters()[0]
    row = service.library.adopt_cluster(ham.id)
    assert row.cluster_id == ham.id and row.folder == artist and row.track_count == 3
    assert (row.artist, row.title, row.status) == ("Henry Rollins", "Hot Animal Machine", service.MatchStatus.UNMATCHED)
    assert service.library.adopt_cluster(ham.id).id == row.id  # idempotent
    assert tree(root) == before
    assert service.jobs.wait(service.library.scan().id).state is JobState.DONE  # rescan keeps the split
    loose = _loose()
    assert loose.track_count == 1 and service.library.album(row.id).track_count == 3
    assert service.library.clusters()[0].album_id == row.id

    assert service.jobs.wait(service.match.find([row.id]).id).state is JobState.DONE
    view = service.match.view(row.id)
    assert view.album_id == row.id and view.cluster_id == ham.id and view.release_id == HAM_ID and view.fetched
    assert [c.release_id for c in view.candidates] == [HAM_ID]
    assert [(p.local.path, p.remote.title, p.ok) for p in view.pairs if p.local and p.remote] == [
        (files[0], "Black And White", True), (files[1], "Followed Around", True), (files[2], "Lost And Found", True)]
    assert view.unmatched_files == [] and [t.title for t in view.unmatched_tracks] == ["There's A Man Outside"]
    assert [p.kind for p in view.problems] == ["track_mismatch"]
    assert service.match.view(ham.id) == view  # by cluster id once adopted
    assert tree(root) == before

    service.match.choose(row.id, HAM_ID)
    plan = service.plan.build([row.id], allow_track_mismatch={row.id})
    [ap] = plan.albums
    assert ap.problems == [] and sorted(c.path_before for c in ap.files) == files[:3]
    assert service.jobs.wait(service.plan.apply(plan.id).id).state is JobState.DONE
    assert not any(f.exists() for f in files[:3]) and files[3].exists()
    assert (artist / "[1987]  Big Ugly Mouth" / "01 Intro.flac").exists()
    assert len([p for p in root.rglob("*.flac") if p.parent == ap.folder_after]) == 3

    [op] = [o for o in service.journal.list().items if o.kind == "apply"]
    undo = service.journal.undo_plan(op.id)
    assert service.jobs.wait(service.journal.undo(op.id, undo.id).id).state is JobState.DONE
    assert all(f.exists() for f in files)
    assert service.jobs.wait(service.library.scan().id).state is JobState.DONE
    assert _loose().track_count == 4


def test_cluster_holds_still_apply(lib):
    _root, _artist, files = lib
    row = service.library.adopt_cluster(service.library.clusters()[0].id)
    assert service.jobs.wait(service.match.find([row.id]).id).state is JobState.DONE
    service.match.choose(row.id, HAM_ID)
    plan = service.plan.build([row.id])  # 3 files, 4 tracks: held
    assert [p.kind for p in plan.albums[0].problems] == ["track_mismatch"]
    with pytest.raises(service.MMError):
        service.plan.apply(plan.id)
    assert all(f.exists() for f in files)
