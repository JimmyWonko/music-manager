"""Integration tests against Bassline's real music_manager.service (MM_BACKEND=real).

Runs only with MM_BACKEND=real (skipped otherwise); then it fails if the checkout or its dependencies (requests, mutagen) or ffmpeg aren't available.
Everything lives in tmp_path: config, SQLite index, Discogs cache, journals, trash and the music
itself (synthetic FLAC/MP3 made with ffmpeg). Discogs is a stand-in passed through
service.configure(discogs=...); sockets are blocked and every connect attempt fails the test.
The checkout is read-only: no bytecode, no new files (checked before/after).
"""
from __future__ import annotations

import copy
import importlib.util
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog

from ui_env import BACKEND_PATH, BASSLINE, SUITE_BACKEND   # MM_TEST_BASSLINE overrides (e.g. a git-archive of a pinned commit)
from mm_ui import backend as B

SRC = BASSLINE / "src"
_MISSING = [why for bad, why in (
    (not (SRC / "music_manager" / "service").is_dir(), f"real service checkout not present at {SRC}"),
    (shutil.which("ffmpeg") is None, "ffmpeg needed for synthetic audio"),
    (any(importlib.util.find_spec(m) is None for m in ("requests", "mutagen")), "requests/mutagen not installed"),
) if bad]
if _MISSING and SUITE_BACKEND == "real":
    raise RuntimeError("MM_BACKEND=real but the real backend can't run: " + "; ".join(_MISSING))
pytestmark = [pytest.mark.real_backend, pytest.mark.skipif(bool(_MISSING), reason="; ".join(_MISSING))]


def snapshot(root: Path):
    """What WE could write into the checkout: bytecode with this interpreter's cache tag, and service
    state files. (Bassline edits the checkout concurrently, so a whole-tree compare would be flaky.)"""
    tag = sys.implementation.cache_tag
    pyc = sorted((str(p), p.stat().st_mtime_ns) for p in root.rglob(f"*.{tag}*.pyc"))
    state = sorted(str(p) for n in ("library.db", "config.toml", "folder.jpg", "journals") for p in root.rglob(n))
    return pyc, state


# ---------------------------------------------------------------- synthetic Discogs + music
def tone(path: Path, seconds: int = 1):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                    "-t", str(seconds), "-y", str(path)], check=True)


def release(rid, artist, title, year, catno, tracks, label="Synth Label", fmt="CD"):
    return {"id": rid, "master_id": 0, "title": title, "year": year,
            "artists": [{"name": artist, "anv": "", "join": ""}],
            "labels": [{"name": label, "catno": catno}], "formats": [{"name": fmt, "qty": "1"}],
            "tracklist": [{"position": str(i), "type_": "track", "title": t, "duration": "0:01"}
                          for i, t in enumerate(tracks, 1)],
            "images": [{"type": "primary", "uri": f"https://img.example/{rid}.jpg",
                        "uri150": f"https://img.example/{rid}-150.jpg"}]}


def search_result(rel):
    return {"id": rel["id"], "title": f"{rel['artists'][0]['name']} - {rel['title']}", "year": str(rel["year"]),
            "catno": rel["labels"][0]["catno"], "label": [rel["labels"][0]["name"]],
            "format": [rel["formats"][0]["name"]], "country": "US", "master_id": 0,
            "thumb": rel["images"][0]["uri150"], "cover_image": rel["images"][0]["uri"]}


class FakeDiscogs:
    """Same methods the service uses on providers.discogs.Discogs; records every call."""

    def __init__(self, releases):
        self.releases = {r["id"]: r for r in releases}
        self.calls = []
        self.on_wait = None

    def search_releases(self, artist, album, catno="", year=""):
        self.calls.append(("search", album))
        return [search_result(r) for r in self.releases.values() if r["title"].casefold() == (album or "").casefold()]

    def release(self, release_id):
        self.calls.append(("release", release_id))
        return copy.deepcopy(self.releases[int(release_id)])

    def master(self, master_id):
        self.calls.append(("master", master_id))
        return {}

    def image(self, url):
        self.calls.append(("image", url))
        return b"\xff\xd8\xff synthetic jpeg"


@pytest.fixture
def net_calls(monkeypatch):
    calls = []

    def blocked(self, addr, *a, **k):
        calls.append(addr)
        raise OSError(f"network blocked in tests: {addr}")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", lambda addr, *a, **k: blocked(None, addr))
    yield calls
    assert calls == [], f"unexpected network calls: {calls}"


@pytest.fixture
def real(qapp, tmp_path, monkeypatch, net_calls):
    """The real backend via the UI adapter, configured into tmp_path with a synthetic library."""
    before = snapshot(BASSLINE)
    for k, sub in (("XDG_CONFIG_HOME", "xdg-config"), ("XDG_DATA_HOME", "xdg-data"),
                   ("XDG_STATE_HOME", "xdg-state"), ("XDG_CACHE_HOME", "xdg-cache")):
        monkeypatch.setenv(k, str(tmp_path / sub))
    monkeypatch.setenv("MM_BACKEND", "real"); monkeypatch.setenv("MM_BACKEND_PATH", str(BACKEND_PATH))
    monkeypatch.setenv("MM_DATA_DIR", str(tmp_path / "data")); monkeypatch.setenv("MM_CONFIG_DIR", str(tmp_path / "config"))
    svc = B.get_service()
    assert svc.kind == "real"
    # 4504848/f74133c: journals, Discogs cache, token and trash follow configure()'s dirs and are read through
    # state_dir() / cache_dir() / token_path() / trash_root(); the module constants aren't patched any more

    lib = tmp_path / "lib"
    flac = lib / "Synth Band" / "[1990]  Test Album [SYN001]"
    for i, t in enumerate(["Alpha", "Beta", "Gamma"], 1):
        tone(flac / f"{i:02d} {t}.flac")
    mp3 = lib / "Other Band" / "[1995]  Short Record [OTH002]"
    for i, t in enumerate(["One", "Two"], 1):                          # Discogs lists 3: track_mismatch
        tone(mp3 / f"{i:02d} {t}.mp3")
    rels = [release(501, "Synth Band", "Test Album", 1990, "SYN 001", ["Alpha", "Beta", "Gamma"]),
            release(502, "Other Band", "Short Record", 1995, "OTH 002", ["One", "Two", "Three"])]
    discogs = FakeDiscogs(rels)
    svc._c.configure(data_dir=tmp_path / "data", config_dir=tmp_path / "config", discogs=discogs)  # the hook
    paths = engine_paths()
    assert all(_inside(pth, tmp_path) for pth in paths.values()), paths     # nothing under ~
    svc.settings.set(library_roots=[lib])
    job = svc.library.scan()
    assert svc._c.jobs.wait(job.id).state.name == "DONE"
    rows = svc.albums(None, limit=10).items
    job = svc.match.find([r.id for r in rows])
    assert svc._c.jobs.wait(job.id).state.name == "DONE"
    by_title = {r.title: r for r in svc.albums(None, limit=10).items}
    yield dict(svc=svc, lib=lib, flac=flac, mp3=mp3, discogs=discogs, ids=by_title, tmp=tmp_path)
    svc._c.reset()
    assert snapshot(BASSLINE) == before, "we wrote into Bassline's checkout"


def engine_paths() -> dict:
    """Where the engine keeps journals, the Discogs cache, the token and the trash (f74133c accessors)."""
    j, d, f = (sys.modules[m] for m in ("music_manager.journal", "music_manager.providers.discogs",
                                        "music_manager.fs"))
    return {"state_dir": j.state_dir(), "cache_dir": d.cache_dir(), "token_path": d.token_path(),
            "trash_root": f.trash_root()}


def _inside(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def patch_exec(monkeypatch, cls, result):
    seen = []
    monkeypatch.setattr(cls, "exec", lambda self: seen.append(self) or result)
    return seen


# ---------------------------------------------------------------- tests
def test_real_scan_find_statuses_and_types(real):
    svc, ids = real["svc"], real["ids"]
    assert set(ids) == {"Test Album", "Short Record"}
    assert all(isinstance(m.value, str) for m in svc.types.MatchStatus)
    a = svc.library.album(ids["Test Album"].id)
    assert a.track_count == 3 and a.status.name in ("MATCHED", "NEEDS_REVIEW")
    c = svc.match.candidates(a.id)[0]
    assert c.release_id == 501 and c.fetched and c.artwork is None          # never downloaded (deviation #7)
    assert [p.method for p in c.alignment] == ["number"] * 3
    assert ("search", "Test Album") in real["discogs"].calls


def test_real_auto_accept_unfetched_is_not_a_winner(real):
    import dataclasses
    svc = real["svc"]
    aid = real["ids"]["Test Album"].id
    cands = svc.match.candidates(aid)
    rules = svc.match.auto_rules()
    assert svc.match.auto_accept(cands, rules, local_track_count=3).release_id == 501
    unfetched = [dataclasses.replace(cands[0], alignment=None, fetched=False)] + cands[1:]
    assert svc.match.auto_accept(unfetched, rules, local_track_count=3) is None


def test_real_template_unknown_function_field(real):
    svc = real["svc"]
    with pytest.raises(svc.types.ValidationError) as e:
        svc.templates.preview("%albumartist%/$nope(%album%)/%title%", [real["ids"]["Test Album"].id])
    assert e.value.field == "nope"


def test_real_review_apply_then_history_undo(real, qtbot, monkeypatch):
    """build (off the UI thread) -> apply -> undo through the UI on synthetic FLACs; files and tags on disk."""
    import mutagen
    from mm_ui.history import HistoryDialog
    from mm_ui.review import ApplyDialog, ReviewWindow
    svc, flac = real["svc"], real["flac"]
    aid = real["ids"]["Test Album"].id
    svc.match.choose(aid, 501)
    before_names = sorted(p.name for p in flac.iterdir())
    w = ReviewWindow(svc); qtbot.addWidget(w)
    seen = patch_exec(monkeypatch, ApplyDialog, QDialog.Accepted)
    summaries = []
    monkeypatch.setattr(ReviewWindow, "show_summary", lambda self, t: summaries.append(t))
    with qtbot.waitSignal(svc.events.job_finished, timeout=20000) as sig:
        w.open_apply()
    (dlg,) = seen
    assert [ap.album_id for ap in dlg.clean_aps] == [aid]
    assert any("folder.jpg will be saved" in x for x in dlg.plan.warnings)
    assert "folder.jpg will be saved" in dlg.warnings_label.text()
    assert sig.args[0].state.name == "DONE" and sig.args[0].kind.name == "APPLY"
    qtbot.waitUntil(lambda: bool(summaries), timeout=5000)
    assert summaries[0].startswith("Applied 1 album.")
    after = sorted(p.name for p in flac.iterdir())
    assert after == ["1. Alpha.flac", "2. Beta.flac", "3. Gamma.flac", "folder.jpg"]
    assert mutagen.File(flac / "1. Alpha.flac", easy=True)["album"] == ["Test Album"]
    assert svc.library.album(aid).status.name == "APPLIED"

    h = HistoryDialog(svc); qtbot.addWidget(h)
    op = h.current()
    assert op.kind == "apply" and h.status_text(op) == "Can undo"
    useen = patch_exec(monkeypatch, ApplyDialog, QDialog.Accepted)
    with qtbot.waitSignal(svc.events.job_finished, timeout=20000) as sig:
        h.preview_undo()
    assert useen and not useen[0].blocking and sig.args[0].kind.name == "UNDO"
    assert sig.args[0].state.name == "DONE", sig.args[0].message
    assert sorted(p.name for p in flac.iterdir()) == before_names
    assert "album" not in (mutagen.File(flac / "01 Alpha.flac", easy=True) or {})
    # folder.jpg went to the configured trash (trash_root(): <data_dir>/Trash since 4504848), inside tmp_path
    trash = engine_paths()["trash_root"]
    assert _inside(trash, real["tmp"]) and (trash / "files").is_dir() and any((trash / "files").iterdir()), trash
    # undo preview: the moved FLACs are kind "extra" (0666aad) or "track" (fixed); either way shown as tracks
    up = useen[0].plan
    kinds = {f.kind for ap in up.albums for f in ap.files if f.path_before.suffix == ".flac"}
    assert kinds and kinds <= {"extra", "track"}, kinds
    top = useen[0].tree.topLevelItem(0)
    shown = {top.child(i).text(1): top.child(i).text(0) for i in range(top.childCount())}
    assert all(v == "track" for k, v in shown.items() if k.endswith(".flac")), shown
    assert shown.get("folder.jpg") == "extra"


def test_real_track_mismatch_holds_then_apply_anyway(real, qtbot, monkeypatch):
    from mm_ui import problems as P
    from mm_ui.review import ApplyDialog, ReviewWindow
    svc, mp3 = real["svc"], real["mp3"]
    aid = real["ids"]["Short Record"].id
    svc.match.choose(aid, 502)
    plan = svc.plan.build([aid])
    kinds = {p.kind.value for p in plan.albums[0].problems}
    assert kinds == {"track_mismatch"}             # 2 local files, Discogs lists 3
    w = ReviewWindow(svc); qtbot.addWidget(w)
    seen = patch_exec(monkeypatch, ApplyDialog, QDialog.Rejected)
    with qtbot.waitSignal(w.plan_flow_done, timeout=20000):
        w.open_apply()
    assert seen[0].held_aps and not seen[0].ok_btn.isEnabled()
    assert seen[0].anyway_btn is not None
    # the real apply holds it (HELD + held_for), which the review screen shows as "Needs a look"
    # nothing applicable: 0666aad runs a Job that ends FAILED ("Applied 0 album(s); refused 1 …");
    # Bassline's coming change raises MMError from plan.apply right away. The UI handles both.
    try:
        job = svc._c.jobs.wait(svc.plan.apply(plan.id).id)
        raised = None
    except svc.types.MMError as e:
        job, raised = None, e
    real["apply_refused"] = (raised, job)
    if raised is None:                                # 0666aad
        assert job.state.name == "FAILED" and "refused 1" in job.message, job
        assert P.nothing_applied(job.message) and P.refused_count(job.message) == 1
        w.show_apply_failed(job, 0)
        assert w.last_apply_message[0] == "Nothing was applied" and "All 1 album(s)" in w.last_apply_message[1]
    else:                                             # a06a7c9: MMError before any job; held albums still HELD
        assert "Nothing to apply" in str(raised)
        w.show_apply_error(raised, seen[0])
        title, text, detail = w.last_apply_message
        assert title == "Nothing to apply" and "1 held back for a look" in text and "Nothing to apply" in detail
    a = svc.library.album(aid)
    assert a.status.name == "HELD" and {p.kind.value for p in a.held_for} == kinds
    w.reload(); w.filter_box.setCurrentText("Needs a look")
    assert w.model.rowCount() == 1 and "Needs a look" in w.model.index(0).data()
    assert "1 need a look" in w.queue_label.text()
    if True:
        p2 = svc.plan.build([aid], allow_track_mismatch={aid})
        assert not p2.albums[0].problems
        assert svc.library.album(aid).status.name == "HELD"            # build stays read-only
        job = svc._c.jobs.wait(svc.plan.apply(p2.id).id)
        assert job.state.name == "DONE", job.message
        assert svc.library.album(aid).status.name == "APPLIED" and svc.library.album(aid).held_for == []
        assert (mp3 / "1. One.mp3").exists()


def test_real_events_reach_the_ui_thread(real, qtbot):
    """Signals are emitted from the service's worker thread; an AutoConnection slot runs on the Qt thread."""
    import threading
    svc = real["svc"]
    got = []

    from PySide6.QtCore import QObject, Slot

    class Receiver(QObject):
        @Slot(object)
        def on(self, job):
            got.append((job.kind.name, threading.current_thread() is threading.main_thread()))
    r = Receiver()
    svc.events.job_finished.connect(r.on)
    with qtbot.waitSignal(svc.events.job_finished, timeout=20000):
        svc.library.scan()
    qtbot.waitUntil(lambda: bool(got), timeout=5000)
    assert got[0] == ("SCAN", True)
    svc.events.job_finished.disconnect(r.on)


# ---------------------------------------------------------------- 0666aad: background release fetching
def _needs_fetch_job(svc):
    import dataclasses
    if "fetch_job" not in {f.name for f in dataclasses.fields(svc.types.Plan)}:
        pytest.skip("service predates Plan.fetch_job (1d70271)")


def _age_releases(monkeypatch, seconds):
    """Make every stored release look `seconds` older (contract: > 6 h counts as not fetched). Patches the
    `time` name inside music_manager.service._releases only, in memory."""
    import time as _time
    import types
    rel = sys.modules["music_manager.service._releases"]
    monkeypatch.setattr(rel, "time", types.SimpleNamespace(**{k: getattr(_time, k) for k in dir(_time)
                                                              if not k.startswith("_")}))
    monkeypatch.setattr(rel.time, "time", lambda: _time.time() + seconds)


def test_real_plan_build_never_fetches_and_reuses_fetch_job(real, monkeypatch):
    """(1) RELEASE_NOT_FETCHED, (2) Plan.fetch_job last field + same id while running, (4) no Discogs in build."""
    import dataclasses
    from mm_ui import problems as P
    svc = real["svc"]; _needs_fetch_job(svc)
    T = svc.types
    assert T.ProblemKind.RELEASE_NOT_FETCHED.value == "release_not_fetched"
    assert [f.name for f in dataclasses.fields(T.Plan)][-1] == "fetch_job"
    aid = real["ids"]["Test Album"].id
    svc.match.choose(aid, 501)
    assert svc.plan.build([aid]).fetch_job is None                 # stored by match.find: nothing to fetch
    _age_releases(monkeypatch, 6 * 3600 + 60)
    discogs = real["discogs"]
    gate = __import__("threading").Event()
    orig = discogs.release
    discogs.release = lambda rid: gate.wait(10) and orig(rid)       # hold the job so it stays running
    n_calls = len(discogs.calls)
    p1 = svc.plan.build([aid])
    p2 = svc.plan.build([aid])
    (ap,) = p1.albums
    assert [p.kind.value for p in ap.problems] == ["release_not_fetched"] and ap.files == []
    assert P.waiting(ap) and not P.held(ap)
    assert p1.fetch_job is not None and p2.fetch_job == p1.fetch_job
    assert len(discogs.calls) == n_calls                            # build itself never calls Discogs
    assert svc.jobs.get(p1.fetch_job).kind.name == "FETCH_CANDIDATES"
    gate.set()
    job = svc._c.jobs.wait(p1.fetch_job, 20)
    assert job.state.name == "DONE", job.message
    assert ("release", 501) in discogs.calls[n_calls:]
    p3 = svc.plan.build([aid])
    assert p3.fetch_job is None and not p3.albums[0].problems and p3.albums[0].files


def test_real_review_waits_for_fetch_job_then_rebuilds(real, qtbot, monkeypatch):
    """(2) The review screen shows progress, waits for the fetch job's job_finished and builds again."""
    from mm_ui.review import ApplyDialog, ReviewWindow
    svc = real["svc"]; _needs_fetch_job(svc)
    aid = real["ids"]["Test Album"].id
    svc.match.choose(aid, 501)
    _age_releases(monkeypatch, 6 * 3600 + 60)
    builds = []
    orig = svc.plan.build
    monkeypatch.setattr(svc.plan, "build", lambda *a, **k: builds.append(orig(*a, **k)) or builds[-1])
    w = ReviewWindow(svc); qtbot.addWidget(w)
    seen = patch_exec(monkeypatch, ApplyDialog, QDialog.Rejected)
    with qtbot.waitSignal(w.plan_flow_done, timeout=20000):
        w.open_apply()
    assert [b.fetch_job is not None for b in builds] == [True, False]
    (d,) = seen
    assert [ap.album_id for ap in d.clean_aps] == [aid] and not d.waiting_aps


def test_real_jobs_wait_returns_after_job_finished(real):
    """(3) 0666aad: jobs.wait returns only after job_finished was emitted (a direct slot has run)."""
    svc = real["svc"]
    got = []
    from PySide6.QtCore import Qt
    slot = lambda j: got.append(j.id)   # noqa: E731
    svc.events.job_finished.connect(slot, Qt.DirectConnection)      # runs in the service's worker thread
    try:
        for _ in range(5):
            job = svc.library.scan()
            svc._c.jobs.wait(job.id, 20)
            assert job.id in got
    finally:
        svc.events.job_finished.disconnect(slot)


# ---------------------------------------------------------------- UI polish on the real service
def test_real_apply_dialog_counts_warnings_over_applying_albums_only(real, qtbot):
    """Real plan.build counts held albums in "folder.jpg will be saved … for N album(s)" and adds a
    "refused (held)" line; the dialog counts only albums that apply and leaves held ones to the held box."""
    from mm_ui.review import ApplyDialog
    svc, ids = real["svc"], real["ids"]
    good, short = ids["Test Album"].id, ids["Short Record"].id
    svc.match.choose(good, 501); svc.match.choose(short, 502)
    plan = svc.plan.build([good, short])
    assert any("will be refused (held)" in w for w in plan.warnings)
    assert "folder.jpg will be saved from Discogs for 2 album(s)" in plan.warnings, plan.warnings
    d = ApplyDialog(plan); qtbot.addWidget(d)
    assert d.warnings == ["folder.jpg will be saved from Discogs for 1 album(s)"], d.warnings
    rows = [d.tree.topLevelItem(i) for i in range(d.tree.topLevelItemCount())]
    assert [r.text(0) for r in rows] == ["Synth Band – Test Album", "Held back: Other Band – Short Record"]
    assert rows[1].childCount() == 0 and "stays in place" in rows[1].text(2)


def test_real_library_footer_label_is_short(real, qtbot):
    from mm_ui.library import FOLDER_COL, LibraryWindow
    svc = real["svc"]
    w = LibraryWindow(svc); qtbot.addWidget(w)
    assert "backend: music_manager.service" in w.info.text() and "/src/" not in w.info.text()
    assert svc.origin in w.info.toolTip()
    assert w.model.index(0, FOLDER_COL).data(Qt.ToolTipRole) == str(w.model.rows[0].folder)


def test_real_engine_paths_follow_configure(real):
    """f74133c: configure(data_dir, config_dir) moves journals, cache, token and trash out of the home folder."""
    p, tmp = engine_paths(), real["tmp"]
    assert p["state_dir"] == tmp / "data" / "state"
    assert p["cache_dir"] == tmp / "data" / "cache" / "discogs"
    assert p["trash_root"] == tmp / "data" / "Trash"
    assert p["token_path"] == tmp / "config" / "discogs-token"


def test_real_plans_name_release_artist_title(real, qtbot):
    """e1eca5e: AlbumPlan.artist/title from the release in plan.build, and from the journal in undo_plan;
    the apply dialog and undo preview show them instead of "Album not in the library index"."""
    from mm_ui.history import HistoryDialog
    from mm_ui.review import ApplyDialog
    svc = real["svc"]
    aid = real["ids"]["Test Album"].id
    svc.match.choose(aid, 501)
    plan = svc.plan.build([aid])
    (ap,) = plan.albums
    assert (ap.artist, ap.title, ap.release_id) == ("Synth Band", "Test Album", 501)
    assert svc._c.jobs.wait(svc.plan.apply(plan.id).id).state.name == "DONE"
    h = HistoryDialog(svc); qtbot.addWidget(h)
    up = svc.journal.undo_plan(h.current().id)
    (uap,) = up.albums
    assert (uap.artist, uap.title, uap.release_id) == ("Synth Band", "Test Album", 501)
    d = ApplyDialog(up, undo=True, album_name=lambda _id: "Album not in the library index"); qtbot.addWidget(d)
    top = d.tree.topLevelItem(0)
    assert top.text(0) == "Synth Band – Test Album" and "Discogs release 501" in top.toolTip(0)


def test_real_accept_clear_winners_ranks_by_auto_score(real):
    """4dc5e69: Candidate.score adds track count and durations once fetched; auto_accept ranks by auto_score."""
    svc = real["svc"]
    aid = real["ids"]["Short Record"].id            # 2 local files, Discogs lists 3: track count 0
    c = svc.match.candidates(aid)[0]
    assert c.fetched and c.auto_score is not None and c.score < c.auto_score
    parts = {p.field: p for p in c.breakdown}
    assert parts["track_count"].value == 0.0 and abs(sum(p.weight * p.value for p in c.breakdown) - c.score) < 1e-6
    good = svc.match.candidates(real["ids"]["Test Album"].id)[0]
    assert {p.field: p for p in good.breakdown}["track_count"].value == 1.0


# ---------------------------------------------------------------- multi-disc pairing (5231769 / fc27fcf)
DISC_TRACKS = [("1-1", "One"), ("1-2", "Two"), ("1-3", "Three"), ("2-1", "Four"), ("2-2", "Five")]
DISC_LAYOUTS = {   # relpath (no extension), disc number tag or None
    "Tagged Discs": [("01 One", 1), ("02 Two", 1), ("03 Three", 1), ("01 Four", 2), ("02 Five", 2)],
    "Folder Discs": [("CD1/01 One", None), ("CD1/02 Two", None), ("CD1/03 Three", None),
                     ("CD2/01 Four", None), ("CD2/02 Five", None)],
    "Named Discs": [("1-01 One", None), ("1-02 Two", None), ("1-03 Three", None), ("2-01 Four", None),
                    ("2-02 Five", None)],
    "Titled Discs": [("01 One", None), ("02 Two", None), ("03 Three", None), ("01 Four", None), ("02 Five", None)],
    "Lost Discs": [("01 a", None), ("02 b", None), ("03 c", None), ("01 d", None), ("02 e", None)],
}


def _two_disc_album(real, title, rid):
    """Synthetic 2-disc album (disc 1 = 1..3, disc 2 = 1..2: overlapping numbers) + its Discogs release,
    scanned and matched. Returns (album id, album folder)."""
    import mutagen
    svc = real["svc"]
    folder = real["lib"] / "Disc Band" / f"[1999]  {title} [DSC{rid}]"
    for rel, disc in DISC_LAYOUTS[title]:
        path = folder / f"{rel}.flac"
        tone(path)
        if disc is not None:
            f = mutagen.File(path, easy=True); f["discnumber"] = str(disc); f.save()
    r = release(rid, "Disc Band", title, 1999, f"DSC {rid}", [t for _p, t in DISC_TRACKS])
    for tr, (pos, _t) in zip(r["tracklist"], DISC_TRACKS):
        tr["position"] = pos
    r["formats"] = [{"name": "CD", "qty": "2"}]
    real["discogs"].releases[rid] = r
    job = svc.library.scan()
    assert svc._c.jobs.wait(job.id).state.name == "DONE"
    (row,) = [x for x in svc.albums(None, limit=50).items if x.title == title]
    job = svc.match.find([row.id])
    assert svc._c.jobs.wait(job.id).state.name == "DONE"
    assert svc.match.candidates(row.id)[0].release_id == rid
    svc.match.choose(row.id, rid)
    return row.id, folder


@pytest.mark.parametrize("title", ["Tagged Discs", "Folder Discs", "Named Discs", "Titled Discs"])
def test_real_multi_disc_pairs_on_disc_and_number(real, title):
    from mm_ui import problems as P
    svc = real["svc"]
    aid, folder = _two_disc_album(real, title, 700 + list(DISC_LAYOUTS).index(title))
    c = svc.match.candidates(aid)[0]
    pairs = {p.local.path.relative_to(folder).as_posix(): (p.remote.disc, p.remote.number, p.remote.title)
             for p in c.alignment if p.local is not None and p.remote is not None}
    assert pairs == {f"{rel}.flac": (int(pos[0]), int(pos[2]), t)
                     for (rel, _d), (pos, t) in zip(DISC_LAYOUTS[title], DISC_TRACKS)}, pairs
    (ap,) = svc.plan.build([aid]).albums
    assert not [p for p in ap.problems if P.kind_of(p) == P.DISC_AMBIGUOUS], ap.problems


def test_real_multi_disc_ambiguous_is_held_and_not_overridable(real, qtbot, monkeypatch):
    """Flat folder, restarting numbers, titles that pick nothing: the engine refuses to guess."""
    from mm_ui import problems as P
    from mm_ui.review import ApplyDialog, ReviewWindow
    svc = real["svc"]
    aid, folder = _two_disc_album(real, "Lost Discs", 709)
    good = real["ids"]["Test Album"].id
    svc.match.choose(good, 501)
    refused = {"01 a.flac", "01 d.flac", "02 b.flac", "02 e.flac"}
    c = svc.match.candidates(aid)[0]
    assert {p.local.path.name for p in c.alignment if p.local is not None and p.method == "unmatched"} == refused
    plan = svc.plan.build([aid, good])
    by = {ap.album_id: ap for ap in plan.albums}
    disc = [p for p in by[aid].problems if P.kind_of(p) == P.DISC_AMBIGUOUS]
    assert len(disc) == 1 and {x.name for x in disc[0].paths} == refused, by[aid].problems
    assert P.held(by[aid]) and not P.overridable(by[aid]) and by[good].problems == []
    assert P.label(disc[0]) == "Can't work out the disc layout"
    # allow_track_mismatch doesn't clear it
    (ap2,) = svc.plan.build([aid], allow_track_mismatch={aid}).albums
    assert [p for p in ap2.problems if P.kind_of(p) == P.DISC_AMBIGUOUS] and P.held(ap2)
    d = svc.match.destination(aid, 709, svc.active_template())
    assert any(P.kind_of(p) == P.DISC_AMBIGUOUS for p in d.problems)
    # the dialog: held back, files listed (relative to the album folder), no "Apply anyway…"
    dlg = ApplyDialog(plan); qtbot.addWidget(dlg)
    assert [ap.album_id for ap in dlg.held_aps] == [aid] and dlg.anyway_btn is None
    assert dlg.ok_btn.isEnabled() and dlg.ok_btn.text() == "Apply 1 album(s)"
    text = "\n".join(dlg.problem_list.item(i).text() for i in range(dlg.problem_list.count()))
    assert "Can't work out the disc layout" in text and "Files (4):" in text
    assert all(f"\n      {n}" in text for n in refused), text
    # the review screen marks the refused rows
    w = ReviewWindow(svc); qtbot.addWidget(w)
    w.filter_box.setCurrentText("All")
    row = next(i for i in range(w.model.rowCount()) if "Lost Discs" in w.model.index(i).data())
    w.list.setCurrentIndex(w.model.index(row))
    marks = [w.table.item(r, 7).text() for r in range(w.table.rowCount()) if w.table.item(r, 7) is not None]
    assert marks.count("disc unknown") == 4, marks
    assert "Can't work out the disc layout" in w.preview.text()
    # apply: the clean album applies, the disc_ambiguous one is held and nothing in it moves
    before = sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*"))
    job = svc._c.jobs.wait(svc.plan.apply(plan.id).id)
    assert job.state.name == "DONE", job.message
    a = svc.library.album(aid)
    assert a.status.name == "HELD" and {p.kind.value for p in a.held_for} == {"disc_ambiguous"}
    assert sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*")) == before
    assert svc.library.album(good).status.name == "APPLIED"
    # on its own: nothing applicable -> MMError right away or a FAILED job (both handled by the UI)
    plan3 = svc.plan.build([aid])
    try:
        job = svc._c.jobs.wait(svc.plan.apply(plan3.id).id)
        assert job.state.name == "FAILED" and P.nothing_applied(job.message), job
    except svc.types.MMError as e:
        assert "Nothing to apply" in str(e)
    assert svc.library.album(aid).status.name == "HELD"


# ---------------------------------------------------------------- loose audio next to subfolders (68cf4d7)
def _layout_library(real):
    """Synthetic folders for the three layouts in API-CONTRACT "Album folders with loose audio and
    subfolders", with Discogs releases so each album gets a candidate; scanned, matched and chosen."""
    svc, lib, dg = real["svc"], real["lib"], real["discogs"]
    art = lib / "Black Flag"
    tone(art / "stray.mp3")
    war = art / "[1984]  My War [SST023]"
    for i, t in enumerate(["My War", "Can't Decide"], 1):
        tone(war / f"{i:02d} {t}.flac")
    mixed = lib / "Mix Band" / "[2001]  Mixed Up [MIX001]"
    tone(mixed / "01 One.flac"); tone(mixed / "CD1" / "02 Two.flac"); tone(mixed / "CD2" / "01 Four.flac")
    main = lib / "Bonus Band" / "[2002]  Main Thing [BON001]"
    tone(main / "01 Main.flac"); tone(main / "Bonus" / "01 Extra.flac")
    for r in (release(801, "Black Flag", "Black Flag", 1984, "X 1", ["Stray"]),
              release(802, "Black Flag", "My War", 1984, "SST 023", ["My War", "Can't Decide"]),
              release(803, "Mix Band", "Mixed Up", 2001, "MIX 001", ["One", "Two", "Four"]),
              release(804, "Bonus Band", "Main Thing", 2002, "BON 001", ["Main"])):
        dg.releases[r["id"]] = r
    job = svc.library.scan()
    assert svc._c.jobs.wait(job.id).state.name == "DONE"
    rows = {Path(r.folder): r for r in svc.albums(None, limit=50).items}
    ids = {k: rows[f].id for k, f in (("stray", art), ("war", war), ("mixed", mixed), ("main", main),
                                      ("bonus", main / "Bonus"))}
    job = svc.match.find([ids[k] for k in ("stray", "war", "mixed", "main")])
    assert svc._c.jobs.wait(job.id).state.name == "DONE"
    for k, rid in (("stray", 801), ("war", 802), ("mixed", 803), ("main", 804)):
        assert rid in [c.release_id for c in svc.match.candidates(ids[k])], k
        svc.match.choose(ids[k], rid)
    return ids, dict(art=art, war=war, mixed=mixed, main=main)


def _tree(folder):
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*"))


def test_real_scan_lists_subfolders_and_keeps_loose_files_apart(real):
    svc = real["svc"]
    ids, f = _layout_library(real)
    a = {k: svc.library.album(v) for k, v in ids.items()}
    assert [t.path.name for t in a["stray"].tracks] == ["stray.mp3"]          # just the loose file
    assert (a["stray"].artist, a["stray"].title, a["stray"].loose_tracks) == ("Black Flag", "", True)   # d6ac2f3
    assert [t.path.name for t in a["war"].tracks] == ["01 My War.flac", "02 Can't Decide.flac"]
    assert [t.path.relative_to(f["mixed"]).as_posix() for t in a["mixed"].tracks] == [
        "01 One.flac", "CD1/02 Two.flac", "CD2/01 Four.flac"]                  # one album: loose first, then discs
    assert [t.path.name for t in a["bonus"].tracks] == ["01 Extra.flac"] and a["bonus"].title == "Bonus"
    assert [t.path.name for t in a["main"].tracks] == ["01 Main.flac"]
    assert a["main"].loose_tracks and a["main"].artist == f["main"].name          # album_folders: loose tracks too
    assert not a["mixed"].loose_tracks and a["mixed"].title == "Mixed Up"           # disc_folders: a real album
    assert not a["war"].loose_tracks and not a["bonus"].loose_tracks


def test_real_layout_ambiguous_is_held_and_not_overridable(real, qtbot):
    from mm_ui import problems as P
    from mm_ui.review import ApplyDialog, ReviewWindow
    svc = real["svc"]
    ids, f = _layout_library(real)
    held_ids = [ids["stray"], ids["mixed"], ids["main"]]
    plan = svc.plan.build(held_ids + [ids["war"]])
    by = {ap.album_id: ap for ap in plan.albums}
    for k in ("stray", "mixed", "main"):
        lps = [p for p in by[ids[k]].problems if P.kind_of(p) == P.LAYOUT_AMBIGUOUS]
        assert len(lps) == 1 and lps[0].paths == [f["art" if k == "stray" else k]], (k, by[ids[k]].problems)
        assert P.held(by[ids[k]]) and not P.overridable(by[ids[k]])
        assert P.label(lps[0]) == "Loose tracks next to subfolders with audio"
    assert P.layout_shape(P.layout_problem(by[ids["mixed"]].problems)) == "discs"
    assert P.layout_dirs(P.layout_problem(by[ids["mixed"]].problems)) == ["CD1", "CD2"]
    assert P.layout_dirs(P.layout_problem(by[ids["stray"]].problems)) == ["[1984]  My War [SST023]"]
    assert P.layout_dirs(P.layout_problem(by[ids["main"]].problems)) == ["Bonus"]
    assert not P.layout_problem(by[ids["war"]].problems)
    # stray file only: no extras, the album folder below is not part of it
    assert [fc.path_before.name for fc in by[ids["stray"]].files] == ["stray.mp3"]
    p2 = svc.plan.build(held_ids, allow_track_mismatch=set(held_ids))
    assert all(P.layout_problem(ap.problems) and P.held(ap) for ap in p2.albums)
    d = svc.match.destination(ids["mixed"], 803, svc.active_template())
    assert P.layout_problem(d.problems)
    # the apply dialog: held back, files involved listed, no "Apply anyway…"
    w = ReviewWindow(svc); qtbot.addWidget(w)
    dlg = ApplyDialog(plan, album_name=w.album_name, album_row=w.album_row); qtbot.addWidget(dlg)
    assert {ap.album_id for ap in dlg.held_aps} == set(held_ids) and dlg.anyway_btn is None
    items = {(dlg.problem_list.item(i).data(Qt.UserRole), dlg.problem_list.item(i).data(Qt.UserRole + 1)):
             dlg.problem_list.item(i).text() for i in range(dlg.problem_list.count())}
    st = items[(ids["stray"], "layout_ambiguous")]
    assert st.startswith("⚠ Black Flag: loose tracks next to subfolders with audio (held) · [1984]  My War [SST023]/\n"), st
    assert (by[ids["stray"]].artist, by[ids["stray"]].title) == ("Black Flag", "")      # AlbumPlan (d6ac2f3)
    assert "Loose files (1):\n      stray.mp3" in st and "[1984]  My War [SST023]/" in st
    mx = items[(ids["mixed"], "layout_ambiguous")]
    assert "Mix Band – Mixed Up · loose tracks next to disc folders" in mx and "CD2/01 Four.flac" in mx
    # apply: My War applies, the three are held and nothing in them moves
    before = {k: _tree(f[k]) for k in ("mixed", "main")}
    stray_before = sorted(p.name for p in f["art"].iterdir())
    job = svc._c.jobs.wait(svc.plan.apply(plan.id).id)
    assert job.state.name == "DONE", job.message
    for k in ("stray", "mixed", "main"):
        a = svc.library.album(ids[k])
        assert a.status.name == "HELD" and "layout_ambiguous" in {p.kind.value for p in a.held_for}, (k, a.held_for)
    assert {k: _tree(f[k]) for k in ("mixed", "main")} == before
    assert (f["art"] / "stray.mp3").exists() and "stray.mp3" in stray_before
    assert svc.library.album(ids["war"]).status.name == "APPLIED"
    # on their own: nothing applicable -> MMError right away or a FAILED job
    plan3 = svc.plan.build(held_ids)
    try:
        job = svc._c.jobs.wait(svc.plan.apply(plan3.id).id)
        assert job.state.name == "FAILED" and P.nothing_applied(job.message), job
    except svc.types.MMError as e:
        assert "Nothing to apply" in str(e)


def test_real_lists_name_held_loose_folder_and_subfolder_albums(real, qtbot):
    from mm_ui.library import LibraryWindow
    from mm_ui.review import ReviewWindow
    svc = real["svc"]
    ids, f = _layout_library(real)
    job = svc._c.jobs.wait(svc.plan.apply(svc.plan.build([ids["stray"], ids["main"], ids["war"]]).id).id)
    assert job.state.name == "DONE", job.message
    lib = LibraryWindow(svc); qtbot.addWidget(lib)
    m = lib.model
    shown = {m.rows[i].id: (m.index(i, 0).data(), m.index(i, 1).data(), m.index(i, 1).data(Qt.ToolTipRole))
             for i in range(m.rowCount())}
    assert shown[ids["stray"]][:2] == ("Black Flag", "loose tracks next to subfolders with audio (held) · [1984]  My War [SST023]/")
    assert "Next to: [1984]  My War [SST023]/" in shown[ids["stray"]][2]
    assert shown[ids["main"]][:2] == ("[2002]  Main Thing [BON001]", "loose tracks next to subfolders with audio (held) · Bonus/")
    assert all("not an album" not in (v[1] or "") for v in shown.values())
    assert shown[ids["bonus"]][:2] == ("", "[2002]  Main Thing [BON001]/Bonus")
    assert "Listed as its own album" in shown[ids["bonus"]][2]
    w = ReviewWindow(svc); qtbot.addWidget(w)
    w.filter_box.setCurrentText("Needs a look")
    texts = [w.model.index(i).data() for i in range(w.model.rowCount())]
    row = next(i for i, t in enumerate(texts) if t.startswith("Black Flag: loose tracks next to subfolders with audio (held) · [1984]  My War [SST023]/"))
    assert texts[row].endswith("⚠ Needs a look")
    w.list.setCurrentIndex(w.model.index(row))
    assert "Loose tracks next to subfolders with audio" in w.preview.text() and "stray.mp3" in w.preview.text()


# ---------------------------------------------------------------- structured problem detail (ccfa4a8 / 2b82859)
def test_real_types_match_the_mock(real):
    """Problem's new fields, DiscConflict / DiscChoice / DiscFolderMismatch (exported from music_manager.service)
    and ProblemKind (same names and values, and now the same order) match the mock."""
    import dataclasses
    from mm_ui import fake_service as FS
    T = real["svc"].types
    for name in ("Problem", "DiscConflict", "DiscChoice", "DiscFolderMismatch"):
        assert name in T.__all__, name
        assert [f.name for f in dataclasses.fields(getattr(T, name))] == [f.name for f in dataclasses.fields(getattr(FS, name))], name
    assert [(m.name, m.value) for m in T.ProblemKind] == [(m.name, m.value) for m in FS.ProblemKind]
    pr = T.Problem(T.ProblemKind.COLLISION, "m", [])
    assert (pr.disc_conflicts, pr.layout_reason, pr.blocking_subdirs, pr.loose_files, pr.folder_mismatches) == (
        [], "", [], [], [])


def test_real_disc_conflicts_reason_and_choices(real, qtbot):
    from mm_ui import problems as P
    from mm_ui.review import ApplyDialog
    svc = real["svc"]
    aid, folder = _two_disc_album(real, "Lost Discs", 709)
    (ap,) = svc.plan.build([aid]).albums
    p = next(p for p in ap.problems if P.kind_of(p) == P.DISC_AMBIGUOUS)
    assert [c.path for c in p.disc_conflicts] == p.paths
    assert {c.path.name: c.reason for c in p.disc_conflicts} == dict.fromkeys(
        ["01 a.flac", "02 b.flac", "01 d.flac", "02 e.flac"], "no_title_match")
    c = next(c for c in p.disc_conflicts if c.path.name == "01 d.flac")
    assert (c.number, c.title, c.title_matches) == (1, "d", [])
    assert [(x.disc, x.position, x.number, x.title) for x in c.same_number] == [(1, "1-1", 1, "One"), (2, "2-1", 1, "Four")]
    assert all(0.0 <= x.title_similarity < 0.8 for x in c.same_number)
    d = ApplyDialog(svc.plan.build([aid])); qtbot.addWidget(d)
    t = d.problem_list.item(0).text()
    assert "01 d.flac — its title doesn't match any of the release's tracks with this number" in t
    assert "Track 1 on each disc: 1-1 “One” (" in t and "2-1 “Four” (" in t and "title match)" in t


def test_real_sources_disagree_folder_tag_and_m3u(real, qtbot):
    """A CD2 folder file tagged disc 1 and listed by CD1's playlist: held, never paired by the tags."""
    import mutagen
    from mm_ui import problems as P
    from mm_ui.review import ApplyDialog, ReviewWindow
    svc = real["svc"]
    title, rid = "Mason Jar", 720
    folder = real["lib"] / "Disc Band" / f"[1999]  {title} [DSC{rid}]"
    layout = [("CD1/01 One", 1), ("CD1/02 Two", 1), ("CD1/03 Three", 1), ("CD2/01 Four", 1), ("CD2/02 Five", 2)]
    for rel, disc in layout:
        tone(folder / f"{rel}.flac")
        f = mutagen.File(folder / f"{rel}.flac", easy=True); f["discnumber"] = str(disc); f.save()
    (folder / "CD1" / "Disc Band - CD1.m3u").write_text("01 One.flac\n02 Two.flac\n03 Three.flac\n../CD2/01 Four.flac\n")
    r = release(rid, "Disc Band", title, 1999, f"DSC {rid}", [t for _p, t in DISC_TRACKS])
    for tr, (pos, _t) in zip(r["tracklist"], DISC_TRACKS):
        tr["position"] = pos
    real["discogs"].releases[rid] = r
    assert svc._c.jobs.wait(svc.library.scan().id).state.name == "DONE"
    (row,) = [x for x in svc.albums(None, limit=50).items if x.title == title]
    assert svc._c.jobs.wait(svc.match.find([row.id]).id).state.name == "DONE"
    svc.match.choose(row.id, rid)
    plan = svc.plan.build([row.id])
    (ap,) = plan.albums
    p = next(p for p in ap.problems if P.kind_of(p) == P.DISC_AMBIGUOUS)
    (c,) = p.disc_conflicts
    assert c.path.name == "01 Four.flac" and c.reason == "sources_disagree" and c.title_matches == []
    assert (c.folder_disc, c.tag_disc, c.sidecar_disc) == (2, 1, 1) and c.sidecar.name == "Disc Band - CD1.m3u"
    assert [x.position for x in c.same_number] == ["1-1", "2-1"]
    assert P.held(ap) and not P.overridable(ap)
    assert P.held(svc.plan.build([row.id], allow_track_mismatch={row.id}).albums[0])
    d = ApplyDialog(plan); qtbot.addWidget(d)
    t = d.problem_list.item(0).text()
    assert "CD2/01 Four.flac — its disc folder, disc tag and cue/m3u say different discs" in t
    assert "folder says disc 2; disc tag says disc 1; Disc Band - CD1.m3u says disc 1" in t and d.anyway_btn is None
    w = ReviewWindow(svc); qtbot.addWidget(w)
    w.filter_box.setCurrentText("All")
    i = next(i for i in range(w.model.rowCount()) if title in w.model.index(i).data())
    w.list.setCurrentIndex(w.model.index(i))
    marks = [w.table.item(r_, 7).text() for r_ in range(w.table.rowCount()) if w.table.item(r_, 7) is not None]
    assert marks.count("discs disagree") == 1, marks


def test_real_layout_problems_carry_structured_fields(real, qtbot):
    from mm_ui import problems as P
    svc = real["svc"]
    ids, f = _layout_library(real)
    by = {ap.album_id: ap for ap in svc.plan.build([ids["stray"], ids["mixed"], ids["main"]]).albums}
    st, mx, mn = (P.layout_problem(by[ids[k]].problems) for k in ("stray", "mixed", "main"))
    assert (st.layout_reason, st.blocking_subdirs, st.loose_files) == ("album_folders", [f["war"]], [f["art"] / "stray.mp3"])
    assert (mx.layout_reason, mx.blocking_subdirs, mx.loose_files) == (
        "disc_folders", [f["mixed"] / "CD1", f["mixed"] / "CD2"], [f["mixed"] / "01 One.flac"])
    assert (mn.layout_reason, mn.blocking_subdirs, mn.loose_files) == ("album_folders", [f["main"] / "Bonus"],
                                                                       [f["main"] / "01 Main.flac"])
    assert P.layout_dirs(mx) == ["CD1", "CD2"] and P.layout_dirs(st) == ["[1984]  My War [SST023]"]


def test_real_disc_folder_mismatch_held_with_naming_button(real, qtbot, monkeypatch):
    from mm_ui import problems as P
    from mm_ui.naming import NamingDialog
    from mm_ui.review import ApplyDialog, ReviewWindow
    svc = real["svc"]
    aid, folder = _two_disc_album(real, "Folder Discs", 701)
    (ap,) = svc.plan.build([aid]).albums
    assert not P.has_folder_mismatch(ap.problems)                  # the default layout never uses CD folders
    t = svc.templates.save("CD folder per total", "%albumartist%/%album%/CD%disctotal%/$num(%tracknumber%,2) %title%")
    svc.settings.set(active_template=t.id)
    plan = svc.plan.build([aid])
    (ap,) = plan.albums
    p = next(p for p in ap.problems if P.kind_of(p) == P.DISC_FOLDER_MISMATCH)
    assert [m.path.relative_to(folder).as_posix() for m in p.folder_mismatches] == [
        "CD1/01 One.flac", "CD1/02 Two.flac", "CD1/03 Three.flac"]
    assert p.paths == [m.path for m in p.folder_mismatches]
    assert {(m.folder.name, m.folder_disc, m.tag_disc) for m in p.folder_mismatches} == {("CD2", 2, 1)}
    assert all(m.destination.parent == m.folder for m in p.folder_mismatches)
    assert "01 One.flac -> CD2/ (disc tag 1)" in p.message
    assert P.held(ap) and not P.overridable(ap)
    assert P.has_folder_mismatch(svc.plan.build([aid], allow_track_mismatch={aid}).albums[0].problems)
    assert P.has_folder_mismatch(svc.match.destination(aid, 701, svc.active_template()).problems)
    d = ApplyDialog(plan); qtbot.addWidget(d)
    text = d.problem_list.item(0).text() if d.problem_list.count() == 1 else "\n".join(
        d.problem_list.item(i).text() for i in range(d.problem_list.count()))
    assert "Tracks would go into another disc's folder" in text
    assert "CD1/01 One.flac → CD2/ (disc tag 1)" in text and P.NAMING_HINT in text
    assert d.naming_btn is not None and d.anyway_btn is None and not d.ok_btn.isEnabled()
    # review screen: the button opens the naming settings
    opened = []
    monkeypatch.setattr(NamingDialog, "exec", lambda self: opened.append(self) or QDialog.Rejected)
    w = ReviewWindow(svc); qtbot.addWidget(w)
    w.filter_box.setCurrentText("All")
    i = next(i for i in range(w.model.rowCount()) if "Folder Discs" in w.model.index(i).data())
    w.list.setCurrentIndex(w.model.index(i))
    assert not w.naming_btn.isHidden() and "CD1/01 One.flac → CD2/ (disc tag 1)" in w.preview.text()
    w.naming_btn.click()
    assert len(opened) == 1
    # apply holds it; nothing moves
    before = _tree(folder)
    try:
        job = svc._c.jobs.wait(svc.plan.apply(plan.id).id)
        assert job.state.name == "FAILED", job.message
    except svc.types.MMError as e:
        assert "Nothing to apply" in str(e)
    a = svc.library.album(aid)
    assert a.status.name == "HELD" and "disc_folder_mismatch" in {p.kind.value for p in a.held_for}
    assert _tree(folder) == before
    held = next(p for p in a.held_for if p.kind.value == "disc_folder_mismatch")
    assert [m.folder.name for m in held.folder_mismatches] == ["CD2"] * 3     # survives the index round trip
    svc.settings.set(active_template="archivist")
    assert not P.has_folder_mismatch(svc.plan.build([aid]).albums[0].problems)


def test_real_loose_tracks_named_before_any_plan(real, qtbot):
    """d6ac2f3: AlbumRow.loose_tracks from the scan names it in every screen before a plan exists or it's held."""
    from mm_ui.library import LibraryWindow
    from mm_ui.review import ApplyDialog, ReviewWindow
    svc = real["svc"]
    ids, f = _layout_library(real)
    a = svc.library.album(ids["stray"])
    assert a.loose_tracks and (a.artist, a.title, a.year, a.catno) == ("Black Flag", "", "", "")
    assert a.status.name != "HELD" and not a.held_for
    row = next(r for r in svc.albums(None, limit=50).items if r.id == ids["stray"])
    assert row.loose_tracks
    lib = LibraryWindow(svc); qtbot.addWidget(lib)
    m = lib.model
    i = next(i for i in range(m.rowCount()) if m.rows[i].id == ids["stray"])
    assert (m.index(i, 0).data(), m.index(i, 1).data()) == ("Black Flag", "loose tracks next to subfolders with audio (held)")     # no plan yet: no subfolders
    w = ReviewWindow(svc); qtbot.addWidget(w)
    w.filter_box.setCurrentText("All")
    texts = [w.model.index(j).data() for j in range(w.model.rowCount())]
    t = next(t for t in texts if t.startswith("Black Flag: loose tracks next to subfolders with audio (held)"))
    assert "My War" not in t
    assert "Needs a look" not in t
    assert not any(t.startswith("lib – ") for t in texts)          # never the grandparent's name again
    assert w.album_name(ids["stray"]) == "Black Flag: loose tracks next to subfolders with audio (held)"
    plan = svc.plan.build([ids["stray"], ids["war"]])
    d = ApplyDialog(plan, album_name=w.album_name, album_row=w.album_row); qtbot.addWidget(d)
    held = [d.tree.topLevelItem(k).text(0) for k in range(d.tree.topLevelItemCount())]
    assert "Held back: Black Flag: loose tracks next to subfolders with audio (held) · [1984]  My War [SST023]/" in held, held      # the plan names them
    d2 = ApplyDialog(plan); qtbot.addWidget(d2)                      # no library lookup: AlbumPlan names
    assert "Held back: Black Flag: loose tracks next to subfolders with audio (held) · [1984]  My War [SST023]/" in [
        d2.tree.topLevelItem(k).text(0) for k in range(d2.tree.topLevelItemCount())]


def test_real_review_header_counts_accepted(real, qtbot):
    from mm_ui import problems as P
    from mm_ui.review import ReviewWindow
    svc = real["svc"]
    svc.match.choose(real["ids"]["Test Album"].id, 501)
    c = {s.name: n for s, n in svc.library.status_counts().items()}
    assert c.get("ACCEPTED")
    w = ReviewWindow(svc); qtbot.addWidget(w)
    assert f"{c['ACCEPTED']} accepted" in w.counts.text() and P.status_summary(c) in w.counts.text()


def test_real_layout_messages_match_the_mock(real):
    """The mock quotes the engine's layout messages (0a3fad1: STRAY_AUDIO worded neutrally); keep them equal."""
    from mm_ui import fake_service as FS
    fs = sys.modules["music_manager.fs"]
    assert (FS.STRAY_AUDIO, FS.MIXED_DISCS) == (fs.STRAY_AUDIO, fs.MIXED_DISCS)
    assert "not an album" not in fs.STRAY_AUDIO


def test_real_disc_track_count_medley_held_and_shown(real, qtbot):
    """1566b36: CD2 has a medley file for 2-1 + 2-2, so 2 files for 3 tracks; held, disc left unpaired."""
    from mm_ui import problems as P
    from mm_ui.review import ApplyDialog, ReviewWindow
    svc = real["svc"]
    title, rid = "Medley Night", 730
    folder = real["lib"] / "Disc Band" / f"[1999]  {title} [DSC{rid}]"
    for rel in ("CD1/01 One", "CD1/02 Two", "CD1/03 Three", "CD2/01 Four,Five", "CD2/02 Six"):
        tone(folder / f"{rel}.flac")
    tracks = [("1-1", "One"), ("1-2", "Two"), ("1-3", "Three"), ("2-1", "Four"), ("2-2", "Five"), ("2-3", "Six")]
    r = release(rid, "Disc Band", title, 1999, f"DSC {rid}", [t for _p, t in tracks])
    for tr, (pos, _t) in zip(r["tracklist"], tracks):
        tr["position"] = pos
    r["formats"] = [{"name": "CD", "qty": "2"}]
    real["discogs"].releases[rid] = r
    assert svc._c.jobs.wait(svc.library.scan().id).state.name == "DONE"
    (row,) = [x for x in svc.albums(None, limit=50).items if x.title == title]
    assert svc._c.jobs.wait(svc.match.find([row.id]).id).state.name == "DONE"
    svc.match.choose(row.id, rid)
    plan = svc.plan.build([row.id])
    (ap,) = plan.albums
    (p,) = [p for p in ap.problems if P.kind_of(p) == P.DISC_TRACK_COUNT]
    assert sorted(x.relative_to(folder).as_posix() for x in p.paths) == ["CD2/01 Four,Five.flac", "CD2/02 Six.flac"]
    assert P.held(ap) and not P.overridable(ap)
    assert [q for q in svc.plan.build([row.id], allow_track_mismatch={row.id}).albums[0].problems
            if P.kind_of(q) == P.DISC_TRACK_COUNT]
    d = ApplyDialog(plan); qtbot.addWidget(d)
    t = "\n".join(d.problem_list.item(i).text() for i in range(d.problem_list.count()))
    assert "A disc's file count doesn't match its tracks" in t and "CD2/01 Four,Five.flac" in t and d.anyway_btn is None
    w = ReviewWindow(svc); qtbot.addWidget(w)
    w.filter_box.setCurrentText("All")
    i = next(i for i in range(w.model.rowCount()) if title in w.model.index(i).data())
    w.list.setCurrentIndex(w.model.index(i))
    marks = [w.table.item(r_, 7).text() for r_ in range(w.table.rowCount()) if w.table.item(r_, 7) is not None]
    assert marks.count("disc count differs") == 2, marks
    before = _tree(folder)
    try:
        job = svc._c.jobs.wait(svc.plan.apply(plan.id).id)
        assert job.state.name == "FAILED", job.message
    except svc.types.MMError as e:
        assert "Nothing to apply" in str(e)
    a = svc.library.album(row.id)
    assert a.status.name == "HELD" and "disc_track_count" in {q.kind.value for q in a.held_for}
    assert _tree(folder) == before


# ---------------------------------------------------------------- clustering + Match window (c334bbf)
def _tagged(path: Path, artist: str, album: str, title: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "1",
                    "-metadata", f"artist={artist}", "-metadata", f"album={album}", "-metadata", f"title={title}",
                    "-y", str(path)], check=True)


def _rollins(real):
    """The contract's clustering example: loose files of two albums next to an album folder."""
    svc, lib, dg = real["svc"], real["lib"], real["discogs"]
    art = lib / "Henry Rollins"
    for i, t in enumerate(["Black And White", "Followed Around", "Lost And Found"], 1):
        _tagged(art / f"{i:02d} {t}.mp3", "Henry Rollins", "Hot Animal Machine", t)
    _tagged(art / "01 Gone Fishin'.mp3", "Rollins Band", "Life Time", "Gone Fishin'")
    tone(art / "[1987]  Hot Animal Machine [TXH 0002]" / "01 Black And White.flac")
    (art / "cover.jpg").write_bytes(b"\xff\xd8\xff")
    dg.releases[901] = release(901, "Henry Rollins", "Hot Animal Machine", 1987, "TXH 0002",
                               ["Black And White", "Followed Around", "Lost And Found"])
    job = svc.library.scan()
    assert svc._c.jobs.wait(job.id).state.name == "DONE"
    return art


def test_real_clusters_adopt_and_match_window(real, qtbot, monkeypatch):
    from mm_ui import problems as P
    from mm_ui.match import FETCHING, KEY, MatchWindow
    svc = real["svc"]
    art = _rollins(real)
    w = MatchWindow(svc); qtbot.addWidget(w)
    groups, loose = w.left.topLevelItem(0), w.left.topLevelItem(1)
    assert groups.child(0).text(0) == "Henry Rollins – Hot Animal Machine" and groups.child(0).childCount() == 3
    assert loose.child(0).text(0) == "01 Gone Fishin'.mp3"
    cid = groups.child(0).data(0, KEY)[1]
    assert cid.startswith("cl-") and len(cid) == 15
    w.select(cid)
    assert w.view.album_id is None and len(w.view.unmatched_files) == 3 and w.adopt_btn.isEnabled()
    w.adopt()
    assert svc._c.jobs.wait(w.last_find_job.id).state.name == "DONE"
    qtbot.waitUntil(lambda: w.view is not None and w.view.album_id is not None and bool(w.view.candidates), timeout=5000)
    aid = w.view.album_id
    assert svc.library.album(aid).cluster_id == cid and w.item_for(cid).text(0).endswith(f"[{P.CLUSTER_BADGE}]")
    if not w.view.fetched:
        assert w.fetch_label.text() == FETCHING and not w.fetch_label.isHidden()
    svc.match.choose(aid, 901)
    plan = svc.plan.build([aid])
    if plan.fetch_job is not None:
        assert svc._c.jobs.wait(plan.fetch_job.id if hasattr(plan.fetch_job, "id") else plan.fetch_job).state.name == "DONE"
        plan = svc.plan.build([aid])
    qtbot.waitUntil(lambda: w.view.fetched, timeout=5000)
    assert w.fetch_label.isHidden()
    rel = w.tracks.topLevelItem(0)
    assert rel.text(1).startswith("Release tracks (3)")
    assert [rel.child(i).text(3) for i in range(3)] == ["01 Black And White.mp3", "02 Followed Around.mp3",
                                                        "03 Lost And Found.mp3"]
    # apply dialog: only the cluster's files move, out of their common folder; the cover stays
    from mm_ui.review import ApplyDialog
    assert not plan.albums[0].problems, plan.albums[0].problems
    dlg = ApplyDialog(plan, album_row=svc.library.album); qtbot.addWidget(dlg)
    assert "is a cluster: only its 3 file(s) move" in dlg.cluster_label.text()
    assert str(art) in dlg.cluster_label.text() and P.NON_AUDIO_STAYS in dlg.cluster_label.text()
    assert all(Path(f.path_before).parent == art for f in plan.albums[0].files)
