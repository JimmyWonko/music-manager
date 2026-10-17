"""music_manager.service against API-CONTRACT.md: temp library, synthetic audio, fake Discogs (no network)."""

from __future__ import annotations

import dataclasses
import json
import os
from datetime import UTC
from pathlib import Path

import pytest
from service_fixtures import FakeDiscogs, Library, direct, make_album, search_result, tree

from music_manager import service
from music_manager.apply import apply_album
from music_manager.fs import collect_albums
from music_manager.match import AUTO_MIN_MARGIN, AUTO_MIN_SCORE, TITLE_MATCH_MIN
from music_manager.providers.discogs import flatten_tracklist, release_artists
from music_manager.service import (
    AlbumFilter,
    AlbumSort,
    JobState,
    MatchStatus,
    ProblemKind,
)
from music_manager.tags import read_easy_tags
from music_manager.tags.read import already_tagged


@pytest.fixture
def lib(tmp_path):
    library = Library(tmp_path).start(tmp_path)
    yield library
    service.reset()


def wait(job):
    return service.jobs.wait(job.id)


def rows(sort=AlbumSort.FOLDER, **kw):
    return service.library.albums(AlbumFilter(**kw), sort, 0, 100).items


def accepted_double(lib):
    d = lib.album_id(lib.double)
    lib.find(d)
    service.match.choose(d, 1000004)
    return d


# --- the module surface Pixel's loader checks -------------------------------------------------

def test_module_surface_matches_the_contract():
    for ns, names in {
        "library": ("albums", "status_counts", "album", "search", "scan"),
        "match": ("find", "candidates", "choose", "destination", "skip", "manual", "auto_rules", "auto_accept"),
        "provider": ("submit_link",), "plan": ("build", "apply"), "journal": ("list", "undo_plan", "undo"),
        "templates": ("list", "save", "delete", "preview"), "settings": ("get", "set"),
        "jobs": ("get", "cancel", "pause", "resume"),
    }.items():
        for name in names:
            assert callable(getattr(getattr(service, ns), name)), f"{ns}.{name}"
    for sig in ("job_progress", "job_finished", "album_changed", "candidate_ready", "rate_limited"):
        assert hasattr(service.events, sig)
    for name in ("AlbumFilter", "AlbumSort", "MatchStatus", "JobState", "JobKind", "MMError", "ValidationError",
                 "NotFound", "PlanStale", "ProviderError", "ConfigError", "Page", "Plan", "AlbumPlan", "FileChange",
                 "Problem", "ProblemKind", "Operation", "Template", "TemplatePreview", "Collision", "Settings",
                 "Destination", "AutoRules", "Candidate", "ScorePart", "TrackPair", "RemoteTrack", "LocalTrack",
                 "TagChange", "AlbumRow", "Album", "Job", "ProviderCredit"):
        assert hasattr(service, name), name


def test_types_are_frozen_with_the_contract_fields_and_defaults():
    def names(cls):
        return [f.name for f in dataclasses.fields(cls)]

    assert names(service.AlbumRow) == ["id", "folder", "artist", "title", "year", "catno", "track_count", "status",
                                       "confidence", "artwork", "artist_sort", "added_at", "held_for"]
    assert names(service.Album)[-2:] == ["tracks", "extras"]
    assert names(service.Plan) == ["id", "albums", "warnings", "files_to_move", "tags_to_write", "bytes_to_copy",
                                   "skipped", "fetch_job"]  # fetch_job: additive, defaults to None
    assert dataclasses.fields(service.Plan)[-1].default is None
    assert names(service.FileChange) == ["kind", "path_before", "path_after", "tags", "skip_reason"]
    assert names(service.Candidate) == ["release_id", "master_id", "artist", "title", "year", "country", "label",
                                        "catno", "formats", "score", "margin", "breakdown", "alignment",
                                        "artwork_url", "thumb_url", "artwork", "fetched", "source"]
    assert names(service.Operation)[-2:] == ["journal_format", "legacy"]
    assert service.Plan(1, [], [], 0, 0, 0).skipped == 0
    assert service.FileChange("track", Path("/a"), Path("/b"), []).skip_reason is None
    op = service.Operation(1, None, None, 0, 0, "apply", True, False, None, None, "")
    assert (op.journal_format, op.legacy) == (2, False)
    assert service.AutoRules() == service.AutoRules(0.90, 0.10, True, 0.80)
    assert service.Settings([], "archivist") == service.Settings([], "archivist", False, True, False)
    for cls in (service.AlbumRow, service.Plan, service.FileChange, service.Candidate, service.Settings,
                service.AlbumFilter, service.Job, service.Template):
        assert cls.__dataclass_params__.frozen, cls
    assert MatchStatus.HELD.value == "held"
    assert service.ValidationError("x", field="f").field == "f"
    assert service.ProviderError("x", retry_after=3.0).retry_after == 3.0
    assert issubclass(service.PlanStale, service.MMError) and issubclass(service.ConfigError, service.MMError)


# --- settings and templates ----------------------------------------------------------------------------

def test_settings_default_persist_and_validate(tmp_path):
    service.configure(data_dir=tmp_path / "d", config_dir=tmp_path / "c", discogs=FakeDiscogs())
    try:
        s = service.settings.get()
        assert s.library_roots == [] and s.active_template == "archivist"
        assert (s.sequential_sides, s.save_cover_art, s.clear_other_tags) == (False, True, False)
        with pytest.raises(service.ConfigError):
            service.library.scan()
        with pytest.raises(service.ValidationError) as err:
            service.settings.set(colour="red")
        assert err.value.field == "colour"
        with pytest.raises(service.ValidationError) as err:
            service.settings.set(active_template="nope")
        assert err.value.field == "active_template"
        with pytest.raises(service.ValidationError):
            service.settings.set(library_roots=["relative/path"])
        with pytest.raises(service.ValidationError):
            service.settings.set(sequential_sides="yes")
        service.settings.set(library_roots=[tmp_path / "music"], active_template="simple", sequential_sides=True)
        service.configure(data_dir=tmp_path / "d", config_dir=tmp_path / "c", discogs=FakeDiscogs())
        s = service.settings.get()
        assert s.library_roots == [tmp_path / "music"] and s.active_template == "simple" and s.sequential_sides
    finally:
        service.reset()


def test_templates_list_save_delete(lib):
    ids = [t.id for t in service.templates.list()]
    assert ids == ["archivist", "simple", "edition-detail", "label-archive"]
    assert all(t.builtin for t in service.templates.list())
    t = service.templates.save("My Layout!", "%albumartist%/%album%/%tracknumber% %title%")
    assert (t.id, t.name, t.builtin) == ("user:my-layout", "My Layout!", False)
    t2 = service.templates.save("my layout", "%albumartist%/%album%/%title%")
    assert t2.id == t.id
    assert [x.text for x in service.templates.list() if x.id == t.id] == ["%albumartist%/%album%/%title%"]
    with pytest.raises(service.ValidationError) as err:
        service.templates.save("bad", "%albumartist%/%nope%/%title%")
    assert err.value.field == "nope"
    with pytest.raises(service.ValidationError) as err:
        service.templates.save("", "%album%/%title%")
    assert err.value.field == "name"
    with pytest.raises(service.ValidationError):
        service.templates.delete("archivist")
    with pytest.raises(service.NotFound):
        service.templates.delete("user:none")
    service.settings.set(active_template=t.id)
    service.templates.delete(t.id)
    assert service.settings.get().active_template == "archivist"
    assert t.id not in [x.id for x in service.templates.list()]


def test_template_preview_validates_fields_and_renders(lib):
    for name in ("albumartist", "catno_raw", "position", "disctotal"):
        service.templates.preview(f"%{name}%", [])  # Pixel's field probe: valid fields never raise
    with pytest.raises(service.ValidationError) as err:
        service.templates.preview("%bogus%", [])
    assert err.value.field == "bogus"
    assert service.templates.preview("%title%", []).warnings  # not a usable layout: a warning, no moves
    d = accepted_double(lib)
    before = tree(lib.root)
    pv = service.templates.preview("%label%/%albumartist% - %album%/$num(%tracknumber%,2) %title%", [d])
    assert tree(lib.root) == before
    assert [after.relative_to(lib.root).as_posix() for _b, after in pv.moves] == [
        "Label Two/Band A & Band B - Double Feature/01 One.flac",
        "Label Two/Band A & Band B - Double Feature/02 Two.flac",
        "Label Two/Band A & Band B - Double Feature/01 Three.flac",
        "Label Two/Band A & Band B - Double Feature/02 Four.flac",
    ]
    assert pv.collisions == [] and pv.warnings == []
    pv = service.templates.preview("%albumartist%/%album%/%tracknumber%", [d])
    assert {c.target.name for c in pv.collisions} == {"1.flac", "2.flac"}
    assert all(c.album_ids == [d] for c in pv.collisions)


def test_template_preview_counts_albums_without_label(lib):
    d = lib.album_id(lib.double)
    lib.discogs.releases[1000004]["labels"] = [{"name": "", "catno": "LT-2CD"}]
    lib.find(d)
    pv = service.templates.preview(service.templates.list()[3].text, [d])
    assert pv.warnings == ["1 album has no label: Unknown label/"]
    assert all("Unknown label" in str(after) for _b, after in pv.moves)


# --- library ----------------------------------------------------------------------------------------------

def test_scan_indexes_albums_and_statuses(lib):
    by_folder = {r.folder: r for r in rows()}
    assert set(by_folder) == {lib.double, lib.side, lib.tape, lib.done}
    double = by_folder[lib.double]
    assert (double.artist, double.title, double.year, double.catno, double.track_count) == (
        "Band A & Band B", "Double Feature", "1998", "LT-2CD", 4)
    assert double.status is MatchStatus.UNMATCHED and double.confidence is None and double.held_for == []
    assert double.artwork == lib.double / "cover.jpg"
    assert double.added_at.tzinfo is not None and double.added_at.utcoffset() == UTC.utcoffset(None)
    assert by_folder[lib.done].status is MatchStatus.APPLIED
    album = service.library.album(double.id)
    assert [t.path.name for t in album.tracks] == ["1-01 One.flac", "1-02 Two.flac", "2-01 Three.flac",
                                                   "2-02 Four.flac"]
    assert [(t.disc, t.number, t.title) for t in album.tracks] == [(1, 1, "One"), (1, 2, "Two"), (2, 1, "Three"),
                                                                   (2, 2, "Four")]
    assert set(album.extras) == {lib.double / "scans", lib.double / "cover.jpg", lib.double / "rip.log"}
    with pytest.raises(service.NotFound):
        service.library.album(9999)
    # a rescan changes nothing
    first = {r.id: r for r in rows()}
    assert wait(service.library.scan()).state is JobState.DONE
    assert {r.id: r for r in rows()} == first


def test_scan_drops_vanished_albums(lib):
    import shutil

    shutil.rmtree(lib.tape)
    wait(service.library.scan())
    assert lib.tape not in {r.folder for r in rows()}


def test_albums_paging_sorting_filtering(lib):
    ids = [r.id for r in rows()]
    lib.find(*ids)
    page = service.library.albums(AlbumFilter(), AlbumSort.ARTIST, 0, 2)
    assert page.total == 4 and page.offset == 0 and len(page.items) == 2
    rest = service.library.albums(AlbumFilter(), AlbumSort.ARTIST, 2, 2)
    assert [r.artist_sort for r in page.items + rest.items] == ["Band A & Band B", "Done", "Kino", "The Testers"]
    desc = service.library.albums(AlbumFilter(), AlbumSort.ARTIST, 0, 10, descending=True).items
    assert [r.artist for r in desc] == ["The Testers", "Kino", "Done", "Band A & Band B"]
    status = [r.status for r in rows(AlbumSort.STATUS)]
    assert status == [MatchStatus.NEEDS_REVIEW, MatchStatus.MATCHED, MatchStatus.NO_MATCH, MatchStatus.APPLIED]
    conf = [r.confidence for r in rows(AlbumSort.CONFIDENCE)]
    assert conf[:2] == [0.75, 1.0] and conf[2:] == [None, None]
    conf_desc = [r.confidence for r in service.library.albums(AlbumFilter(), AlbumSort.CONFIDENCE, 0, 10,
                                                              descending=True).items]
    assert conf_desc[:2] == [1.0, 0.75] and conf_desc[2:] == [None, None]
    assert [r.title for r in rows(status={MatchStatus.NEEDS_REVIEW, MatchStatus.NO_MATCH})] == [
        "Tape Loops", "Side Story"]
    assert [r.title for r in rows(max_confidence=0.90)] == ["Side Story"]
    assert [r.title for r in rows(text="feat")] == ["Double Feature"]
    assert [r.title for r in rows(text="closing")] == ["Side Story"]  # track titles are searched too
    assert [r.title for r in service.library.search("testers side")] == ["Side Story"]
    with pytest.raises(service.ValidationError):
        service.library.albums(AlbumFilter(), "nonsense", 0, 10)
    counts = service.library.status_counts(AlbumFilter(status={MatchStatus.APPLIED}))
    assert set(counts) == set(MatchStatus)
    assert counts[MatchStatus.NEEDS_REVIEW] == 1 and counts[MatchStatus.APPLIED] == 1 and counts[MatchStatus.HELD] == 0
    assert sum(service.library.status_counts(AlbumFilter(text="side")).values()) == 1


def test_status_sort_puts_held_right_after_needs_review(lib):
    ids = [r.id for r in rows()]
    lib.find(*ids)
    d = lib.album_id(lib.double)
    from music_manager.service._common import set_status

    set_status(d, MatchStatus.HELD, held_for=[service.Problem(ProblemKind.COLLISION, "x", [lib.double])])
    order = [r.status for r in rows(AlbumSort.STATUS)]
    assert order[:2] == [MatchStatus.NEEDS_REVIEW, MatchStatus.HELD]
    held = next(r for r in rows() if r.id == d)
    assert held.held_for and held.held_for[0].kind is ProblemKind.COLLISION


# --- matching ------------------------------------------------------------------------------------------

def test_find_scores_like_the_cli_and_fetches_candidates(lib):
    d = lib.album_id(lib.double)
    ready = []
    changed = []
    direct(service.events.candidate_ready, lambda a, r: ready.append((a, r)))
    direct(service.events.album_changed, changed.append)
    lib.find(d, lib.album_id(lib.side), lib.album_id(lib.tape))
    assert (d, 1000004) in ready and (d, 7777) in ready
    assert d in changed
    by = {r.folder: r for r in rows()}
    assert by[lib.double].status is MatchStatus.MATCHED and by[lib.double].confidence == 1.0
    assert by[lib.side].status is MatchStatus.NEEDS_REVIEW
    assert by[lib.tape].status is MatchStatus.NO_MATCH
    cands = service.match.candidates(d)
    assert [c.release_id for c in cands] == [1000004, 7777]
    top, second = cands
    assert top.score == 1.0 and top.margin == pytest.approx(top.score - second.score) and second.margin is None
    assert top.source == service.ProviderCredit("Discogs", "Data provided by Discogs", "https://www.discogs.com/")
    assert top.fetched and top.alignment is not None
    parts = {p.field: p for p in top.breakdown}
    assert [p.field for p in top.breakdown] == ["album", "artist", "catno", "year", "track_count", "durations"]
    assert [parts[f].weight for f in ("album", "artist", "catno", "year")] == [0.40, 0.35, 0.18, 0.07]
    assert parts["track_count"].weight == 0.0 and parts["track_count"].ok
    assert all(p.ok for p in top.alignment) and {p.method for p in top.alignment} == {"number"}
    assert [p.remote.duration_s for p in top.alignment] == [181.0, 202.0, 213.0, 244.0]
    assert top.artwork_url == "https://img.example/1000004.jpg" and top.thumb_url.endswith("1000004-150.jpg")
    assert (top.artist, top.title, top.year, top.label, top.catno, top.formats) == (
        "Band A & Band B", "Double Feature", "1998", "Label Two", "LT-2CD", ["CD"])


def test_candidates_unfetched_until_the_release_arrives(lib):
    s = lib.album_id(lib.side)
    lib.discogs.fail_release.add(1000001)
    lib.find(s)
    (cand,) = service.match.candidates(s)
    assert not cand.fetched and cand.alignment is None
    assert [p.field for p in cand.breakdown] == ["album", "artist", "catno", "year"]
    assert cand.margin is None
    dest = service.match.destination(s, cand.release_id)
    assert dest.files == [] and dest.folder.parent.name == "Testers, The"


def test_auto_rules_and_auto_accept_match_the_cli(lib):
    rules = service.match.auto_rules()
    assert rules == service.AutoRules(AUTO_MIN_SCORE, AUTO_MIN_MARGIN, True, TITLE_MATCH_MIN)
    d = lib.album_id(lib.double)
    lib.find(d)
    cands = service.match.candidates(d)
    assert service.match.auto_accept(cands, local_track_count=4) == cands[0]
    assert service.match.auto_accept(cands, local_track_count=5) is None  # track count differs
    assert service.match.auto_accept(cands, dataclasses.replace(rules, require_track_count=False),
                                     local_track_count=5) == cands[0]
    assert service.match.auto_accept([]) is None
    top = cands[0]

    def c(score, fetched=True):
        return dataclasses.replace(top, score=score, alignment=top.alignment if fetched else None, fetched=fetched)

    # --auto: score >= 0.90 and lead >= 0.10; a lone result leads by its whole score
    assert service.match.auto_accept([c(0.95), c(0.84)]) is not None
    assert service.match.auto_accept([c(0.95), c(0.90)]) is None
    assert service.match.auto_accept([c(0.89)]) is None
    assert service.match.auto_accept([c(0.90)]) is not None
    assert service.match.auto_accept([c(0.95, fetched=False)], local_track_count=4) is None
    assert service.match.auto_accept([c(0.95, fetched=False)]) is not None


def test_choose_skip_manual_and_submit_link(lib):
    d = lib.album_id(lib.double)
    lib.find(d)
    with pytest.raises(service.NotFound):
        service.match.choose(d, 123)
    service.match.choose(d, 7777)
    row = next(r for r in rows() if r.id == d)
    assert row.status is MatchStatus.ACCEPTED
    service.match.skip(d)
    assert next(r for r in rows() if r.id == d).status is MatchStatus.SKIPPED
    t = lib.album_id(lib.tape)
    with pytest.raises(service.ValidationError):
        service.match.manual(t, "not a release")
    job = wait(service.match.manual(t, "https://www.discogs.com/release/1000002-Kino-Tape-Loops"))
    assert job.state is JobState.DONE, job.message
    assert [c.release_id for c in service.match.candidates(t)] == [1000002]
    assert next(r for r in rows() if r.id == t).status is MatchStatus.NEEDS_REVIEW
    assert service.provider.submit_link(t) == "https://www.discogs.com/release/add"
    with pytest.raises(service.NotFound):
        service.provider.submit_link(9999)


def test_destination_is_read_only_and_works_before_choose(lib):
    d = lib.album_id(lib.double)
    lib.find(d)
    before = tree(lib.root)
    dest = service.match.destination(d, 1000004)
    assert tree(lib.root) == before
    assert dest.library_root == lib.root and lib.root in dest.folder.parents
    assert dest.folder == lib.root / "Band A & Band B" / "[1998]  Double Feature 2CD [LT-2CD]"
    assert [b.name for b, _a in dest.files] == ["1-01 One.flac", "1-02 Two.flac", "2-01 Three.flac", "2-02 Four.flac"]
    assert dest.problems == []
    assert next(r for r in rows() if r.id == d).status is MatchStatus.MATCHED
    simple = service.match.destination(d, 1000004, "simple")
    assert simple.folder == lib.root / "Band A & Band B" / "Double Feature (1998)"
    with pytest.raises(service.NotFound):
        service.match.destination(d, 1000004, "user:missing")


def test_archivist_destination_matches_the_cli_layout(lib, tmp_path):
    d = accepted_double(lib)
    dest = service.match.destination(d, 1000004)
    copy = tmp_path / "cli" / "[1998]  Double Feature [LT-2CD]"
    make_album(copy, ["1-01 One", "1-02 Two", "2-01 Three", "2-02 Four"])
    album = next(a for a in collect_albums(copy) if a.path == copy)
    rel = lib.multi
    moves = apply_album(album, tmp_path / "cli-lib", rel, flatten_tracklist(rel, release_artists(rel)), "1998",
                        dry_run=False, journal_dir=tmp_path / "j")
    cli = sorted(dst.relative_to(tmp_path / "cli-lib") for _src, dst in moves if dst.suffix == ".flac")
    svc = sorted(after.relative_to(lib.root) for _b, after in dest.files)
    assert cli == svc


# --- plan.build -----------------------------------------------------------------------------------

def test_plan_build_is_read_only_and_describes_every_file(lib):
    d = accepted_double(lib)
    s = lib.album_id(lib.side)
    lib.find(s)
    from music_manager.service._common import set_status

    set_status(s, MatchStatus.HELD, held_for=[service.Problem(ProblemKind.COLLISION, "x", [lib.side])])
    before_tree, before_rows = tree(lib.root), rows()
    plan = service.plan.build([d, s])
    assert tree(lib.root) == before_tree and rows() == before_rows  # no disk write, no status written
    double, side = plan.albums
    assert (double.album_id, double.release_id, double.folder_before) == (d, 1000004, lib.double)
    assert double.problems == []
    kinds = [f.kind for f in double.files]
    assert kinds == ["track"] * 4 + ["extra"] * 3
    track = double.files[0]
    tags = {t.key: (t.before, t.after) for t in track.tags}
    assert tags["title"] == (None, "One") and tags["discogs_release_id"] == (None, "1000004")
    assert tags["catalognumber"] == (None, "LT-2CD")
    assert all(f.path_after.parent == double.folder_after for f in double.files if f.kind == "track")
    assert side.problems == [] and side.folder_after == lib.root / "Testers, The" / "[1985]  Side Story [WAX001]"
    assert [f.path_after.name for f in side.files][:2] == ["A1. Opening.mp3", "A2. Second.mp3"]
    assert plan.files_to_move == sum(1 for a in plan.albums for f in a.files
                                     if not f.skip_reason and f.kind != "playlist" and f.path_before != f.path_after)
    assert plan.tags_to_write == sum(len(f.tags) for a in plan.albums for f in a.files if not f.skip_reason)
    assert plan.bytes_to_copy == 0 and plan.skipped == 0
    with pytest.raises(service.NotFound):
        service.plan.build([d], "user:nope")
    with pytest.raises(service.NotFound):
        service.plan.build([9999])


def test_plan_build_unmatched_files_are_skipped_steps_and_track_mismatch(lib):
    d = accepted_double(lib)
    make_album(lib.double, ["bonus demo"])
    wait(service.library.scan())
    plan = service.plan.build([d])
    (ap,) = plan.albums
    skipped = [f for f in ap.files if f.skip_reason]
    assert [f.path_before.name for f in skipped] == ["bonus demo.flac"]
    assert skipped[0].path_after == skipped[0].path_before and skipped[0].tags == []
    assert plan.skipped == 1
    assert plan.files_to_move == 7 and plan.tags_to_write == sum(len(f.tags) for f in ap.files)
    assert [p.kind for p in ap.problems] == [ProblemKind.TRACK_MISMATCH]
    allowed = service.plan.build([d], allow_track_mismatch={d})
    assert allowed.albums[0].problems == []


def test_plan_build_reports_collisions_between_albums(lib, tmp_path):
    d = accepted_double(lib)
    twin = make_album(lib.root / "incoming" / "Band A & Band B - Double Feature",
                      ["1-01 One", "1-02 Two", "2-01 Three", "2-02 Four"])
    wait(service.library.scan())
    t = lib.album_id(twin)
    lib.find(t)
    service.match.choose(t, 1000004)
    plan = service.plan.build([d, t])
    for ap in plan.albums:
        assert [p.kind for p in ap.problems] == [ProblemKind.COLLISION]
    pv = service.templates.preview(service.templates.list()[0].text, [d, t])
    assert [sorted(c.album_ids) for c in pv.collisions] == [sorted([d, t])]


def test_plan_build_refuses_layout_problems(lib):
    d = accepted_double(lib)
    (lib.double / "outside.flac").symlink_to(lib.side / "01 Opening.mp3")
    plan = service.plan.build([d])
    kinds = [p.kind for p in plan.albums[0].problems]
    assert ProblemKind.SYMLINK_OUTSIDE in kinds
    problem = next(p for p in plan.albums[0].problems if p.kind is ProblemKind.SYMLINK_OUTSIDE)
    assert problem.paths == [lib.double / "outside.flac"]


# --- plan.apply ---------------------------------------------------------------------------------------

def test_apply_tags_moves_records_and_saves_cover(lib):
    d = accepted_double(lib)
    (lib.double / "cover.jpg").unlink()
    wait(service.library.scan())
    finished = []
    direct(service.events.job_finished, finished.append)
    plan = service.plan.build([d])
    assert any("folder.jpg" in w for w in plan.warnings)
    job = wait(service.plan.apply(plan.id))
    assert job.state is JobState.DONE and job.message.startswith("Applied 1 album(s)")
    assert finished and finished[-1].id == job.id
    target = plan.albums[0].folder_after
    assert not lib.double.exists()
    assert sorted(p.name for p in target.iterdir()) == ["1. One.flac", "1. Three.flac", "2. Four.flac", "2. Two.flac",
                                                       "folder.jpg", "rip.log", "scans"]
    assert (target / "folder.jpg").read_bytes() == b"\xff\xd8\xff fake jpeg"
    assert already_tagged(target / "1. One.flac") == "1000004"
    row = service.library.album(d)
    assert row.status is MatchStatus.APPLIED and row.folder == target
    assert [t.path.parent for t in row.tracks] == [target] * 4
    (op,) = service.journal.list(0, 10).items
    assert (op.kind, op.albums, op.undoable, op.undone, op.journal_format, op.legacy) == ("apply", 1, True, False, 2,
                                                                                          False)
    assert op.started.tzinfo is not None and op.finished >= op.started
    assert "Double Feature" in op.summary
    with pytest.raises(service.PlanStale):
        service.plan.apply(plan.id)
    with pytest.raises(service.NotFound):
        service.plan.apply(424242)


def test_apply_holds_refused_albums_and_choose_clears_hold(lib):
    d = accepted_double(lib)
    make_album(lib.double, ["bonus demo"])
    wait(service.library.scan())
    plan = service.plan.build([d])
    before = tree(lib.root)
    job = wait(service.plan.apply(plan.id))
    assert job.state is JobState.FAILED and "refused 1" in job.message
    assert tree(lib.root) == before
    row = service.library.album(d)
    assert row.status is MatchStatus.HELD
    assert [p.kind for p in row.held_for] == [ProblemKind.TRACK_MISMATCH]
    # survives a restart: it is in the library DB
    service.configure(data_dir=lib.root.parent / "data", config_dir=lib.root.parent / "config", discogs=lib.discogs)
    assert service.library.album(d).status is MatchStatus.HELD
    assert service.library.status_counts()[MatchStatus.HELD] == 1
    # plan.build never clears it; choose does
    service.plan.build([d], allow_track_mismatch={d})
    assert service.library.album(d).status is MatchStatus.HELD
    service.match.choose(d, 1000004)
    row = service.library.album(d)
    assert row.status is MatchStatus.ACCEPTED and row.held_for == []
    job = wait(service.plan.apply(service.plan.build([d], allow_track_mismatch={d}).id))
    assert job.state is JobState.DONE, job.message
    assert (lib.double / "bonus demo.flac").exists()  # unmatched file left in place


def test_apply_refuses_albums_whose_files_changed_since_the_plan(lib):
    d = accepted_double(lib)
    plan = service.plan.build([d])
    with open(lib.double / "1-01 One.flac", "ab") as fh:
        fh.write(b"\0")
    os.utime(lib.double / "1-01 One.flac", ns=(1, 1))
    job = wait(service.plan.apply(plan.id))
    assert job.state is JobState.FAILED
    row = service.library.album(d)
    assert row.status is MatchStatus.HELD
    assert row.held_for[0].kind is ProblemKind.FILE_CHANGED and lib.double / "1-01 One.flac" in row.held_for[0].paths
    assert (lib.double / "1-01 One.flac").exists() and not read_easy_tags(lib.double / "1-01 One.flac").get("title")


def test_apply_with_a_user_template(lib):
    d = accepted_double(lib)
    t = service.templates.save("Flat", "%albumartist%/%album% [%catno_raw%]/%discnumber%-$num(%tracknumber%,2) %title%")
    plan = service.plan.build([d], t.id)
    assert wait(service.plan.apply(plan.id)).state is JobState.DONE
    target = lib.root / "Band A & Band B" / "Double Feature [LT-2CD]"
    assert sorted(p.name for p in target.glob("*.flac")) == ["1-01 One.flac", "1-02 Two.flac", "2-01 Three.flac",
                                                            "2-02 Four.flac"]


# --- journal and undo ---------------------------------------------------------------------------------

def applied_double(lib):
    d = accepted_double(lib)
    original = {p.relative_to(lib.double): p.read_bytes() for p in lib.double.rglob("*") if p.is_file()}
    plan = service.plan.build([d])
    assert wait(service.plan.apply(plan.id)).state is JobState.DONE
    (op,) = service.journal.list(0, 10).items
    return d, plan, op, original


def test_undo_preview_runs_the_plan_backwards_and_is_read_only(lib):
    d, plan, op, _original = applied_double(lib)
    before = tree(lib.root)
    undo = service.journal.undo_plan(op.id)
    assert tree(lib.root) == before
    (ap,) = undo.albums
    assert ap.album_id == d and ap.folder_before == plan.albums[0].folder_after and ap.folder_after == lib.double
    forward = {f.path_before: f.path_after for f in plan.albums[0].files}
    back = {f.path_after: f.path_before for f in ap.files if f.path_before != f.path_after}
    assert back == {k: v for k, v in forward.items() if k in back}
    assert set(forward) <= set(back) | {f.path_after for f in ap.files}
    track = next(f for f in ap.files if f.path_after == lib.double / "1-01 One.flac")
    tags = {t.key: (t.before, t.after) for t in track.tags}
    assert tags["title"] == ("One", None) and tags["discogs_release_id"] == ("1000004", None)
    assert ap.problems == [] and undo.skipped == 0
    assert undo.tags_to_write == sum(len(f.tags) for f in ap.files)


def test_undo_restores_files_and_tags(lib):
    d, plan, op, original = applied_double(lib)
    undo = service.journal.undo_plan(op.id)
    job = wait(service.journal.undo(op.id, undo.id))
    assert job.state is JobState.DONE, job.message
    now = {p.relative_to(lib.double): p.read_bytes() for p in lib.double.rglob("*") if p.is_file()}
    assert now == original  # every file back, byte for byte (tags restored)
    assert not plan.albums[0].folder_after.exists()
    row = service.library.album(d)
    assert row.status is MatchStatus.NEEDS_REVIEW and row.folder == lib.double
    ops = service.journal.list(0, 10).items
    undo_op, applied = ops
    assert (undo_op.kind, undo_op.undo_of, undo_op.undoable) == ("undo", op.id, False)
    assert (applied.undone, applied.undone_by, applied.undoable) == (True, undo_op.id, False)
    with pytest.raises(service.MMError):
        service.journal.undo_plan(undo_op.id)
    with pytest.raises(service.MMError):
        service.journal.undo_plan(op.id)


def test_undo_skips_files_changed_since_the_apply(lib):
    _d, plan, op, _original = applied_double(lib)
    edited = plan.albums[0].folder_after / "2. Four.flac"
    with open(edited, "ab") as fh:
        fh.write(b"\0edited")
    undo = service.journal.undo_plan(op.id)
    (ap,) = undo.albums
    skipped = [f for f in ap.files if f.skip_reason]
    assert [f.path_before for f in skipped] == [edited]
    assert undo.skipped == 1
    assert [p.kind for p in ap.problems] == [ProblemKind.FILE_CHANGED] * len(ap.problems) and ap.problems
    assert undo.files_to_move == sum(1 for f in ap.files if not f.skip_reason and f.path_before != f.path_after)
    job = wait(service.journal.undo(op.id, undo.id))
    assert job.state is JobState.DONE and "skipped" in job.message
    assert edited.exists()  # left alone
    applied = next(o for o in service.journal.list(0, 10).items if o.id == op.id)
    assert applied.undone is False and applied.undone_by is not None  # partly undone counts False


def test_undo_needs_the_latest_preview_of_the_current_state(lib):
    _d, plan, op, _original = applied_double(lib)
    first = service.journal.undo_plan(op.id)
    second = service.journal.undo_plan(op.id)
    with pytest.raises(service.PlanStale):
        service.journal.undo(op.id, first.id)
    (plan.albums[0].folder_after / "1. One.flac").unlink()
    job = wait(service.journal.undo(op.id, second.id))
    assert job.state is JobState.FAILED and "preview again" in job.message
    with pytest.raises(service.NotFound):
        service.journal.undo(op.id, 123456)


def test_legacy_journal_needs_trust(lib, tmp_path):
    import base64
    import pickle

    _d, _plan, op, _original = applied_double(lib)
    # turn the journal into an old-format (pickled snapshot) one
    from music_manager.service._state import state

    path = Path(state().index.one("SELECT journal FROM operations WHERE id = ?", (op.id,))["journal"])
    lines = [json.loads(x) for x in path.read_text().splitlines()]
    for e in lines:
        if e.get("action") == "tags" and e.get("phase") == "before":
            e["snapshot"] = {"kind": "map", "data": base64.b64encode(pickle.dumps([])).decode()}
    path.write_text("".join(json.dumps(e) + "\n" for e in lines))
    with state().index.tx() as conn:
        conn.execute("UPDATE operations SET journal_format = 1 WHERE id = ?", (op.id,))
    listed = service.journal.list(0, 10).items[0]
    assert listed.legacy and listed.journal_format == 1
    undo = service.journal.undo_plan(op.id)
    assert [p.kind for p in undo.albums[0].problems] == [ProblemKind.LEGACY_JOURNAL]
    assert all(f.skip_reason is None for f in undo.albums[0].files)  # moves still undo normally
    with pytest.raises(service.MMError):
        service.journal.undo(op.id, undo.id)
    trusted = service.journal.undo_plan(op.id, trust_legacy=True)
    assert all(p.kind is not ProblemKind.LEGACY_JOURNAL for p in trusted.albums[0].problems)


def test_journal_list_includes_discogs_tag_journals(lib, tmp_path):
    copy = make_album(tmp_path / "cli-src" / "Some Album", ["1-01 One", "1-02 Two", "2-01 Three", "2-02 Four"])
    album = next(a for a in collect_albums(copy) if a.path == copy)
    rel = lib.multi
    from music_manager import journal as journal_mod

    apply_album(album, tmp_path / "cli-lib", rel, flatten_tracklist(rel, release_artists(rel)), "1998",
                dry_run=False, journal_dir=journal_mod.STATE_DIR / "journal")
    ops = service.journal.list(0, 10)
    assert ops.total == 1
    (op,) = ops.items
    assert op.kind == "apply" and op.undoable and op.files >= 4 and "Some Album" in op.summary
    undo = service.journal.undo_plan(op.id)
    assert undo.albums[0].album_id == 0  # not in the library index
    assert wait(service.journal.undo(op.id, undo.id)).state is JobState.DONE
    assert sorted(p.name for p in copy.glob("*.flac")) == ["1-01 One.flac", "1-02 Two.flac", "2-01 Three.flac",
                                                          "2-02 Four.flac"]


# --- jobs and events -----------------------------------------------------------------------------------

def test_jobs_cancel_pause_resume(lib):
    import threading

    from music_manager.service._state import state

    gate = threading.Event()
    runner = state().runner
    blocker = runner.submit(service.JobKind.SCAN, lambda ctx: gate.wait(5) and "blocked")
    queued = service.library.scan()
    service.jobs.cancel(queued.id)
    assert service.jobs.get(queued.id).state is JobState.CANCELLED
    paused = service.library.scan()
    service.jobs.pause(paused.id)
    assert service.jobs.get(paused.id).state is JobState.PAUSED
    gate.set()
    assert wait(blocker).state is JobState.DONE
    assert service.jobs.get(paused.id).state is JobState.PAUSED
    service.jobs.resume(paused.id)
    assert wait(paused).state is JobState.DONE
    with pytest.raises(service.NotFound):
        service.jobs.get(99999)


def test_rate_limit_waits_are_signalled(lib):
    from music_manager.providers.discogs import Discogs
    from music_manager.service._state import state

    seen = []
    direct(service.events.rate_limited, seen.append)
    client = Discogs.__new__(Discogs)
    client.on_wait = None
    state().discogs = client
    state().client()
    assert client.on_wait is not None
    client.on_wait(1.5)
    assert seen == [1.5]


def test_discogs_errors_become_provider_errors_in_jobs(lib):
    s = lib.album_id(lib.side)

    def broken(*_a):
        from music_manager.providers.discogs import DiscogsError

        raise DiscogsError("Discogs HTTP 429: slow down")

    lib.discogs.search_releases = broken
    job = wait(service.match.find([s]))
    assert job.state is JobState.DONE and "1 failed" in job.message
    assert service.library.album(s).status is MatchStatus.UNMATCHED


def test_search_result_helper_round_trip():
    rel = {"id": 1, "title": "T", "year": 2000, "artists": [{"name": "A"}], "labels": [{"name": "L", "catno": "C 1"}],
           "formats": [{"name": "CD"}], "master_id": 0}
    assert search_result(rel)["title"] == "A - T"
