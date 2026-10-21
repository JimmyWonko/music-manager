"""Adapter tests: mode selection, real-backend loading/errors (read-only), fake contract facade."""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from mm_ui import backend as B
from mm_ui.fake_service import FakeService, MatchStatus

from ui_env import BACKEND_PATH, BASSLINE  # noqa: F401
from test_ui_flows import built

# A minimal contract-shaped music_manager.service for a temp "checkout"
STUB = textwrap.dedent('''
    from enum import Enum, StrEnum, auto
    from dataclasses import dataclass
    from types import SimpleNamespace as NS
    class MatchStatus(Enum):
        UNMATCHED = auto(); NEEDS_REVIEW = auto(); MATCHED = auto(); ACCEPTED = auto()
    class JobState(Enum):
        DONE = auto(); FAILED = auto()
    class AlbumSort(StrEnum):
        ARTIST = "artist"; YEAR = "year"
    class MMError(Exception): pass
    class ValidationError(MMError): pass
    @dataclass(frozen=True)
    class AlbumFilter:
        status: object = None; max_confidence: object = None; text: str = ""
    @dataclass(frozen=True)
    class Page:
        items: list; total: int; offset: int
    _f = lambda *a, **k: None
    library = NS(albums=lambda f, s, o, l, descending=False: Page([], 0, o), album=_f,
                 status_counts=lambda f=None: {s: (3 if s is MatchStatus.MATCHED else 0) for s in MatchStatus})
    match = NS(candidates=_f, choose=_f, skip=_f, destination=_f)
    provider = NS(submit_link=_f)
    plan = NS(build=_f, apply=_f)
    journal = NS(list=_f, undo_plan=_f, undo=_f)
    templates = NS(list=_f, save=_f, delete=_f, preview=_f)
    settings = NS(get=_f, set=_f)
    jobs = NS(get=_f)
    events = NS(job_finished=_f, job_progress=_f, album_changed=_f, candidate_ready=_f, rate_limited=_f)
''')


def make_checkout(tmp_path, body=STUB, layout="src"):
    root = tmp_path / "checkout"
    pkg = (root / "src" if layout == "src" else root) / "music_manager"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "service.py").write_text(body)
    return root


def tree(root: Path):
    return sorted((str(p.relative_to(root)), p.stat().st_size, p.stat().st_mtime_ns)
                  for p in root.rglob("*"))


class Notes:
    def __init__(self): self.calls = []
    def __call__(self, msg, fatal): self.calls.append((msg, fatal))


# ---------------------------------------------------------------- mode selection
def test_default_is_fake_and_cached(qapp):
    a = B.get_service()
    assert a.is_fake and a.kind == "fake" and a.label == "mock data"
    assert B.get_service() is a


def test_explicit_fake(qapp, monkeypatch):
    monkeypatch.setenv("MM_BACKEND", "FAKE")
    assert B.get_service().is_fake


def test_unknown_mode_is_an_error(qapp, monkeypatch):
    monkeypatch.setenv("MM_BACKEND", "bogus")
    n = Notes()
    with pytest.raises(B.BackendUnavailable, match="bogus"):
        B.get_service(n)
    assert n.calls and n.calls[0][1] is True


def test_real_missing_path_no_fallback(qapp, monkeypatch, tmp_path):
    missing = tmp_path / "nope"
    monkeypatch.setenv("MM_BACKEND", "real"); monkeypatch.setenv("MM_BACKEND_PATH", str(missing))
    n = Notes()
    with pytest.raises(B.BackendUnavailable) as e:
        B.get_service(n)
    assert str(missing) in str(e.value)
    assert n.calls[0][1] is True and "not falling back" in n.calls[0][0]


def test_real_default_to_stderr(qapp, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MM_BACKEND", "real"); monkeypatch.setenv("MM_BACKEND_PATH", str(tmp_path / "x"))
    with pytest.raises(B.BackendUnavailable):
        B.get_service()
    err = capsys.readouterr().err
    assert err.startswith("ERROR: Real backend path does not exist:") and str(tmp_path / "x") in err


def test_auto_falls_back_with_warning(qapp, monkeypatch, tmp_path):
    monkeypatch.setenv("MM_BACKEND", "auto"); monkeypatch.setenv("MM_BACKEND_PATH", str(tmp_path / "nope"))
    n = Notes()
    svc = B.get_service(n)
    assert svc.is_fake
    assert n.calls[0][1] is False and "Falling back" in n.calls[0][0]


@pytest.mark.skipif(not (BASSLINE / "src" / "music_manager" / "service").is_dir(),
                    reason="Bassline's service checkout not present")
def test_real_against_bassline_checkout_is_clear_and_read_only(qapp, monkeypatch, tmp_path):
    """MM_BACKEND=real loads music_manager.service from <checkout>/src and leaves the checkout untouched."""
    before, pyc_before = tree(BASSLINE), _pyc(BASSLINE)
    monkeypatch.setenv("MM_BACKEND", "real"); monkeypatch.setenv("MM_BACKEND_PATH", str(BACKEND_PATH))
    monkeypatch.setenv("MM_DATA_DIR", str(tmp_path / "data")); monkeypatch.setenv("MM_CONFIG_DIR", str(tmp_path / "config"))
    n = Notes()
    svc = B.get_service(n)
    assert n.calls == [] and svc.kind == "real" and not svc.is_fake
    assert svc.origin.startswith(str((BASSLINE / "src").resolve()))
    assert {m.name for m in svc.types.MatchStatus} >= {"NEEDS_REVIEW", "HELD"}
    assert all(isinstance(m.value, str) for m in svc.types.MatchStatus)     # API-DEVIATIONS #13
    s = svc.settings.get()                                                 # the configured temp dirs, not XDG
    assert s.library_roots == [] and (tmp_path / "data" / "library.db").exists()
    svc._c.reset()
    assert _pyc(BASSLINE) == pyc_before and _ours_in(BASSLINE) == []   # no bytecode, index or config written there


def test_real_stub_checkout_loads_without_bytecode(qapp, monkeypatch, tmp_path):
    root = make_checkout(tmp_path)
    monkeypatch.setenv("MM_BACKEND", "real"); monkeypatch.setenv("MM_BACKEND_PATH", str(root))
    svc = B.get_service(Notes())
    assert svc.kind == "real" and not svc.is_fake
    assert svc.origin == str((root / "src/music_manager/service.py").resolve())
    assert sys.dont_write_bytecode and os.environ["PYTHONDONTWRITEBYTECODE"] == "1"
    assert not list(root.rglob("__pycache__")) and not list(root.rglob("*.pyc"))
    # UI goes straight to the contract; helpers only translate names
    counts = {k.name: v for k, v in svc.library.status_counts().items()}
    assert counts == {"UNMATCHED": 0, "NEEDS_REVIEW": 0, "MATCHED": 3, "ACCEPTED": 0}
    assert not hasattr(svc, "status_counts") and not hasattr(svc, "destination_preview")
    assert svc.album_filter({"MATCHED"}).status == {svc.types.MatchStatus.MATCHED}
    assert svc.sort("year") is svc.types.AlbumSort.YEAR


def test_real_flat_layout_also_found(qapp, monkeypatch, tmp_path):
    root = make_checkout(tmp_path, layout="flat")
    monkeypatch.setenv("MM_BACKEND", "real"); monkeypatch.setenv("MM_BACKEND_PATH", str(root))
    assert B.get_service(Notes()).kind == "real"


def test_real_stub_missing_contract_members(qapp, monkeypatch, tmp_path):
    body = STUB.replace("journal = NS(list=_f, undo_plan=_f, undo=_f)", "journal = NS(list=_f)") \
               .replace("plan = NS(build=_f, apply=_f)", "") \
               .replace("templates = NS(list=_f, save=_f, delete=_f, preview=_f)", "templates = NS(list=_f, preview=_f)") \
               .replace("match = NS(candidates=_f, choose=_f, skip=_f, destination=_f)",
                        "match = NS(candidates=_f, choose=_f, skip=_f)")
    root = make_checkout(tmp_path, body)
    monkeypatch.setenv("MM_BACKEND", "real"); monkeypatch.setenv("MM_BACKEND_PATH", str(root))
    with pytest.raises(B.BackendUnavailable) as e:
        B.get_service(Notes())
    m = str(e.value)
    for piece in ("plan.build", "plan.apply", "journal.undo_plan", "journal.undo", "templates.save",
                  "templates.delete", "match.destination"):
        assert piece in m
    assert "journal.list" not in m


def test_real_stub_missing_dependency_named(qapp, monkeypatch, tmp_path):
    root = make_checkout(tmp_path, "import mutagen_not_installed_xyz\n" + STUB)
    monkeypatch.setenv("MM_BACKEND", "real"); monkeypatch.setenv("MM_BACKEND_PATH", str(root))
    with pytest.raises(B.BackendUnavailable, match="dependency 'mutagen_not_installed_xyz'"):
        B.get_service(Notes())


def test_real_package_without_service_module(qapp, monkeypatch, tmp_path):
    root = make_checkout(tmp_path)
    (root / "src/music_manager/service.py").unlink()
    monkeypatch.setenv("MM_BACKEND", "real"); monkeypatch.setenv("MM_BACKEND_PATH", str(root))
    with pytest.raises(B.BackendUnavailable, match=r"Missing: music_manager\.service"):
        B.get_service(Notes())


# ---------------------------------------------------------------- fake facade
def test_as_backend_coercions(fake):
    assert B.as_backend(fake) is fake
    raw = FakeService(fetch_delay_ms=10**6)
    wrapped = B.as_backend(raw)
    assert wrapped.is_fake and wrapped._c.fake is raw
    with pytest.raises(TypeError):
        B.as_backend("nope")


def test_status_counts_one_call_ignores_filter_status(fake):
    S = fake.types.MatchStatus
    c = fake.library.status_counts()
    assert set(c) == set(S)                                       # every status is counted (zeros too)
    raw = {}
    for a in fake._c.fake.albums(fake.album_filter()):
        raw[a.status] = raw.get(a.status, 0) + 1
    assert c[S.UNMATCHED] == 50 + raw.get(S.UNMATCHED, 0) and c[S.MATCHED] == raw[S.MATCHED]
    assert fake.library.status_counts(fake.album_filter({"MATCHED"})) == c   # filter.status ignored
    t = fake.library.status_counts(fake.album_filter(None, "damaged"))        # text is honoured
    assert sum(t.values()) == 1 and t[S.UNMATCHED] == 0


def test_library_paging_crosses_into_synthetic_rows(fake):
    f = fake.album_filter()
    n_real = len(fake._c.fake.albums(f))
    first = fake.library.albums(f, fake.types.AlbumSort.FOLDER, 0, n_real + 5)
    assert first.total == n_real + 50 and len(first.items) == n_real + 5
    assert fake.library.albums(fake.album_filter(text="Mock"), "artist", 0, 10).total == 0  # search: no padding
    syn = [a for a in first.items if a.title.startswith("Mock")][0]
    assert fake.library.album(syn.id).tracks == [] and fake.match.candidates(syn.id) == []
    with pytest.raises(fake.types.NotFound):
        fake.library.album(10**9)


@pytest.mark.parametrize("key", ["artist", "title", "year", "added", "confidence", "status", "folder"])
@pytest.mark.parametrize("desc", [False, True])
def test_album_sort_pages_are_stable_and_complete(fake, key, desc):
    sort = fake.sort(key)
    f = fake.album_filter()
    whole = fake.library.albums(f, sort, 0, 10**6, descending=desc)
    paged = [a for off in range(0, whole.total, 7)
             for a in fake.library.albums(f, sort, off, 7, descending=desc).items]
    assert [a.id for a in paged] == [a.id for a in whole.items]
    assert len({a.id for a in whole.items}) == whole.total == len(whole.items)


def test_album_sort_semantics(fake):
    S, f = fake.types.AlbumSort, fake.album_filter({"MATCHED", "NEEDS_REVIEW", "NO_MATCH"})
    rows = lambda s, d=False, flt=f: fake.library.albums(flt, s, 0, 10**6, descending=d).items
    titles = [a.title for a in rows(S.TITLE)]
    assert titles == sorted(titles, key=str.casefold)
    assert [a.title for a in rows(S.TITLE, True)] == titles[::-1]
    with pytest.raises(fake.types.ValidationError):
        rows("-title")                                   # "-" prefix is gone from the contract
    # descending flips the primary key only; the folder tiebreak stays ascending
    y_desc = rows(S.YEAR, True)
    assert [a.year for a in y_desc] == sorted((a.year for a in y_desc), reverse=True)
    same = [str(a.folder) for a in y_desc if a.year == "1984"]
    assert len(same) == 3 and same == sorted(same)
    same_asc = [str(a.folder) for a in rows(S.YEAR) if a.year == "1984"]
    assert same_asc == same
    # FOLDER descending
    folders = [str(a.folder) for a in rows(S.FOLDER, True)]
    assert folders == sorted(folders, reverse=True)
    # confidence: lowest first, unmatched (None) last; descending: highest first, None still last
    allf = fake.album_filter()
    conf = [a.confidence for a in rows(S.CONFIDENCE, flt=allf)]
    real = [c for c in conf if c is not None]
    assert real == sorted(real) and conf[len(real):] == [None] * (len(conf) - len(real))
    conf_d = [a.confidence for a in rows(S.CONFIDENCE, True, allf)]
    assert conf_d[:len(real)] == sorted(real, reverse=True) and conf_d[-1] is None
    order = [a.status.name for a in rows(S.STATUS, flt=allf)]
    assert order.index("NEEDS_REVIEW") < order.index("UNMATCHED") < order.index("MATCHED")


def test_artist_sort_and_added_at_fields_drive_sorting(fake):
    S = fake.types.AlbumSort
    rows = fake.library.albums(fake.album_filter(), S.ARTIST, 0, 10**6).items
    for a in rows:
        assert isinstance(a.artist_sort, str) and a.artist_sort
        assert a.added_at.tzinfo is not None and a.added_at.utcoffset().total_seconds() == 0   # tz-aware UTC
    # ARTIST sorts by artist_sort (ALBUMARTISTSORT), not the display artist
    aid = fake.albums({"MATCHED"}, limit=1).items[0].id
    fake._c.fake._set(aid, artist_sort="Aaa, Flag Black")
    first = fake.library.albums(fake.album_filter(), S.ARTIST, 0, 1).items[0]
    assert first.id == aid and first.artist == "Black Flag"
    last = fake.library.albums(fake.album_filter(), S.ARTIST, 0, 1, descending=True).items[0]
    assert last.artist_sort.startswith("Mock Artist")
    # ADDED: newest scan first; descending = oldest first
    added = [a.added_at for a in fake.library.albums(fake.album_filter(), S.ADDED, 0, 10**6).items]
    assert added == sorted(added, reverse=True)
    oldest = fake.library.albums(fake.album_filter(), S.ADDED, 0, 1, descending=True).items[0]
    assert oldest.added_at == min(added)


def test_match_destination(fake):
    D = fake.types.Destination
    a = fake.albums({"NEEDS_REVIEW"}, limit=1).items[0]        # Damaged, nothing chosen yet
    c = fake.match.candidates(a.id)[0]
    d = fake.match.destination(a.id, c.release_id)            # works before choose; None = active
    assert isinstance(d, D) and d.files == [] and d.problems == []   # files empty until fetched
    assert d.library_root == Path("/mock/grok-bot-approved") and d.library_root.is_absolute()
    assert d.folder.is_absolute() and d.library_root in d.folder.parents
    assert d.folder.relative_to(d.library_root) == Path("Black Flag/[1981]  Damaged [SST007]")
    fake._c.fake._fetch_queue = [(a.id, c.release_id)]; fake._c.fake._fetch_one()
    d = fake.match.destination(a.id, c.release_id, "archivist")
    assert len(d.files) == fake.library.album(a.id).track_count
    assert all(b.is_absolute() and x.is_absolute() for b, x in d.files)
    assert d.files[0][1].name == "A1. Rise Above.flac"
    assert fake.match.destination(a.id, c.release_id, "simple").folder.name == "Damaged (1981)"
    ed = fake.match.destination(a.id, c.release_id, "edition-detail")
    assert ed.folder.name == "Damaged (1981) [SST007 • Vinyl]"     # empty reissueyear drops with its ⁄
    t = fake.templates.save("Flat", "%albumartist%/x/%title%")
    assert fake.match.destination(a.id, c.release_id, t.id).folder.name == "x"
    with pytest.raises(fake.types.NotFound):                       # template text is not accepted
        fake.match.destination(a.id, c.release_id, "%albumartist%/x/%title%")
    with pytest.raises(fake.types.NotFound):
        fake.match.destination(a.id, c.release_id, "no-such-id")
    fake.settings.set(sequential_sides=True)
    assert fake.match.destination(a.id, c.release_id).files[0][1].name == "1. Rise Above.flac"
    with pytest.raises(fake.types.NotFound):
        fake.match.destination(a.id, 123)


def test_match_destination_reports_collision_with_queued_album(fake):
    ids = [a.id for a in fake.albums({"MATCHED", "NEEDS_REVIEW"}, limit=10).items]
    fake.match.choose(ids[0], fake.match.candidates(ids[0])[0].release_id)
    t = fake.templates.save("Artist only", "%albumartist%/%title%")
    fake.settings.set(active_template=t.id)
    d = fake.match.destination(ids[1], fake.match.candidates(ids[1])[0].release_id)
    assert d.problems and "Collision with another album" in d.problems[0].message and d.problems[0].kind == "collision"


def test_templates_list_save_delete_and_settings(fake):
    T = fake.templates.list()
    assert [t.id for t in T] == ["archivist", "simple", "edition-detail", "label-archive"]
    assert all(t.builtin for t in T)
    assert T[0].name == "Archivist (default)"
    assert T[0].text == "%albumartist%/[%leadyear%]  %album% [%reissueyear%] [%catno%]/%position%. %title%"
    s = fake.settings.get()
    assert isinstance(s, fake.types.Settings) and s.active_template == "archivist" and not s.sequential_sides
    t = fake.templates.save("My Layout", "%albumartist%/%album%/%position% %title%")
    assert (t.id, t.name, t.builtin) == ("user:my-layout", "My Layout", False)
    assert fake.templates.list()[-1] == t
    with pytest.raises(fake.types.ValidationError) as e:
        fake.templates.save("Bad", "%albumartist%/%nope%")
    assert e.value.field == "nope"
    s = fake.settings.set(active_template=t.id)
    assert s.active_template == t.id and fake.settings.get() == s
    with pytest.raises(fake.types.ValidationError):
        fake.settings.set(active_template="user:missing")
    with pytest.raises(fake.types.ValidationError):
        fake.settings.set(no_such_setting=1)
    with pytest.raises(fake.types.ValidationError):
        fake.templates.delete("archivist")
    fake.templates.delete(t.id)
    assert fake.settings.get().active_template == "archivist"   # mock choice; contract silent
    with pytest.raises(fake.types.NotFound):
        fake.templates.delete(t.id)


def test_templates_preview_shape_collisions_unknown_label(fake):
    ids = fake.sample_album_ids(5)
    pv = fake.templates.preview(fake.templates.list()[3].text, ids)     # label-archive
    assert isinstance(pv, fake.types.TemplatePreview)
    assert pv.collisions == []
    assert pv.warnings == ["1 album has no label: Unknown label/"]       # Live '84 has no release
    assert any("/Unknown label/" in str(after) for _b, after in pv.moves)
    assert all(b.is_absolute() and a.is_absolute() for b, a in pv.moves)
    assert all(str(a).endswith(".flac") for _b, a in pv.moves)
    syn = fake.albums(None, "", 5, 1).items[0]                          # track-less synthetic album
    pv = fake.templates.preview(fake.templates.list()[3].text, [syn.id])
    assert pv.moves == [(syn.folder, Path("/mock/synthetic/Unknown label/Mock Artist 00000 - Mock Album 0 (1960)"))]
    pv = fake.templates.preview("%albumartist%/%title%", ids)           # every album in one folder
    folder = [c for c in pv.collisions if len(c.album_ids) > 1]
    assert folder and sorted(folder[0].album_ids) == sorted(ids)
    pv = fake.templates.preview("%albumartist%/%album%/%album%", ids[:1])   # two files, one album
    assert pv.collisions and pv.collisions[0].album_ids == ids[:1]
    with pytest.raises(fake.types.ValidationError):
        fake.templates.preview("%bogus%", ids)


def test_plan_build_refuses_collisions_and_apply_skips_them(fake, qtbot):
    ids = [a.id for a in fake.albums({"MATCHED", "NEEDS_REVIEW"}, limit=10).items][:2]
    for i in ids:
        fake.match.choose(i, fake.match.candidates(i)[0].release_id)
    t = fake.templates.save("Artist only", "%albumartist%/%title%")
    with pytest.raises(fake.types.NotFound):
        fake.plan.build(ids, "%albumartist%/%title%")            # ids only, never text
    plan = built(fake, ids, t.id)
    assert all(ap.folder_after.is_absolute() for ap in plan.albums)
    assert all(ap.problems for ap in plan.albums)
    with qtbot.waitSignal(fake.events.job_finished, timeout=2000) as sig:
        fake.plan.apply(plan.id)
    assert sig.args[0].state.name == "FAILED" and "refused 2" in sig.args[0].message
    assert all(fake.library.album(i).status.name == "HELD" for i in ids)          # contract: refused -> HELD
    assert all(fake.library.album(i).held_for and fake.library.album(i).held_for[0].kind == "collision" for i in ids)
    clean = built(fake, ids[:1], "archivist")             # contract: build is read-only, HELD stays
    assert not clean.albums[0].problems
    assert fake.library.album(ids[0]).status.name == "HELD" and fake.library.album(ids[0]).held_for
    with qtbot.waitSignal(fake.events.job_finished, timeout=2000) as sig:
        fake.plan.apply(clean.id)                                 # successful apply clears it
    assert sig.args[0].state.name == "DONE"
    assert fake.library.album(ids[0]).status.name == "APPLIED" and fake.library.album(ids[0]).held_for == []
    fake.match.skip(ids[1])                                      # choose/skip/manual clear it too
    assert fake.library.album(ids[1]).status.name == "SKIPPED" and fake.library.album(ids[1]).held_for == []


def test_manual_clears_held(fake, qtbot):
    T = fake.types
    a = fake.albums({"MATCHED"}, limit=1).items[0]
    fake._c.fake._set(a.id, status=T.MatchStatus.HELD,
                      held_for=[T.Problem(T.ProblemKind.COLLISION, "x", [a.folder])])
    with qtbot.waitSignal(fake.events.job_finished, timeout=2000):
        fake.match.manual(a.id, "https://www.discogs.com/release/1")
    assert fake.library.album(a.id).status.name != "HELD" and fake.library.album(a.id).held_for == []


def test_plan_apply_is_an_async_job(fake, qtbot):
    a = fake.albums({"MATCHED"}, limit=1).items[0]
    fake.match.choose(a.id, fake.match.candidates(a.id)[0].release_id)
    plan = built(fake, [a.id])
    assert fake.library.album(a.id).status.name == "ACCEPTED"          # build is read-only
    with qtbot.waitSignal(fake.events.job_finished, timeout=2000) as sig:
        job = fake.plan.apply(plan.id)
        assert job.state.name == "QUEUED" and job.kind.name == "APPLY"
    assert sig.args[0].id == job.id and sig.args[0].state.name == "DONE"
    assert fake.library.album(a.id).status is MatchStatus.APPLIED
    op = fake.journal.list(0, 1).items[0]
    assert op.summary.startswith("Tagged and moved") and op.kind == "apply" and op.undoable
    assert (op.undone, op.undone_by, op.undo_of) == (False, None, None)
    assert fake.library.album(a.id).folder == plan.albums[0].folder_after


def test_journal_seed_and_undo_fields(fake, qtbot):
    ops = fake.journal.list(0, 100).items
    undone = [o for o in ops if o.undone]
    undos = [o for o in ops if o.kind == "undo"]
    assert len(undone) == 1 and len(undos) == 1
    assert undone[0].undone_by == undos[0].id and undos[0].undo_of == undone[0].id
    assert not undone[0].undoable and not undos[0].undoable
    with pytest.raises(fake.types.MMError):
        fake.journal.undo_plan(undone[0].id)
    live = next(o for o in ops if o.undoable)
    p = fake.journal.undo_plan(live.id)
    with qtbot.waitSignal(fake.events.job_finished, timeout=2000) as sig:
        fake.journal.undo(live.id, p.id)
    assert sig.args[0].state.name == "DONE"
    after = {o.id: o for o in fake.journal.list(0, 100).items}
    u = next(o for o in after.values() if o.undo_of == live.id)
    assert u.kind == "undo" and after[live.id].undone and after[live.id].undone_by == u.id


def test_undo_with_stale_plan_id_fails(fake, qtbot):
    op = fake.journal.list(0, 10).items[0]
    p = fake.journal.undo_plan(op.id)
    with qtbot.waitSignal(fake.events.job_finished, timeout=2000) as sig:
        fake.journal.undo(op.id, p.id + 999)
    assert sig.args[0].state.name == "FAILED" and "PlanStale" in sig.args[0].message
    assert fake.journal.list(0, 10).items[0].undoable


# ---------------------------------------------------------------- app entry point (subprocess, offscreen)
def _run_app(env_extra, tmp_path):
    import mm_ui
    home = Path(mm_ui.__file__).resolve().parents[1]      # the folder holding mm_ui (working copy or src/)
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "MM_SMOKE": "1", **env_extra}
    env["PYTHONPATH"] = os.pathsep.join(x for x in (str(home), env.get("PYTHONPATH", "")) if x)
    return subprocess.run([sys.executable, "-m", "mm_ui"], cwd=home, env=env, capture_output=True, text=True,
                          timeout=60)


def test_app_smoke_fake(tmp_path):
    r = _run_app({"MM_BACKEND": "fake"}, tmp_path)
    assert r.returncode == 0, r.stderr
    assert "MM_SMOKE ok" in r.stdout and "backend=fake" in r.stdout and "Matrixwell — Library" in r.stdout


@pytest.mark.skipif(not (BASSLINE / "src" / "music_manager" / "service").is_dir(),
                    reason="Bassline's service checkout not present")
def test_app_smoke_real(tmp_path):
    before, pyc_before = tree(BASSLINE), _pyc(BASSLINE)
    r = _run_app({"MM_BACKEND": "real", "MM_BACKEND_PATH": str(BACKEND_PATH), "MM_DATA_DIR": str(tmp_path / "data"),
                  "MM_CONFIG_DIR": str(tmp_path / "config"), "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
                  "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"), "XDG_STATE_HOME": str(tmp_path / "xdg-state"),
                  "XDG_CACHE_HOME": str(tmp_path / "xdg-cache")}, tmp_path)
    assert r.returncode == 0, r.stderr
    assert "backend=real" in r.stdout and (tmp_path / "data" / "library.db").exists()
    assert _pyc(BASSLINE) == pyc_before and _ours_in(BASSLINE) == []


def test_app_real_strict_exits_2_naming_path(tmp_path):
    missing = tmp_path / "no-checkout"
    r = _run_app({"MM_BACKEND": "real", "MM_BACKEND_PATH": str(missing)}, tmp_path)
    assert r.returncode == 2
    assert str(missing) in r.stderr


def test_app_auto_falls_back(tmp_path):
    r = _run_app({"MM_BACKEND": "auto", "MM_BACKEND_PATH": str(tmp_path / "no-checkout")}, tmp_path)
    assert r.returncode == 0 and "backend=fake" in r.stdout and "WARNING:" in r.stderr


# ---------------------------------------------------------------- mock vs API-CONTRACT.md (read-only parse)
CONTRACT = Path(os.environ.get("MM_TEST_CONTRACT", "/workspace/mm-fix/API-CONTRACT.md"))


# Contract sections for service commits the UI doesn't build against yet (regexes on the heading).
PENDING_SECTIONS: list[str] = []   # Subtracks (2026-10-21) is built against now; was ["Subtracks \\(2026-10-21\\)"]


def _without_pending(text: str) -> str:
    """The contract minus PENDING_SECTIONS (each up to the next heading of the same or a higher level)."""
    import re
    out, skip = [], None
    for line in text.splitlines():
        h = re.match(r"(#+) ", line)
        if h and skip is not None and len(h.group(1)) <= skip:
            skip = None
        if h and any(re.search(pat, line) for pat in PENDING_SECTIONS):
            skip = len(h.group(1))
        if skip is None:
            out.append(line)
    return "\n".join(out)


def _contract_code():
    """The contract's python blocks (1f7b340: every type is a full block, including the additions)."""
    import re
    text = _without_pending(CONTRACT.read_text())
    return "\n".join(re.findall(r"```python\n(.*?)```", text, re.S))


def _contract_classes(code, with_partial=False):
    """Class blocks from the contract. A later `class X: ...; field: T` block (or `class X:  # changed` whose body
    starts with `...`) extends X (appends fields,
    also to subclasses); `class X(Enum): ...; M = v` is a partial enum listing only additions."""
    import re
    out, lines, partial = {}, code.splitlines(), set()
    for i, line in enumerate(lines):
        m = re.match(r"class (\w+)(?:\((\w+)\))?:\s*(.*)", line)
        if not m:
            continue
        name, base, rest = m.groups()
        body = [rest] if rest.strip() else []
        for nxt in lines[i + 1:]:
            if not nxt.startswith("    "):
                break
            body.append(nxt)
        text = "\n".join(b.split("#", 1)[0] for b in body)
        code = [b.split("#", 1)[0].strip() for b in body if b.split("#", 1)[0].strip()]
        if code and (code[0].startswith("...") or ("..." in code[1:] and name in out)):
            # "class X: ...; f: T", or "class X:  # changed" whose body has a "..." line: extends X
            text = "\n".join(c for c in code if c != "...").removeprefix("...")
            if base in ("Enum", "StrEnum"):
                prev = out.get(name, ("enum", []))[1]
                adds = [m for m in re.findall(r"\b([A-Z][A-Z_]+)\b", text) if m not in prev]   # restated: no dup
                out[name] = ("enum", prev + adds); partial.add(name)
            else:
                old = out[name][1]
                new = old + [f for f in re.findall(r"(\w+)\s*:", text) if f not in old]   # restated: no dup
                out[name] = ("fields", new)
                for other, (k, m) in list(out.items()):      # subclasses (Album(AlbumRow)) inherit it
                    if other != name and k == "fields" and m[:len(old)] == old and len(m) > len(old):
                        out[other] = (k, new + m[len(old):])
            continue
        if base in ("Enum", "StrEnum"):
            out[name] = ("enum", re.findall(r"\b([A-Z][A-Z_]+)\b", text))
        elif base == "QObject":
            out[name] = ("signals", re.findall(r"(\w+)\s*=\s*Signal", text))
        else:
            out[name] = ("fields", (out.get(base, ("", []))[1] if base else []) + re.findall(r"(\w+)\s*:", text))
    return (out, partial) if with_partial else out


@pytest.mark.skipif(not CONTRACT.is_file(), reason="contract not present")
def test_mock_types_match_contract_exactly():
    import dataclasses
    import mm_ui.fake_service as T
    classes, partial = _contract_classes(_contract_code(), with_partial=True)
    assert {"AlbumSort", "Destination", "Template", "TemplatePreview", "Collision", "Settings"} <= set(classes)
    assert classes["AlbumRow"][1][-3:] == ["held_for", "loose_tracks", "cluster_id"]   # d6ac2f3, c334bbf
    assert classes["Album"][1][-3:] == ["cluster_id", "tracks", "extras"]
    assert classes["Cluster"][1][-2:] == ["album_id", "kind"] and classes["MatchView"][1][-3:] == ["problems", "pins", "subtrack_groups"]
    assert {"ClusterRow", "Pin", "ManualPair", "PinConflict"} <= set(classes)                      # c2fcf25, 1f7b340
    assert classes["Problem"][1][-1] == "pin_conflicts" and classes["PinConflict"][1] == ["path", "position", "reason"]
    assert classes["FileChange"][1][-2:] == ["pinned", "merged_subtracks"] and classes["JobKind"][1][-1] == "CLUSTER"
    assert classes["ClusterFile"][1] == ["path", "disc", "number", "title", "duration_s", "album", "artist"]
    assert classes["FileChange"][1][-3] == "skip_reason" and classes["Plan"][1][-2:] == ["skipped", "fetch_job"]
    assert "RELEASE_NOT_FETCHED" in classes["ProblemKind"][1]
    assert T.MatchStatus.HELD.value == "held"
    assert [m.name for m in T.MatchStatus][-1] == "HELD"
    assert classes["Destination"][1][0] == "library_root"
    # ccfa4a8 / 2b82859: structured Problem detail, new kinds appended last (match by value, never position)
    assert classes["Problem"][1] == ["kind", "message", "paths", "disc_conflicts", "layout_reason",
                                     "blocking_subdirs", "loose_files", "folder_mismatches", "pin_conflicts"]
    assert {"DiscConflict", "DiscChoice", "DiscFolderMismatch"} <= set(classes)
    assert classes["ProblemKind"][1][-6:] == ["DISC_AMBIGUOUS", "LAYOUT_AMBIGUOUS", "DISC_FOLDER_MISMATCH", "DISC_TRACK_COUNT",
                                             "PIN_CONFLICT", "ENCODING_DROPPED"]   # c2fcf25, tag batch
    assert all(m.value == m.name.lower() for m in T.ProblemKind)
    for name, (kind, members) in classes.items():
        mine = getattr(T, name)
        if kind == "enum" and name in partial:      # "class X(Enum): ...; NEW" lists only additions
            assert set(members) <= {m.name for m in mine}, name
        elif kind == "enum":
            assert [m.name for m in mine] == members, name
        elif kind == "signals":
            assert all(hasattr(mine, s) for s in members), name
        else:
            assert [f.name for f in dataclasses.fields(mine)] == members, name
    assert {m.value for m in T.AlbumSort} == {m.name.lower() for m in T.AlbumSort}


@pytest.mark.skipif(not CONTRACT.is_file(), reason="contract not present")
def test_mock_namespaces_cover_contract_functions(fake):
    import inspect
    import re
    calls = re.findall(r"^(library|match|provider|plan|journal|templates|settings|jobs)\.(\w+)\(([^)]*)\)",
                       _contract_code(), re.M)
    assert len(calls) >= 20
    for ns, fn, args in calls:
        f = getattr(getattr(fake, ns), fn)
        want = [a.split(":")[0].split("=")[0].strip().lstrip("*") for a in args.split(",") if a.strip()]
        params = inspect.signature(f).parameters
        got = list(params)
        keywords_only = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
        if want and not args.strip().startswith("**") and not keywords_only:   # settings.set(x=...): an example
            assert got[:len(want)] == want, f"{ns}.{fn}: contract {want} vs mock {got}"
    # the adapter's required list is a subset of the contract
    names = {f"{ns}.{fn}" for ns, fn, _ in calls}
    assert {f"{ns}.{fn}" for ns, fns in B.REQUIRED.items() if ns != "events" for fn in fns} <= names


def _contract_presets():
    import re
    rows = re.findall(r"^\| `([\w-]+)` \| ([^|]+?) \| `([^`]+)` \| `([^`]+)` \|$", CONTRACT.read_text(), re.M)
    return [(i, n.strip(), t, ex) for i, n, t, ex in rows]


# Inputs for each contract example (the example strings themselves come from the contract file).
GOLDEN_INPUT = {
    "archivist": dict(albumartist="Black Flag", leadyear="1979", album="Nervous Breakdown", reissueyear="1988",
                      catno="SSTCD001", position="1", tracknumber="1", title="Nervous Breakdown"),
    "simple": dict(albumartist="Black Flag", album="My War", originalyear="1984", tracknumber="1", title="My War"),
    "edition-detail": dict(albumartist="Black Flag", album="Nervous Breakdown", originalyear="1979",
                           reissueyear="1988", catno="SSTCD001", format="CD", tracknumber="1",
                           title="Nervous Breakdown"),
    "label-archive": dict(label="SST Records", catno="SST023", albumartist="Black Flag", album="My War",
                          leadyear="1984", position="1", tracknumber="1", title="My War"),
}


@pytest.mark.skipif(not CONTRACT.is_file(), reason="contract not present")
def test_builtin_presets_match_contract_verbatim_and_render_its_examples(fake):
    from mm_ui.fake_contract import render_segments
    rows = _contract_presets()
    assert [r[0] for r in rows] == ["archivist", "simple", "edition-detail", "label-archive"]
    mine = fake.templates.list()
    assert [(t.id, t.name, t.text, t.builtin) for t in mine[:4]] == [(i, n, t, True) for i, n, t, _ in rows]
    ed = next(t for t in mine if t.id == "edition-detail").text
    assert "\u2044" in ed and "\u2044" in rows[2][2]            # U+2044 FRACTION SLASH, verbatim
    for tid, _n, text, example in rows:
        out = "/".join(render_segments(text, GOLDEN_INPUT[tid])) + ".flac"   # engine adds the extension
        assert out == example, tid
        assert len(Path(out).parts) == 3, tid                      # ⁄ never becomes a separator


def test_fraction_slash_is_never_a_separator(fake):
    from mm_ui.fake_contract import render_segments
    segs = render_segments("%albumartist%/%album% \u2044 %title%/x", dict(albumartist="A", album="B \u2044 C",
                                                                          title="D"))
    assert segs == ["A", "B \u2044 C \u2044 D", "x"]
    assert render_segments("%album%/x", dict(album="AC/DC")) == ["AC-DC", "x"]       # ASCII / in values is not


def test_sdlc_archivist_folder_goldens(fake):
    from mm_ui.fake_contract import BUILTINS, render_segments
    arch = BUILTINS[0].text
    cases = [  # SDLC-project-plan.md §4.2 "Archivist folder examples"
        (dict(leadyear="1979", album="Nervous Breakdown", reissueyear="1988", catno="SSTCD001"),
         "Black Flag/[1979]  Nervous Breakdown [1988] [SSTCD001]"),
        (dict(leadyear="1984", album="My War", catno="SST023"), "Black Flag/[1984]  My War [SST023]"),
        (dict(leadyear="1983", album="The First Four Years", reissueyear="1990"),
         "Black Flag/[1983]  The First Four Years [1990]"),
        (dict(leadyear="2013", album="What The…"), "Black Flag/[2013]  What The…"),
    ]
    for f, want in cases:
        segs = render_segments(arch, dict(f, albumartist="Black Flag", position="1", title="t"))
        assert "/".join(segs[:-1]) == want


def test_picard_functions(fake):
    from mm_ui.fake_contract import render_segments
    f = dict(albumartist="Black Flag", album="Damaged", catno="", label="SST", title="Rise Above")
    assert render_segments("$if(%catno%,has,none)/$if2(%catno%,%label%)/$left(%album%,3)/$upper(%label%)"
                           "/$lower(%title%)", f) == ["none", "SST", "Dam", "SST", "rise above"]
    with pytest.raises(fake.types.ValidationError) as e:
        fake.templates.preview("$nope(%album%)", [1])
    assert e.value.field == "nope"          # real service: the bare function name (API-DEVIATIONS #9)


@pytest.mark.skipif(not CONTRACT.is_file(), reason="contract not present")
def test_albums_descending_parameter_matches_contract(fake):
    import inspect
    p = inspect.signature(fake.library.albums).parameters
    assert list(p) == ["filter", "sort", "offset", "limit", "descending"] and p["descending"].default is False
    assert "descending: bool = False" in _contract_code()


# ---------------------------------------------------------------- %catno% / %catno_raw%
def _pyc(root):
    """Bytecode THIS interpreter would write (cache tag, e.g. cpython-313). Bassline works in the checkout
    concurrently with another Python (cpython-312), so only our own tag proves we wrote nothing."""
    tag = sys.implementation.cache_tag
    return {(p, p.stat().st_mtime_ns) for p in root.rglob(f"*.{tag}*.pyc")}


def _ours_in(root):
    """Files the service or the UI would create if pointed at the checkout by mistake."""
    names = ("library.db", "config.toml", "folder.jpg")
    return [p for n in names for p in root.rglob(n)] + list(root.rglob("journals"))


BASSLINE_NAMING = BASSLINE / "src" / "music_manager" / "naming" / "__init__.py"


def _bassline_format_catalog():
    """Compile ONLY CATNO_STRIP + format_catalog from src/music_manager/naming/__init__.py, in memory
    (no import, no bytecode)."""
    import ast
    import re
    src = BASSLINE_NAMING.read_text(encoding="utf-8")
    tree = ast.parse(src)
    keep = [n for n in tree.body if (isinstance(n, ast.FunctionDef) and n.name == "format_catalog") or
            (isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "CATNO_STRIP" for t in n.targets))]
    assert len(keep) == 2
    ns = {"re": re}
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(BASSLINE_NAMING), "exec"), ns)
    return ns["format_catalog"], ns["CATNO_STRIP"]


CATNO_CASES = ["SST 023", "sst 023", "SST CD 007", "SST 007; SST 007CD", "  none ", "[None]", "", None,
               'A/B:C*D?E"F<G>H|I_J\u22c5K', "tab\tX", "back\\slash", "Ünïcode 12-3", "SST-023 ", "nbsp\u00a0X"]


@pytest.mark.skipif(not BASSLINE_NAMING.is_file(), reason="Bassline's checkout not present")
def test_catno_mirrors_bassline_format_catalog():
    from mm_ui.fake_contract import CATNO_STRIP, format_catalog
    before, pyc_before = tree(BASSLINE), _pyc(BASSLINE)
    theirs, their_strip = _bassline_format_catalog()
    assert CATNO_STRIP.pattern == their_strip.pattern
    for c in CATNO_CASES:
        assert format_catalog(c) == (theirs(c) or ""), repr(c)
    assert _pyc(BASSLINE) == pyc_before and _ours_in(BASSLINE) == []


@pytest.mark.skipif(not CONTRACT.is_file(), reason="contract not present")
def test_catno_and_catno_raw_match_contract_text():
    import re
    from mm_ui.fake_contract import CATNO_STRIP, CONTRACT_FIELDS, FIELDS, MOCK_ONLY_FIELDS, catno_raw, format_catalog
    text = CONTRACT.read_text(encoding="utf-8")
    m = re.search(r"`%catno%` \(the default: the first catalog number, uppercased,\s+with spaces and `([^`]+)` "
                  r"removed, so `([^`]+)` becomes `([^`]+)`", text)
    assert m, "contract %catno% wording changed"
    chars, example_in, example_out = m.groups()
    assert set(CATNO_STRIP.pattern[1:-1]) == set(chars) | {" "}        # "spaces" = U+0020, as in the code
    assert format_catalog(example_in) == example_out == "SST023"
    raw = re.search(r"`%catno_raw%` \(the first catalog number exactly as Discogs lists it, e\.g\. `([^`]+)`;"
                    r"\s+only characters\s+not allowed in file names are replaced, with `(.)`\)", text)
    assert raw, "contract %catno_raw% wording changed"
    assert catno_raw(raw.group(1)) == raw.group(1) == "SST 023" and raw.group(2) == "_"
    assert catno_raw("SST 023; SST 023CD") == "SST 023"                 # first catalog number only
    fields_para = text[text.index("Fields: `%albumartist%`"):text.index("Functions: `$if`")]
    contract_fields = set(re.findall(r"`%(\w+)%`", fields_para))
    assert contract_fields == CONTRACT_FIELDS == FIELDS and MOCK_ONLY_FIELDS == set()


def format_catalog_ok():
    from mm_ui.fake_contract import format_catalog
    return format_catalog("sst 023; x") == "SST023"


def test_catno_vs_catno_raw_in_preview(fake):
    from mm_ui.fake_contract import catno_raw
    aid = fake.albums({"MATCHED"}, limit=10).items[1].id                # My War, Discogs catno "SST 023"
    assert fake.match.candidates(aid)[0].catno == "SST 023"
    clean = fake.templates.preview("%albumartist%/[%catno%] %album%/%title%", [aid])
    raw = fake.templates.preview("%albumartist%/[%catno_raw%] %album%/%title%", [aid])
    assert clean.moves[0][1].parent.name == "[SST023] My War"
    assert raw.moves[0][1].parent.name == "[SST 023] My War"
    assert catno_raw('SST/023: "a"') == 'SST_023_ _a_'                # only filename-illegal chars replaced
    assert catno_raw("  [None] ") == "" and format_catalog_ok()
