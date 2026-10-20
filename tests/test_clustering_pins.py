"""Paged cluster listing, moving files between clusters, un-adopting, and manual pins (drag-and-drop)."""

from __future__ import annotations

import pytest
from service_fixtures import direct
from test_clustering import HAM_ID, _loose, _tagged, lib  # noqa: F401  (fixture)

from music_manager import service
from music_manager.service import JobState


def _record(signal):
    """Slots run on the emitting thread (the job worker too), so jobs.wait() returns after delivery."""
    got = []
    direct(signal, lambda *a: got.append(a if len(a) != 1 else a[0]))
    return got


def _scan():
    assert service.jobs.wait(service.library.scan().id).state is JobState.DONE


def test_cluster_rows_page_folder_albums_and_clusters(lib):  # noqa: F811
    _root, artist, _files = lib
    page = service.match.clusters()
    assert page.total == 3 and page.offset == 0
    kinds = [(r.kind, r.title, r.file_count, r.album_id is not None) for r in page.items]
    assert kinds == [("folder", "Big Ugly Mouth", 1, True), ("tag_cluster", "Hot Animal Machine", 3, False),
                     ("tag_cluster", "Life Time", 1, False)]
    folder = page.items[0]
    assert folder.id == f"album-{folder.album_id}" and folder.status is service.MatchStatus.UNMATCHED
    assert page.items[1].loose_tracks and page.items[1].folder == artist and page.items[1].status is None
    assert [r.title for r in service.match.clusters(offset=1, limit=1).items] == ["Hot Animal Machine"]
    assert [r.title for r in service.match.clusters(service.AlbumFilter(text="life")).items] == ["Life Time"]
    unmatched = service.AlbumFilter(status={service.MatchStatus.UNMATCHED})
    assert service.match.clusters(unmatched).total == 1  # clusters not adopted have no status
    assert service.match.view(folder.id).album_id == folder.album_id
    assert service.library.clusters() == service.library.clusters()  # existing call unchanged


def test_cluster_scan_job_reports_progress_and_clusters_changed(lib):  # noqa: F811
    changed = _record(service.events.clusters_changed)
    job = service.library.scan_clusters()
    assert job.kind is service.JobKind.CLUSTER
    done = service.jobs.wait(job.id)
    assert done.state is JobState.DONE and "2 cluster" in done.message
    assert changed and set(changed[-1]) == {c.id for c in service.library.clusters()}


def test_move_files_between_clusters_and_back(lib):  # noqa: F811
    _root, artist, files = lib
    ham, life = service.library.clusters()
    moves = _record(service.events.cluster_changed)
    new = service.library.move_to_cluster(files[3], ham.id)
    assert [f.path for f in new.files] == files and new.id != ham.id and new.title == "Hot Animal Machine"
    assert moves == [(ham.id, new.id), (life.id, "")]
    _scan()
    assert [len(c.files) for c in service.library.clusters()] == [4]  # survives a rescan
    out = service.library.move_to_cluster(files[3], None)
    assert out.kind == "unclustered" and [f.path for f in out.files] == [files[3]]
    assert [(c.kind, len(c.files)) for c in service.library.clusters()] == [("tag_cluster", 3), ("unclustered", 1)]
    with pytest.raises(service.ValidationError):
        service.library.adopt_cluster(out.id)
    with pytest.raises(service.ValidationError):
        service.library.move_to_cluster(artist / "[1987]  Big Ugly Mouth" / "01 Intro.flac", None)

    row = service.library.adopt_cluster(service.library.clusters()[0].id)
    grown = service.library.move_to_cluster(files[3], row.cluster_id)  # into an adopted cluster
    assert grown.album_id == row.id and len(grown.files) == 4
    assert service.library.album(row.id).track_count == 4 and service.library.album(row.id).cluster_id == grown.id
    assert _loose().track_count == 0


@pytest.mark.xfail(strict=True, reason="album_removed not added yet")
def test_unadopt_restores_the_loose_row_and_refuses_after_apply(lib):  # noqa: F811
    ham = service.library.clusters()[0]
    row = service.library.adopt_cluster(ham.id)
    changed = _record(service.events.clusters_changed)
    removed, touched = _record(service.events.album_removed), _record(service.events.album_changed)
    back = service.library.unadopt_cluster(row.id)
    assert back.id == ham.id and back.album_id is None and changed == [[ham.id]]
    assert removed == [row.id] and row.id not in touched and _loose().id in touched
    assert _loose().track_count == 4
    with pytest.raises(service.NotFound):
        service.library.album(row.id)
    with pytest.raises(service.ValidationError):
        service.library.unadopt_cluster(_loose().id)  # not a cluster
    row = service.library.adopt_cluster(ham.id)
    assert service.jobs.wait(service.match.find([row.id]).id).state is JobState.DONE
    service.match.choose(row.id, HAM_ID)
    plan = service.plan.build([row.id], allow_track_mismatch={row.id})
    assert service.jobs.wait(service.plan.apply(plan.id).id).state is JobState.DONE
    with pytest.raises(service.MMError, match="applied"):
        service.library.unadopt_cluster(row.id)


def _adopted_with_mystery(lib):  # noqa: F811
    _root, artist, files = lib
    extra = [_tagged(artist / "04 Mystery.flac", 244, albumartist="Henry Rollins", album="Hot Animal Machine",
                     title="Mystery"),
             _tagged(artist / "05 Bonus.flac", 120, albumartist="Henry Rollins", album="Hot Animal Machine",
                     title="Bonus")]
    _scan()
    row = service.library.adopt_cluster(service.library.clusters()[0].id)
    assert service.jobs.wait(service.match.find([row.id]).id).state is JobState.DONE
    service.match.choose(row.id, HAM_ID)
    return row, files[:3] + extra


def test_pin_reassigns_a_file_and_flows_into_plan_journal_and_history(lib):  # noqa: F811
    _root, _artist, files = lib
    row = service.library.adopt_cluster(service.library.clusters()[0].id)
    assert service.jobs.wait(service.match.find([row.id]).id).state is JobState.DONE
    seen = _record(service.events.match_view_changed)
    pin = service.match.pin(row.id, files[2], "4")
    assert (pin.path, pin.position, pin.release_id, pin.override_hold, pin.stale) == (files[2], "4", HAM_ID, False,
                                                                                         False)
    assert seen == [row.id] and service.match.pins(row.id) == [pin]
    view = service.match.view(row.id)
    [manual] = [p for p in view.pairs if p.method == "manual"]
    assert manual.local.path == files[2] and manual.remote.title == "There's A Man Outside"
    assert [t.title for t in view.unmatched_tracks] == ["Lost And Found"] and view.pins == [pin]
    assert {p.method for p in view.pairs if p.local and p.local.path != files[2]} == {"number"}  # others unshifted
    with pytest.raises(service.ValidationError):
        service.match.pin(row.id, files[3], "1")  # not a file of this album

    service.match.choose(row.id, HAM_ID)
    plan = service.plan.build([row.id], allow_track_mismatch={row.id})
    [ap] = plan.albums
    assert ap.problems == [] and [c.path_before for c in ap.files if c.pinned] == [files[2]]
    assert service.jobs.wait(service.plan.apply(plan.id).id).state is JobState.DONE
    [op] = [o for o in service.journal.list().items if o.kind == "apply"]
    assert service.journal.manual_pairs(op.id) == [service.ManualPair(row.id, files[2], "4", False)]
    assert service.match.pins(row.id) == []  # used up by the apply
    undo = service.journal.undo_plan(op.id)
    assert service.jobs.wait(service.journal.undo(op.id, undo.id).id).state is JobState.DONE
    assert all(f.exists() for f in files)


@pytest.mark.xfail(strict=True, reason="PinConflict not added yet")
def test_bad_pins_are_problems_not_errors(lib):  # noqa: F811
    _root, _artist, files = lib
    row = service.library.adopt_cluster(service.library.clusters()[0].id)
    assert service.jobs.wait(service.match.find([row.id]).id).state is JobState.DONE
    service.match.pin(row.id, files[0], "2")
    service.match.pin(row.id, files[1], "2")
    service.match.pin(row.id, files[2], "9")
    problems = service.match.view(row.id).problems
    kinds = [p.kind for p in problems]
    assert kinds.count("pin_conflict") == 3  # one per file: two on track 2, one on no track
    causes = sorted((c.path, c.position, c.reason) for p in problems for c in p.pin_conflicts)
    assert causes == [(files[0], "2", "duplicate_position"), (files[1], "2", "duplicate_position"),
                      (files[2], "9", "unknown_position")]
    assert all(p.pin_conflicts == [] for p in problems if p.kind != "pin_conflict") and "track_mismatch" in kinds
    service.match.choose(row.id, HAM_ID)
    assert "pin_conflict" in [p.kind for p in service.plan.build([row.id]).albums[0].problems]
    service.match.unpin(row.id, files[0])
    assert [p.path for p in service.match.pins(row.id)] == files[1:3]
    service.match.unpin(row.id)
    assert service.match.pins(row.id) == [] and service.ProblemKind("pin_conflict") is list(service.ProblemKind)[-1]


@pytest.mark.xfail(strict=True, reason="PinConflict not added yet")
def test_a_pin_overrides_a_held_disc_only_when_asked(lib):  # noqa: F811
    row, files = _adopted_with_mystery(lib)
    assert "disc_track_count" in [p.kind for p in service.match.view(row.id).problems]
    service.match.pin(row.id, files[3], "4")  # Mystery -> There's A Man Outside, disc is held
    view = service.match.view(row.id)
    kinds = [p.kind for p in view.problems]
    assert "pin_conflict" in kinds and "disc_track_count" in kinds
    assert [c.reason for p in view.problems for c in p.pin_conflicts] == ["override_hold_required"]
    service.match.choose(row.id, HAM_ID)
    plan = service.plan.build([row.id])
    with pytest.raises(service.MMError):
        service.plan.apply(plan.id)  # held: the cause survives in held_for
    held = service.library.album(row.id).held_for
    assert [c.reason for p in held for c in p.pin_conflicts] == ["override_hold_required"]
    service.match.pin(row.id, files[3], "4", override_hold=True)
    view = service.match.view(row.id)
    assert "disc_track_count" not in [p.kind for p in view.problems]
    assert "pin_conflict" not in [p.kind for p in view.problems] and view.pins[0].override_hold
    assert [p.remote.title for p in view.pairs if p.local and p.remote][:3] == [
        "Black And White", "Followed Around", "Lost And Found"]  # never shifted
    assert [p.local.path for p in view.pairs if p.remote is None] == [files[4]]
    plan = service.plan.build([row.id], allow_track_mismatch={row.id})
    assert any("override" in w for w in plan.warnings)
