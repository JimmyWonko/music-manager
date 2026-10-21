"""service cover art: preview in plan.build, embed on apply, undo restores, Discogs image cache, settings."""

from __future__ import annotations

import pytest
from coverart_fixtures import jpeg, png
from service_fixtures import Library, direct

from music_manager import service
from music_manager.service import JobState

DISCOGS_URL = "https://img.example/1000004.jpg"


@pytest.fixture
def lib(tmp_path):
    library = Library(tmp_path).start(tmp_path)
    service.settings.set(embed_cover_art=True)  # opt-in
    yield library
    service.reset()


def wait(job):
    return service.jobs.wait(job.id)


def accepted_double(lib, cover: bytes | None):
    if cover is None:
        (lib.double / "cover.jpg").unlink()
    else:
        (lib.double / "cover.jpg").write_bytes(cover)
    wait(service.library.scan())
    d = lib.album_id(lib.double)
    lib.find(d)
    service.match.choose(d, 1000004)
    return d


def front(path):
    from music_manager.coverart import embedded_front

    return embedded_front(path)


def cover_changes(plan):
    return [t for f in plan.albums[0].files for t in f.tags if t.key == "cover_art"]


def test_settings_cover_defaults_follow_the_preset(lib):
    assert service.settings.set(embed_cover_art=None).embed_cover_art is False  # opt-in in every preset
    for preset in ("archivist", "simple", "edition-detail", "label-archive", "user:x"):
        assert service.coverart.preset_defaults(preset).embed_cover_art is False
    s = service.settings.set(embed_cover_art=True)
    assert (s.embed_cover_art, s.replace_cover_art, s.cover_source, s.cover_max_bytes) == (
        True, False, "folder_first", 0)
    s = service.settings.set(replace_cover_art=True, cover_source="discogs_first", cover_max_bytes=1000)
    assert (s.replace_cover_art, s.cover_source, s.cover_max_bytes) == (True, "discogs_first", 1000)
    s = service.settings.set(replace_cover_art=None)  # back to the preset's default
    assert s.replace_cover_art is False
    with pytest.raises(service.ValidationError):
        service.settings.set(cover_source="bogus")
    with pytest.raises(service.ValidationError):
        service.settings.set(cover_max_bytes=-1)
    assert service.coverart.preset_defaults("archivist").replace_cover_art is False


def test_folder_art_is_previewed_embedded_and_undone(lib):
    art = jpeg(600, 600, tag=b"folder")
    d = accepted_double(lib, art)
    plan = service.plan.build([d])
    ap = plan.albums[0]
    assert ap.cover is not None and (ap.cover.source, ap.cover.mime, ap.cover.width) == ("folder", "image/jpeg", 600)
    assert ap.cover.path == lib.double / "cover.jpg" and ap.cover.data == art
    changes = cover_changes(plan)
    assert len(changes) == 4 and all(c.before is None and "600x600" in c.after for c in changes)
    job = wait(service.plan.apply(plan.id))
    assert job.state is JobState.DONE, job.message
    tracks = sorted(ap.folder_after.rglob("*.flac"))
    assert len(tracks) == 4 and all(front(t).data == art for t in tracks)
    (op,) = service.journal.list(0, 10).items
    undo = service.journal.undo_plan(op.id)
    assert any(t.key == "cover_art" for a in undo.albums for f in a.files for t in f.tags)
    assert wait(service.journal.undo(op.id, undo.id)).state is JobState.DONE
    assert all(front(t) is None for t in sorted(lib.double.rglob("*.flac")))


def test_keep_existing_art_unless_replace(lib):
    from music_manager.coverart import embed_front, sniff

    old = jpeg(tag=b"old")
    d = accepted_double(lib, png(10, 10))
    first = min(lib.double.glob("*.flac"))
    embed_front(first, sniff(old), replace=False)
    wait(service.library.scan())
    plan = service.plan.build([d])
    assert len(cover_changes(plan)) == 3  # the file with art keeps it
    s = service.settings.set(replace_cover_art=True)
    assert s.replace_cover_art
    plan = service.plan.build([d])
    changes = cover_changes(plan)
    assert len(changes) == 4 and sum(c.before is not None for c in changes) == 1
    assert wait(service.plan.apply(plan.id)).state is JobState.DONE
    (op,) = service.journal.list(0, 10).items
    undo = service.journal.undo_plan(op.id)
    assert wait(service.journal.undo(op.id, undo.id)).state is JobState.DONE
    assert front(first).data == old  # undo put the original art back


def test_embed_off_changes_no_art(lib):
    d = accepted_double(lib, png())
    service.settings.set(embed_cover_art=False)
    plan = service.plan.build([d])
    assert plan.albums[0].cover is None and cover_changes(plan) == []


def test_discogs_art_is_downloaded_by_a_job_then_previewed(lib):
    art = png(300, 300)
    lib.discogs.images[DISCOGS_URL] = art
    d = accepted_double(lib, None)
    ready = []
    direct(service.events.cover_art_ready, ready.append)
    before = len([c for c in lib.discogs.calls if c[0] == "image"])
    plan = service.plan.build([d])
    assert len([c for c in lib.discogs.calls if c[0] == "image"]) == before  # build never calls Discogs
    assert plan.albums[0].cover is None and plan.cover_job is not None
    assert any("cover" in w for w in plan.warnings)
    job = service.jobs.wait(plan.cover_job)
    assert job.state is JobState.DONE and job.kind is service.JobKind.FETCH_COVER, job.message
    assert ready == [d]
    pv = service.coverart.preview(d)
    assert pv.cover is not None and (pv.cover.source, pv.cover.mime, pv.cover.width, pv.cover.height) == (
        "discogs", "image/png", 300, 300)
    assert pv.cover.url == DISCOGS_URL and pv.cover.data == art and pv.pending_url is None
    plan = service.plan.build([d])
    assert plan.cover_job is None and plan.albums[0].cover.source == "discogs"
    assert wait(service.plan.apply(plan.id)).state is JobState.DONE
    target = plan.albums[0].folder_after
    assert all(front(t).data == art for t in target.rglob("*.flac"))
    assert (target / "folder.jpg").read_bytes() == art  # save_cover_art uses the cached image
    assert len([c for c in lib.discogs.calls if c[0] == "image"]) == before + 1


def test_preview_reports_pending_download_and_existing_art(lib):
    lib.discogs.images[DISCOGS_URL] = png()
    d = accepted_double(lib, None)
    pv = service.coverart.preview(d)
    assert pv.cover is None and pv.pending_url == DISCOGS_URL and pv.existing is None
    assert (pv.files_to_embed, pv.files_keeping) == (0, 0)


def test_oversized_art_is_not_embedded(lib):
    d = accepted_double(lib, png(50, 50))
    service.settings.set(cover_max_bytes=10)
    plan = service.plan.build([d])
    assert plan.albums[0].cover is None and cover_changes(plan) == []
    assert any("larger than" in w for w in plan.warnings)


def test_invalid_folder_art_is_ignored_with_a_warning(lib):
    d = accepted_double(lib, b"not an image")
    plan = service.plan.build([d])
    assert plan.albums[0].cover is None
    assert any("not a JPEG or PNG" in w for w in plan.warnings)


def test_changed_folder_art_refuses_the_apply(lib):
    d = accepted_double(lib, png(2, 2))
    plan = service.plan.build([d])
    (lib.double / "cover.jpg").write_bytes(png(9, 9, shade=40))
    job = wait(service.plan.apply(plan.id))
    assert job.state is JobState.FAILED
    assert service.library.album(d).held_for[0].kind is service.ProblemKind.FILE_CHANGED


def test_embedding_is_off_by_default_and_folder_jpg_is_still_saved(tmp_path):
    lib = Library(tmp_path).start(tmp_path)
    try:
        lib.discogs.images[DISCOGS_URL] = png(30, 30)
        d = accepted_double(lib, None)
        plan = service.plan.build([d])
        assert plan.albums[0].cover is None and cover_changes(plan) == [] and plan.cover_job is None
        assert any("folder.jpg" in w for w in plan.warnings)
        assert wait(service.plan.apply(plan.id)).state is JobState.DONE
        target = plan.albums[0].folder_after
        assert (target / "folder.jpg").read_bytes() == png(30, 30)
        assert all(front(t) is None for t in target.rglob("*.flac"))
    finally:
        service.reset()
