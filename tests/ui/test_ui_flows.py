import pytest
"""UI flows on the fake backend: review -> accept -> apply dialog -> Job -> job_finished; history undo;
naming preview; library paging. Dialog exec() is patched so nothing blocks offscreen."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QInputDialog, QMessageBox

from dataclasses import replace
from pathlib import Path

from mm_ui import problems as P
from mm_ui import review as review_mod
from mm_ui.history import HistoryDialog
from mm_ui.library import LibraryWindow
from mm_ui.naming import NamingDialog
from mm_ui.review import ApplyDialog, ReviewWindow



def not_fetch(job):
    """job_finished filter: skip the FETCH_CANDIDATES jobs that plan.build starts (Plan.fetch_job)."""
    return job.kind.name != "FETCH_CANDIDATES"


def built(svc, ids, *a, **kw):
    """plan.build the way the UI does: if it returns a fetch_job, wait for it and build again."""
    plan = svc.plan.build(ids, *a, **kw)
    if plan.fetch_job is not None:
        svc.jobs.wait(plan.fetch_job, 5)
        plan = svc.plan.build(ids, *a, **kw)
    return plan

def patch_dialog(monkeypatch, result):
    seen = []

    def fake_exec(self):
        seen.append(self)
        return QDialog.Accepted if result else QDialog.Rejected
    monkeypatch.setattr(ApplyDialog, "exec", fake_exec)
    return seen


def accept_first(w, filter_name="Needs review"):
    w.filter_box.setCurrentText(filter_name)
    w.list.setCurrentIndex(w.model.index(0))
    aid = w.album_id
    assert aid is not None
    w.accept_current()
    return aid


def test_review_accept_apply_flow(fake, qtbot, monkeypatch):
    w = ReviewWindow(fake); qtbot.addWidget(w); w.show()
    assert w.windowTitle() == "Matrixwell — Discogs review"
    assert not w.apply_btn.isEnabled()
    aid = accept_first(w)
    assert fake.library.album(aid).status.name == "ACCEPTED"
    assert w.apply_btn.isEnabled() and "1 queued" in w.queue_label.text()

    ops_before = fake.journal.list(0, 100).total
    seen = patch_dialog(monkeypatch, True)
    with qtbot.waitSignal(fake.events.job_finished, check_params_cb=not_fetch, timeout=3000) as sig:
        w.open_apply()
        assert w.queue_label.text() == "<b>Building plan…</b>" and not w.apply_btn.isEnabled()   # off-thread
        assert seen == []                                          # the dialog waits for the built plan
        qtbot.waitUntil(lambda: bool(seen), timeout=3000)          # after the fetch job and a second build
    (dlg,) = seen
    assert [ap.album_id for ap in dlg.plan.albums] == [aid] and dlg.plan.files_to_move > 0
    job = sig.args[0]
    assert job.state.name == "DONE" and job.kind.name == "APPLY"
    assert fake.library.album(aid).status.name == "APPLIED"
    assert fake.journal.list(0, 100).total == ops_before + 1
    qtbot.waitUntil(lambda: "0 queued" in w.queue_label.text(), timeout=1000)
    assert not w._pending_jobs


def test_review_apply_cancel_changes_nothing(fake, qtbot, monkeypatch):
    w = ReviewWindow(fake); qtbot.addWidget(w)
    aid = accept_first(w)
    patch_dialog(monkeypatch, False)
    calls = []
    monkeypatch.setattr(fake.plan, "apply", lambda pid: calls.append(pid))
    with qtbot.waitSignal(w.plan_flow_done, timeout=3000):
        w.open_apply()
    assert calls == [] and fake.library.album(aid).status.name == "ACCEPTED" and w.apply_btn.isEnabled()


def test_review_apply_failure_warns(fake, qtbot, monkeypatch):
    w = ReviewWindow(fake); qtbot.addWidget(w)
    accept_first(w)
    patch_dialog(monkeypatch, True)
    monkeypatch.setattr(fake._c.fake, "_set", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: warned.append(a[2])))
    with qtbot.waitSignal(fake.events.job_finished, check_params_cb=not_fetch, timeout=3000) as sig:
        w.open_apply()
    assert sig.args[0].state.name == "FAILED"
    assert warned and "disk full" in warned[0]


def test_review_counts_come_from_status_counts(fake, qtbot, monkeypatch):
    calls = []
    orig = fake.library.status_counts
    monkeypatch.setattr(fake.library, "status_counts", lambda f=None: calls.append(f) or orig(f))
    w = ReviewWindow(fake); qtbot.addWidget(w)
    assert calls and "2 matched" in w.counts.text() and "2 need review" in w.counts.text()
    assert "1 no match" in w.counts.text()
    n = len(calls); w.reload()
    assert len(calls) == n + 1                       # one call per refresh, not one per status


def test_review_after_apply_uses_match_destination(fake, qtbot, monkeypatch):
    seen = []
    orig = fake.match.destination
    monkeypatch.setattr(fake.match, "destination", lambda a, r, t=None: seen.append((a, r, t)) or orig(a, r, t))
    w = ReviewWindow(fake); qtbot.addWidget(w)
    w.filter_box.setCurrentText("Matched"); w.list.setCurrentIndex(w.model.index(0))
    c = fake.match.candidates(w.album_id)[0]
    assert seen[-1] == (w.album_id, c.release_id, "archivist")       # Template.id of the active template
    album = fake.library.album(w.album_id)
    assert album.title == "Everything Went Black"                     # review list is AlbumSort.ARTIST
    assert w.preview.text() == ("Black Flag/[1982]  Everything Went Black [SST015]/"     # relative to library_root
                                "   (file names appear once the release is fetched)")
    assert w.preview.toolTip().startswith("Library: /mock/grok-bot-approved")
    fake._c.fake._fetch_queue = [(w.album_id, c.release_id)]; fake._c.fake._fetch_one()   # candidate_ready
    assert w.preview.text() == ("Black Flag/[1982]  Everything Went Black [SST015]/"
                                "A1. Gimmie Gimmie Gimmie.flac   (+4 more)")
    D = fake.types.Destination
    monkeypatch.setattr(fake.match, "destination",
                        lambda a, r, t=None: D(orig(a, r).library_root, orig(a, r).folder, [],
                                               [fake.types.Problem(fake.types.ProblemKind.COLLISION,
                                                                   "Collision with another album: X", [])]))
    w.render()
    assert "⚠ Two things would get the same name: Collision with another album: X" in w.preview.text() and "#a33" in w.preview.styleSheet()
    monkeypatch.setattr(fake.match, "destination",
                        lambda a, r, t=None: (_ for _ in ()).throw(fake.types.NotFound("release 1")))
    w.render()
    assert w.preview.text() == "⚠ release 1"


def test_review_not_matched_yet_for_unmatched(fake, qtbot):
    w = ReviewWindow(fake); qtbot.addWidget(w)
    w.filter_box.setCurrentText("All")
    w.list.setCurrentIndex(w.model.index(w.model.rowCount() - 1))   # a synthetic, UNMATCHED row
    assert "not matched yet" in w.cand_title.text()


def test_history_undo_flow(fake, qtbot, monkeypatch):
    h = HistoryDialog(fake); qtbot.addWidget(h)
    assert h.table.rowCount() >= 1 and h.undo_btn.isEnabled()
    op = h.current()
    album_id = fake._c._op_plans[op.id].albums[0].album_id
    seen = patch_dialog(monkeypatch, True)
    with qtbot.waitSignal(fake.events.job_finished, check_params_cb=not_fetch, timeout=3000) as sig:
        h.preview_undo()
    assert seen[0].plan.albums and sig.args[0].kind.name == "UNDO" and sig.args[0].state.name == "DONE"
    ops = {o.id: o for o in fake.journal.list(0, 100).items}
    assert not ops[op.id].undoable
    assert fake.library.album(album_id).status.name == "NEEDS_REVIEW"
    assert h.table.rowCount() == len(ops)
    texts = [h.table.item(r, 3).text() for r in range(h.table.rowCount())]
    assert texts[0].startswith("Undo of “Tagged and moved: Black Flag – My War”")
    assert texts[1].startswith("Undone ")


def test_history_status_from_operation_fields(fake, qtbot):
    h = HistoryDialog(fake); qtbot.addWidget(h)
    rows = {h.table.item(r, 1).text(): h.table.item(r, 3).text() for r in range(h.table.rowCount())}
    assert rows["Tagged and moved: Black Flag – My War"] == "Can undo"
    assert rows["Tagged and moved: Black Flag – Slip It In"].startswith("Undone ")
    assert rows["Undid: Tagged and moved: Black Flag – Slip It In"].startswith(
        "Undo of “Tagged and moved: Black Flag – Slip It In”")
    h.table.selectRow(1); assert not h.undo_btn.isEnabled()
    op = h.ops[0]
    h.ops = [replace(op, undoable=False)]
    assert h.status_text(h.ops[0]) == "Can't undo (journal missing)"


def test_history_undo_cancel(fake, qtbot, monkeypatch):
    h = HistoryDialog(fake); qtbot.addWidget(h)
    op = h.current()
    patch_dialog(monkeypatch, False)
    h.preview_undo(); qtbot.wait(20)
    assert {o.id: o for o in fake.journal.list(0, 100).items}[op.id].undoable


def test_naming_preview_via_templates(fake, qtbot, monkeypatch):
    calls = []
    orig = fake.templates.preview
    monkeypatch.setattr(fake.templates, "preview", lambda t, ids: calls.append((t, ids)) or orig(t, ids))
    n = NamingDialog(fake); qtbot.addWidget(n)
    assert [n.preset.itemData(i) for i in range(n.preset.count())] == \
        ["archivist", "simple", "edition-detail", "label-archive", None]
    assert n.preset.itemText(0) == "Archivist (default)"
    assert n.preset.currentData() == "archivist" and not n.delete.isEnabled()
    first = n.preview.toPlainText().splitlines()
    assert first[0] == "Black Flag/[1981]  Damaged [SST007]/A1. Rise Above.flac"
    assert calls[-1][1] == n.sample_ids and n.issues.text() == ""
    n.preset.setCurrentIndex(n.preset.findData("label-archive"))
    assert n.preview.toPlainText().startswith("SST/[SST007] Black Flag - Damaged (1981)/A1. Rise Above.flac")
    assert "Unknown label/" in n.preview.toPlainText()
    assert len(n.sample_ids) == 6        # 5 mock albums + 1 synthetic; Live '84 + synthetic have no label
    assert n.issues.text() == "⚠ 2 albums have no label: Unknown label/"
    n.catno.setChecked(False)
    assert "SST007" not in n.preview.toPlainText()
    n.sides.setChecked(False)                         # sequential sides: numbers in the preview
    assert "SST/Black Flag - Damaged (1981)/1. Rise Above.flac" in n.preview.toPlainText()
    assert "%tracknumber%" in calls[-1][0]
    assert n.preview.toPlainText().splitlines()[-1] == "Unknown label/Mock Artist 00000 - Mock Album 0 (1960)"


def test_naming_shows_collisions_and_validation_errors(fake, qtbot):
    n = NamingDialog(fake); qtbot.addWidget(n)
    n.tpl.setText("%albumartist%/%title%"); n.tpl.textEdited.emit(n.tpl.text())
    assert n.preset.currentData() is None
    assert "Collision (albums): Black Flag ← " in n.issues.text() and "Apply will refuse" in n.issues.text()
    n.tpl.setText("%albumartist%/%nope%"); n.tpl.textEdited.emit(n.tpl.text())
    assert "Unknown field %nope%" in n.issues.text()


def test_naming_save_builtin_sets_active_template(fake, qtbot, monkeypatch):
    saves = []
    monkeypatch.setattr(fake.templates, "save", lambda *a: saves.append(a))
    n = NamingDialog(fake); qtbot.addWidget(n)
    n.preset.setCurrentIndex(n.preset.findData("simple"))
    n.save()
    assert n.result() == QDialog.Accepted and saves == []
    s = fake.settings.get()
    assert s.active_template == "simple" and s.sequential_sides is False


def test_naming_save_custom_creates_user_template(fake, qtbot, monkeypatch):
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("Mine", True)))
    n = NamingDialog(fake); qtbot.addWidget(n)
    n.catno.setChecked(False); n.sides.setChecked(False)
    n.save()
    assert n.result() == QDialog.Accepted
    s = fake.settings.get()
    assert s.active_template == "user:mine" and s.sequential_sides is True
    t = fake.templates.list()[-1]
    assert (t.id, t.name, t.builtin) == ("user:mine", "Mine", False) and "%catno%" not in t.text
    # reopening selects the saved layout and reflects the setting; it can be deleted
    n2 = NamingDialog(fake); qtbot.addWidget(n2)
    assert n2.preset.currentData() == "user:mine" and not n2.sides.isChecked() and n2.delete.isEnabled()
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    n2.delete_current()
    assert [t.id for t in fake.templates.list()][-1] == "label-archive"
    assert n2.preset.currentData() == "archivist" and fake.settings.get().active_template == "archivist"


def test_naming_save_validation_error_keeps_dialog_open(fake, qtbot, monkeypatch):
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("Bad", True)))
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: warned.append(a[2])))
    n = NamingDialog(fake); qtbot.addWidget(n)
    n.tpl.setText("%albumartist%/%nope%"); n.tpl.textEdited.emit(n.tpl.text())
    n.save()
    assert warned and "nope" in warned[0] and n.result() != QDialog.Accepted
    assert fake.settings.get().active_template == "archivist"


def test_naming_save_cancelled_name_prompt(fake, qtbot, monkeypatch):
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("", False)))
    n = NamingDialog(fake); qtbot.addWidget(n)
    n.catno.setChecked(False); n.save()
    assert n.result() != QDialog.Accepted and len(fake.templates.list()) == 4


def test_library_window_pages_counts_and_sorts(fake, qtbot, monkeypatch):
    calls = []
    orig = fake.library.status_counts
    monkeypatch.setattr(fake.library, "status_counts", lambda f=None: calls.append(f) or orig(f))
    w = LibraryWindow(fake); qtbot.addWidget(w)
    assert w.windowTitle() == "Matrixwell — Library"
    n_real = len(fake._c.fake.albums(fake.album_filter()))
    assert w.model.total == n_real + 50 and "mock data" in w.info.text()
    assert w.status.itemText(0) == f"All albums ({n_real + 50:,})" and w.status.itemText(4) == "Matched (2)"
    assert len(calls) == 1
    w.select_status("Matched")
    assert w.model.total == 2 and w.model.data(w.model.index(0, 5)) == "Matched"
    assert w.model.data(w.model.index(0, 6)).endswith("%")
    h = w.view.horizontalHeader()
    w.select_status("All albums")
    h.setSortIndicator(2, Qt.DescendingOrder)                         # Year, descending
    assert w.model.sort_key() is fake.types.AlbumSort.YEAR and w.model.descending
    assert "sorted by year ↓" in w.info.text() and w.desc_btn.isChecked()
    years = [w.model.data(w.model.index(r, 2)) for r in range(10)]
    assert years == sorted(years, reverse=True)
    h.setSortIndicator(3, Qt.AscendingOrder)                          # Cat. no.: no sort key
    assert (h.sortIndicatorSection(), h.sortIndicatorOrder()) == (2, Qt.DescendingOrder)
    assert w.model.sort_key() is fake.types.AlbumSort.YEAR and w.model.descending
    w.desc_btn.setChecked(False)                                      # toggle back to ascending
    assert h.sortIndicatorOrder() == Qt.AscendingOrder and not w.model.descending
    assert [w.model.data(w.model.index(r, 2)) for r in range(3)] == ["1960", "1961", "1962"]
    h.setSortIndicator(7, Qt.AscendingOrder)                          # Added: newest scan first
    assert w.model.sort_key() is fake.types.AlbumSort.ADDED
    assert w.model.data(w.model.index(0, 1)) == "Everything Went Black"   # last mock album scanned
    w.desc_btn.setChecked(True)
    assert w.model.data(w.model.index(0, 1)) == "Mock Album 0"            # oldest first
    h.setSortIndicator(6, Qt.AscendingOrder)                          # Confidence
    assert w.model.sort_key() is fake.types.AlbumSort.CONFIDENCE
    assert w.model.data(w.model.index(0, 6)) == "72%"
    w.search.setText("damaged")
    assert w.status.itemText(0) == "All albums (1)"


def test_windows_default_to_get_service(qapp, qtbot):
    w = ReviewWindow(); qtbot.addWidget(w)
    from mm_ui.backend import get_service
    assert w.svc is get_service() and w.svc.is_fake
    get_service()._c.fake._timer.stop()


def test_ui_modules_do_not_import_the_mock():
    import ast, pathlib
    root = pathlib.Path(review_mod.__file__).parent
    for name in ("review.py", "library.py", "history.py", "naming.py", "app.py"):
        tree = ast.parse((root / name).read_text())
        mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert not {m for m in mods if m and "fake" in m}, name


def test_naming_raw_catno_toggle_swaps_field_and_preview(fake, qtbot):
    n = NamingDialog(fake); qtbot.addWidget(n)
    assert n.raw_catno.isEnabled() and not n.raw_catno.isChecked()      # off by default
    assert "[SST007]" in n.preview.toPlainText() and "[SST 007]" not in n.preview.toPlainText()
    assert "%catno%" in n.tpl.text()
    n.raw_catno.setChecked(True)
    assert "%catno_raw%" in n.tpl.text() and "%catno%" not in n.tpl.text()
    lines = n.preview.toPlainText().splitlines()
    assert lines[0] == "Black Flag/[1981]  Damaged [SST 007]/A1. Rise Above.flac"
    assert any("[SST 023]" in l for l in lines) and not any("[SST023]" in l for l in lines)
    n.preset.setCurrentIndex(n.preset.findData("edition-detail"))       # applies to any preset
    assert "[SST 007 • Vinyl]" in n.preview.toPlainText()
    n.raw_catno.setChecked(False)
    assert "[SST007 • Vinyl]" in n.preview.toPlainText()
    n.catno.setChecked(False)                                           # no catno at all -> toggle moot
    assert not n.raw_catno.isEnabled()


def test_naming_raw_catno_disabled_when_backend_lacks_field(fake, qtbot, monkeypatch):
    orig = fake.templates.preview

    def strict(text, ids):                     # a backend from before %catno_raw% was added
        if "%catno_raw%" in text:
            raise fake.types.ValidationError("Unknown field %catno_raw%", field="catno_raw")
        return orig(text, ids)
    monkeypatch.setattr(fake.templates, "preview", strict)
    n = NamingDialog(fake); qtbot.addWidget(n)
    assert not n.raw_catno.isEnabled() and "doesn't support %catno_raw%" in n.raw_catno.toolTip()
    assert "[SST007]" in n.preview.toPlainText()


# ---------------------------------------------------------------- problems + legacy journals (contract rev. 21:38 PT)
def _plan_with(fake, kinds):
    T = fake.types
    probs = [T.Problem(T.ProblemKind(k), f"msg {k}", [Path(f"/mock/x/{k}")]) for k in kinds]
    ap = T.AlbumPlan(1, 2, Path("/mock/a"), Path("/mock/b"), [], probs)
    return T.Plan(9, [ap], [], 0, 0, 0)


def test_apply_dialog_lists_problems_and_blocks(fake, qtbot):
    from mm_ui import problems as P
    kinds = ["dest_inside_source", "source_inside_target", "symlink_outside", "collision", "track_mismatch",
             "file_changed"]
    d = ApplyDialog(_plan_with(fake, kinds)); qtbot.addWidget(d)
    assert d.problem_list.count() == len(kinds) and not d.ok_btn.isEnabled() and d.trust_btn is None
    for i, k in enumerate(kinds):
        text = d.problem_list.item(i).text()
        assert P.LABELS[k] in text and f"msg {k}" in text and f"/mock/x/{k}" in text
    assert len(set(P.LABELS.values())) == len(P.LABELS)
    d._accept_if_clear(); assert d.result() != QDialog.Accepted
    # contract: "any problem blocks apply" -> track_mismatch alone blocks too
    d2 = ApplyDialog(_plan_with(fake, ["track_mismatch"])); qtbot.addWidget(d2)
    assert not d2.ok_btn.isEnabled()
    d3 = ApplyDialog(_plan_with(fake, [])); qtbot.addWidget(d3)
    assert d3.ok_btn.isEnabled() and d3.problem_list.isHidden()


def _slip(fake):
    return next(a for a in fake.albums(None, limit=10).items if a.title == "Slip It In")


def _matched(fake):
    return fake.albums({"MATCHED"}, limit=1).items[0]


def test_apply_holds_flagged_albums_and_applies_clean(fake, qtbot, monkeypatch):
    slip, good = _slip(fake), _matched(fake)
    for a in (slip, good):
        fake.match.choose(a.id, fake.match.candidates(a.id)[0].release_id)
    w = ReviewWindow(fake); qtbot.addWidget(w)
    seen = patch_dialog(monkeypatch, True)
    summaries = []
    monkeypatch.setattr(ReviewWindow, "show_summary", lambda self, t: summaries.append(t))
    with qtbot.waitSignal(fake.events.job_finished, check_params_cb=not_fetch, timeout=3000):
        w.open_apply()
    (dlg,) = seen
    assert [ap.album_id for ap in dlg.held_aps] == [slip.id] and [ap.album_id for ap in dlg.clean_aps] == [good.id]
    assert dlg.ok_btn.isEnabled() and dlg.ok_btn.text() == "Apply 1 album(s)"      # no whole-batch disable
    qtbot.waitUntil(lambda: bool(summaries), timeout=1000)
    assert summaries == ["Applied 1 album. 1 held back for a look."]
    assert fake.library.album(good.id).status.name == "APPLIED"
    held = fake.library.album(slip.id)
    assert held.status.name == "HELD" and {p.kind.value for p in held.held_for} == {"symlink_outside", "track_mismatch"}
    assert not hasattr(w, "held")                                     # no UI-side list: derived from HELD
    assert "1 need a look" in w.queue_label.text()
    w.filter_box.setCurrentText("Needs a look")
    assert w.model.rowCount() == 1 and "Needs a look" in w.model.index(0).data()
    assert "Link points outside the album" in w.model.index(0).data(Qt.ToolTipRole)
    w.list.setCurrentIndex(w.model.index(0))
    assert "Link points outside the album" in w.preview.text()


def test_needs_a_look_derived_from_status_after_restart(fake, qtbot):
    """HELD lives in the library: a fresh window (as after a restart) shows it with held_for text."""
    T = fake.types
    a = _matched(fake)
    fake._c.fake._set(a.id, status=T.MatchStatus.HELD,
                      held_for=[T.Problem(T.ProblemKind.FILE_CHANGED, "edited since the plan", [a.folder])])
    w = ReviewWindow(fake); qtbot.addWidget(w)
    assert "1 need a look" in w.queue_label.text() and w.apply_btn.isEnabled()
    w.filter_box.setCurrentText("Needs a look")
    assert w.model.rowCount() == 1 and w.album_id == a.id
    assert "Held back at apply: Files changed since this was planned: edited since the plan" in w.preview.text()
    fake.match.choose(a.id, fake.match.candidates(a.id)[0].release_id)    # re-chosen: no longer held
    assert fake.library.album(a.id).held_for == []


def test_apply_summary_wording():
    assert ReviewWindow.apply_summary(12, 3) == "Applied 12 albums. 3 held back for a look."
    assert ReviewWindow.apply_summary(1, 0) == "Applied 1 album."


def test_apply_anyway_overrides_track_mismatch_one_album(fake, qtbot, monkeypatch):
    T = fake.types
    a, b = fake.albums({"MATCHED", "NEEDS_REVIEW"}, limit=2).items
    for x in (a, b):
        fake.match.choose(x.id, fake.match.candidates(x.id)[0].release_id)
        fake._c.mock_problems[x.id] = [T.Problem(T.ProblemKind.TRACK_MISMATCH,
                                                 "Discogs lists 9 tracks, folder has 8", [x.folder])]
    builds = []
    orig = fake.plan.build
    monkeypatch.setattr(fake.plan, "build", lambda ids, template=None, allow_track_mismatch=frozenset():
                        builds.append(set(allow_track_mismatch)) or orig(ids, template, allow_track_mismatch))
    confirms = []
    monkeypatch.setattr(ApplyDialog, "confirm_mismatch", lambda self, ap: confirms.append(
        "; ".join(p.message for p in ap.problems)) or True)
    dialogs = []

    def fake_exec(self):
        dialogs.append(self)
        if len(dialogs) == 1:
            assert not self.ok_btn.isEnabled() and self.anyway_btn is not None
            assert not self.anyway_btn.isEnabled()                   # nothing selected yet
            self.problem_list.setCurrentRow(0); assert self.anyway_btn.isEnabled()
            self.apply_anyway()
            return self.result()
        assert [ap.album_id for ap in self.clean_aps] == [a.id] and [ap.album_id for ap in self.held_aps] == [b.id]
        return QDialog.Accepted
    monkeypatch.setattr(ApplyDialog, "exec", fake_exec)
    monkeypatch.setattr(ReviewWindow, "show_summary", lambda self, t: None)
    w = ReviewWindow(fake); qtbot.addWidget(w)
    with qtbot.waitSignal(fake.events.job_finished, check_params_cb=not_fetch, timeout=3000):
        w.open_apply()
    assert builds == [set(), set(), {a.id}]                                  # fetch round, rebuild, then only the confirmed album
    assert confirms == ["Discogs lists 9 tracks, folder has 8"]
    assert fake.library.album(a.id).status.name == "APPLIED"
    assert fake.library.album(b.id).status.name == "HELD"


def test_apply_anyway_not_offered_for_other_problems(fake, qtbot):
    slip = _slip(fake)
    fake.match.choose(slip.id, fake.match.candidates(slip.id)[0].release_id)
    d = ApplyDialog(built(fake, [slip.id])); qtbot.addWidget(d)
    assert d.anyway_btn is None and not d.ok_btn.isEnabled()          # symlink_outside can't be overridden
    plan = built(fake, [slip.id], allow_track_mismatch={slip.id})
    assert {p.kind.value for p in plan.albums[0].problems} == {"symlink_outside"}


def test_accept_clear_winners_uses_auto_accept_and_rules(fake, qtbot, monkeypatch):
    w = ReviewWindow(fake); qtbot.addWidget(w)
    R = fake.types.AutoRules
    strict = R(min_score=0.99)
    monkeypatch.setattr(fake.match, "auto_rules", lambda: strict)
    calls = []
    orig = fake.match.auto_accept
    monkeypatch.setattr(fake.match, "auto_accept", lambda c, rules=None, local_track_count=None:
                        calls.append((rules, local_track_count)) or orig(c, rules, local_track_count))
    monkeypatch.setattr(ReviewWindow, "confirm_accept_all", lambda self, n, rules, waiting=0: True)
    w.accept_confident()
    assert calls and all(r is strict and n is not None for r, n in calls)
    assert fake.library.status_counts()[fake.types.MatchStatus.ACCEPTED] == 0   # nobody clears 99%
    monkeypatch.setattr(fake.match, "auto_rules", lambda: R())
    w.accept_confident()     # nothing fetched yet: None means "not a clear winner YET", never accept
    assert fake.library.status_counts()[fake.types.MatchStatus.ACCEPTED] == 0 and w.last_auto_waiting >= 1
    m = fake.albums({"MATCHED"}, limit=10).items
    fake._c.fake._fetch_queue = [(a.id, fake.match.candidates(a.id)[0].release_id) for a in m]
    for _ in m:
        fake._c.fake._fetch_one()
    w.accept_confident()
    assert fake.library.status_counts()[fake.types.MatchStatus.ACCEPTED] >= 1
    assert "Accept all ≥" not in w.accept_all.text()


def test_review_shows_margin_and_track_methods(fake, qtbot):
    w = ReviewWindow(fake); qtbot.addWidget(w)
    w.filter_box.setCurrentText("All")
    for title, method, text, color in (("Everything Went Black", "position", "position (check)", "#c98a00"),
                                       ("Slip It In", "unmatched", "stays in place", "#777777")):
        row = next(i for i in range(w.model.rowCount()) if title in w.model.index(i).data())
        w.list.setCurrentIndex(w.model.index(row))
        c = fake.match.candidates(w.album_id)[0]
        fake._c.fake._fetch_queue = [(w.album_id, c.release_id)]; fake._c.fake._fetch_one()
        # 4dc5e69: a fetched release with a track-count mismatch scores lower and may drop below the runner-up
        cands = fake.match.candidates(w.album_id)
        i = next(k for k, x in enumerate(cands) if x.release_id == c.release_id)
        w.pick_candidate(i); c = cands[i]
        if c.margin is not None:
            assert f"leads runner-up by {c.margin * 100:.0f} pts" in w.cand_title.text()
        last = w.table.item(w.table.rowCount() - 1, 7)
        assert last.text() == text and last.foreground().color().name() == color
        assert w.table.item(0, 7).text() in ("track number", "side position")


def test_undo_skips_changed_files_only(fake, qtbot, monkeypatch):
    h = HistoryDialog(fake); qtbot.addWidget(h)
    op = h.current()                                                  # My War: one file changed since
    plan = fake.journal.undo_plan(op.id)
    fc = [p for ap in plan.albums for p in ap.problems]
    assert [p.kind.value for p in fc] == ["file_changed"] and fc[0].message == "changed since (contents differ)"
    files = [f for ap in plan.albums for f in ap.files]
    skipped = [f for f in files if f.skip_reason]
    assert len(files) == len(fake._c._op_plans[op.id].albums[0].files)          # skipped step stays in files
    assert [f.skip_reason for f in skipped] == ["changed since (contents differ)"] and plan.skipped == 1
    assert skipped[0].path_before in fc[0].paths
    assert plan.files_to_move == len(files) - 1 and plan.bytes_to_copy == 0
    seen = patch_dialog(monkeypatch, True)
    with qtbot.waitSignal(fake.events.job_finished, check_params_cb=not_fetch, timeout=3000) as sig:
        h.preview_undo()
    (dlg,) = seen
    assert dlg.ok_btn.isEnabled() and not dlg.blocking and dlg.skipped_list.count() == 1
    assert "changed since (contents differ)" in dlg.skipped_list.item(0).text()
    assert dlg.skipped[0] is skipped[0] or dlg.skipped[0].skip_reason == skipped[0].skip_reason
    assert sig.args[0].state.name == "DONE"


def test_mock_plan_has_problem_objects(fake):
    slip = next(a for a in fake.albums(None, limit=10).items if a.title == "Slip It In")
    c = fake.match.candidates(slip.id)[0]
    d = fake.match.destination(slip.id, c.release_id)
    assert all(isinstance(p, fake.types.Problem) and p.paths for p in d.problems)
    assert {p.kind for p in d.problems} >= {fake.types.ProblemKind.SYMLINK_OUTSIDE,
                                            fake.types.ProblemKind.TRACK_MISMATCH}


def _legacy_row(h):
    return next(r for r, op in enumerate(h.ops) if op.legacy)


def test_history_legacy_status_and_fields(fake, qtbot):
    ops = fake.journal.list(0, 100).items
    legacy = [o for o in ops if o.legacy]
    assert legacy and all(o.journal_format == 1 for o in legacy)
    assert all(o.journal_format == 2 and not o.legacy for o in ops if o not in legacy)
    h = HistoryDialog(fake); qtbot.addWidget(h)
    r = _legacy_row(h)
    assert h.table.item(r, 3).text() == "Older format, undo needs confirmation"
    h.table.selectRow(r); assert h.undo_btn.isEnabled()


def test_undo_plan_legacy_problems_and_trust(fake):
    op = next(o for o in fake.journal.list(0, 100).items if o.legacy)
    p = fake.journal.undo_plan(op.id)
    kinds = [x.kind.value for ap in p.albums for x in ap.problems]
    assert kinds and set(kinds) == {"legacy_journal"}
    assert p.files_to_move > 0 and all(ap.files for ap in p.albums)      # file moves still planned
    t = fake.journal.undo_plan(op.id, trust_legacy=True)
    assert not [x for ap in t.albums for x in ap.problems]


def test_history_legacy_undo_requires_confirm(fake, qtbot, monkeypatch):
    calls = []
    orig = fake.journal.undo_plan
    monkeypatch.setattr(fake.journal, "undo_plan",
                        lambda oid, trust_legacy=False: calls.append(trust_legacy) or orig(oid, trust_legacy))
    h = HistoryDialog(fake); qtbot.addWidget(h)
    h.table.selectRow(_legacy_row(h)); op = h.current()
    dialogs = []

    def fake_exec(self):
        dialogs.append(self)
        if self.trust_btn is not None:          # first preview: blocked, user picks "Confirm older-format undo…"
            assert not self.ok_btn.isEnabled()
            assert all(p.kind.value == "legacy_journal" for p in self.blocking)
            return ApplyDialog.TRUST_LEGACY
        assert self.ok_btn.isEnabled()
        return QDialog.Accepted
    monkeypatch.setattr(ApplyDialog, "exec", fake_exec)
    # declining the confirm: nothing re-planned, nothing undone
    monkeypatch.setattr(HistoryDialog, "confirm_legacy", lambda self, o: False)
    h.preview_undo()
    assert calls == [False] and len(dialogs) == 1
    assert {o.id: o for o in fake.journal.list(0, 100).items}[op.id].undoable
    # confirming: re-plan with trust_legacy=True, then undo runs
    calls.clear(); dialogs.clear()
    monkeypatch.setattr(HistoryDialog, "confirm_legacy", lambda self, o: True)
    with qtbot.waitSignal(fake.events.job_finished, check_params_cb=not_fetch, timeout=3000) as sig:
        h.preview_undo()
    assert calls == [False, True] and len(dialogs) == 2
    assert sig.args[0].state.name == "DONE"
    assert {o.id: o for o in fake.journal.list(0, 100).items}[op.id].undone


def test_fake_undo_refuses_untrusted_legacy_preview(fake, qtbot):
    op = next(o for o in fake.journal.list(0, 100).items if o.legacy)
    p = fake.journal.undo_plan(op.id)
    with qtbot.waitSignal(fake.events.job_finished, check_params_cb=not_fetch, timeout=2000) as sig:
        fake.journal.undo(op.id, p.id)
    assert sig.args[0].state.name == "FAILED" and "legacy_journal" in sig.args[0].message


def test_auto_accept_unfetched_is_never_a_winner(fake):
    a = fake.albums({"MATCHED"}, limit=1).items[0]
    cands = fake.match.candidates(a.id)
    assert not cands[0].fetched
    assert fake.match.auto_accept(cands, fake.match.auto_rules(), local_track_count=a.track_count) is None
    loose = fake.types.AutoRules(require_track_count=False)
    assert fake.match.auto_accept(cands, loose, local_track_count=a.track_count) is cands[0]


def test_status_values_are_strings_like_the_real_service(fake):
    assert all(isinstance(m.value, str) for m in fake.types.MatchStatus)
    assert fake.types.MatchStatus.HELD.value == "held" and fake.types.JobState.DONE.value == "done"


def test_apply_dialog_shows_plan_warnings_and_cover_art(fake, qtbot):
    a = fake.albums({"MATCHED"}, limit=1).items[0]
    fake.match.choose(a.id, fake.match.candidates(a.id)[0].release_id)
    plan = built(fake, [a.id])
    assert "folder.jpg will be saved from Discogs for 1 album(s)" in plan.warnings
    d = ApplyDialog(plan); qtbot.addWidget(d)
    assert not d.warnings_label.isHidden() and "folder.jpg will be saved" in d.warnings_label.text()
    fake.settings.set(save_cover_art=False)
    assert not any("folder.jpg" in w for w in built(fake, [a.id]).warnings)


def test_review_artwork_placeholder(fake, qtbot):
    w = ReviewWindow(fake); qtbot.addWidget(w)
    w.list.setCurrentIndex(w.model.index(0))
    assert w.artwork.pixmap().isNull() and "No cover" in w.artwork.text()


def test_plan_build_runs_off_the_ui_thread(fake, qtbot, monkeypatch):
    import threading
    w = ReviewWindow(fake); qtbot.addWidget(w)
    accept_first(w)
    threads = []
    orig = fake.plan.build
    monkeypatch.setattr(fake.plan, "build", lambda *a, **k: threads.append(threading.current_thread()) or orig(*a, **k))
    patch_dialog(monkeypatch, False)
    with qtbot.waitSignal(w.plan_flow_done, timeout=3000):
        w.open_apply()
        assert w.build_progress is not None                 # progress indicator while it runs
    assert threads and threads[0] is not threading.main_thread()
    assert w.build_progress is None


def test_plan_build_error_is_shown(fake, qtbot, monkeypatch, _no_modal_message_boxes):
    w = ReviewWindow(fake); qtbot.addWidget(w)
    accept_first(w)
    monkeypatch.setattr(fake.plan, "build", lambda *a, **k: (_ for _ in ()).throw(
        fake.types.ProviderError("Discogs HTTP 503", retry_after=5)))
    with qtbot.waitSignal(w.plan_flow_done, timeout=3000):
        w.open_apply()
    assert any("Discogs HTTP 503" in str(a) for n, a in _no_modal_message_boxes if n == "warning")
    assert w.apply_btn.isEnabled()


def test_apply_errors_from_plan_apply_are_shown(fake, qtbot, monkeypatch, _no_modal_message_boxes):
    w = ReviewWindow(fake); qtbot.addWidget(w)
    accept_first(w)
    patch_dialog(monkeypatch, True)
    monkeypatch.setattr(fake.plan, "apply", lambda pid: (_ for _ in ()).throw(fake.types.PlanStale("applied twice")))
    with qtbot.waitSignal(w.plan_flow_done, timeout=3000):
        w.open_apply()
    assert any("applied twice" in str(a) for n, a in _no_modal_message_boxes if n == "warning")


def test_undo_planstale_is_shown(fake, qtbot, monkeypatch, _no_modal_message_boxes):
    h = HistoryDialog(fake); qtbot.addWidget(h)
    patch_dialog(monkeypatch, True)
    monkeypatch.setattr(fake.journal, "undo", lambda o, p: (_ for _ in ()).throw(fake.types.PlanStale("stale")))
    h.preview_undo()
    assert any("stale" in str(a) and "Preview the undo again" in str(a)
               for n, a in _no_modal_message_boxes if n == "warning")
    assert h.album_name(0) == "Album not in the library index"


def test_library_find_and_root_hint(fake, qtbot, monkeypatch):
    w = LibraryWindow(fake); qtbot.addWidget(w)
    assert w.hint.isHidden()
    found = []
    monkeypatch.setattr(fake.match, "find", lambda ids: found.append(list(ids)) or fake._c._find(ids))
    w.view.selectRow(0)
    w.find_selected()
    assert found and found[0] == [w.model.rows[0].id]
    fake.settings.set(library_roots=[])
    w.update_counts()
    assert not w.hint.isHidden() and "No library folder" in w.hint.text() and not w.root_btn.isHidden()
    scans = []
    monkeypatch.setattr(fake.library, "scan", lambda roots=None: scans.append(roots) or fake._c._scan(roots))
    w.choose_library_root("/mock/new-root")
    assert fake.settings.get().library_roots == [Path("/mock/new-root")] and scans == [None]


def test_plan_fetch_job_waits_and_rebuilds(fake, qtbot, monkeypatch):
    """Contract 2026-10-17: unfetched releases -> RELEASE_NOT_FETCHED + Plan.fetch_job; the UI waits for
    that job and builds again, then reviews the full plan."""
    w = ReviewWindow(fake); qtbot.addWidget(w)
    aid = accept_first(w)
    assert not fake.match.candidates(aid)[0].fetched
    p0 = fake.plan.build([aid])
    assert p0.fetch_job is not None and [x.kind.value for x in p0.albums[0].problems] == ["release_not_fetched"]
    assert p0.albums[0].files == [] and fake.plan.build([aid]).fetch_job == p0.fetch_job    # reused while queued
    builds = []
    orig = fake.plan.build
    monkeypatch.setattr(fake.plan, "build", lambda *a, **k: builds.append(1) or orig(*a, **k))
    seen = patch_dialog(monkeypatch, False)
    with qtbot.waitSignal(w.plan_flow_done, timeout=5000):
        w.open_apply()
    assert len(builds) == 2 and len(seen) == 1
    plan = seen[0].plan
    assert plan.fetch_job is None and not plan.albums[0].problems and plan.files_to_move > 0
    assert fake.match.candidates(aid)[0].fetched


def test_release_not_fetched_is_left_out_not_held(fake, qtbot):
    a, b = fake.albums({"MATCHED", "NEEDS_REVIEW"}, limit=2).items
    for x in (a, b):
        fake.match.choose(x.id, fake.match.candidates(x.id)[0].release_id)
    fake._c.fake._fetch_queue = [(a.id, fake.match.candidates(a.id)[0].release_id)]; fake._c.fake._fetch_one()
    plan = fake.plan.build([a.id, b.id])
    d = ApplyDialog(plan); qtbot.addWidget(d)
    assert [x.album_id for x in d.clean_aps] == [a.id] and [x.album_id for x in d.waiting_aps] == [b.id]
    assert d.held_aps == [] and d.ok_btn.isEnabled() and not d.waiting_label.isHidden()
    with qtbot.waitSignal(fake.events.job_finished, check_params_cb=not_fetch, timeout=3000) as sig:
        fake.plan.apply(plan.id)
    assert sig.args[0].state.name == "DONE"
    assert fake.library.album(b.id).status.name == "ACCEPTED"           # not HELD: status stays as it was
    assert ReviewWindow.apply_summary(1, 0, 1) == "Applied 1 album. 1 left out until Discogs sends the release."


# ---- 0666aad: release fetching moved out of plan.build into Plan.fetch_job ------------------------------
def _accepted_unfetched(fake, n=1):
    rows = [r for r in fake.albums({"MATCHED", "NEEDS_REVIEW"}, limit=40).items
            if not fake.match.candidates(r.id)[0].fetched][:n]
    for r in rows:
        fake.match.choose(r.id, fake.match.candidates(r.id)[0].release_id)
    return [r.id for r in rows]


def test_release_not_fetched_kind_and_label(fake, qtbot):
    """(1) RELEASE_NOT_FETCHED = "release_not_fetched": shown as "fetching from Discogs…" with a spinner,
    not an error, never holds the album."""
    T = fake.types
    assert T.ProblemKind.RELEASE_NOT_FETCHED.value == "release_not_fetched"
    assert P.LABELS["release_not_fetched"] == "Fetching from Discogs…"
    (aid,) = _accepted_unfetched(fake)
    plan = fake.plan.build([aid])
    (ap,) = plan.albums
    assert P.waiting(ap) and not P.held(ap) and ap.files == []
    assert ap.problems[0].message.endswith("is being fetched from Discogs: build the plan again when it arrives")
    assert any("being fetched from Discogs (job" in x for x in plan.warnings)
    d = ApplyDialog(plan); qtbot.addWidget(d); d.show()
    assert d.waiting_aps == [ap] and d.held_aps == []
    assert "fetching from Discogs…" in d.waiting_label.text()
    assert d.waiting_spinner.isVisible() and d.waiting_spinner.maximum() == 0      # indeterminate = spinner
    assert "color:#b" not in d.waiting_label.text()                               # not styled as an error
    fake.jobs.wait(plan.fetch_job, 5)


def test_plan_fetch_job_last_field_and_reused_while_running(fake):
    """(2) Plan.fetch_job is the last field; building again while the job runs returns the same id."""
    from dataclasses import fields
    assert [f.name for f in fields(fake.types.Plan)][-1] == "fetch_job"
    ids = _accepted_unfetched(fake, 2)
    p1 = fake.plan.build(ids)
    p2 = fake.plan.build(ids)
    p3 = fake.plan.build(ids[:1])
    assert p1.fetch_job is not None and p1.fetch_job == p2.fetch_job == p3.fetch_job
    job = fake.jobs.get(p1.fetch_job)
    assert job.kind.name == "FETCH_CANDIDATES" and job.total == 2
    assert fake.jobs.wait(p1.fetch_job, 5).state.name == "DONE"
    p4 = fake.plan.build(ids)
    assert p4.fetch_job is None and all(not ap.problems and ap.files for ap in p4.albums)
    assert [ap.album_id for ap in p4.albums] == ids                             # input order kept


def test_plan_fetch_progress_shown_then_rebuilt(fake, qtbot, monkeypatch):
    """(2) The review screen shows the fetch job's progress, waits for job_finished, builds again."""
    w = ReviewWindow(fake); qtbot.addWidget(w)
    _accepted_unfetched(fake, 2)
    w.reload()
    labels, builds, enabled = [], [], []
    orig = fake.plan.build
    monkeypatch.setattr(fake.plan, "build", lambda *a, **k: builds.append(orig(*a, **k)) or builds[-1])
    def on_progress(j):
        if j.kind.name == "FETCH_CANDIDATES":
            labels.append((w.queue_label.text(), w.build_progress.labelText(), w.build_progress.maximum()))
            enabled.append(w.apply_btn.isEnabled())
    fake.events.job_progress.connect(on_progress)
    seen = patch_dialog(monkeypatch, False)
    with qtbot.waitSignal(w.plan_flow_done, timeout=5000):
        w.open_apply()
    assert [b.fetch_job is not None for b in builds] == [True, False]
    assert labels and all(q in ("<b>Building plan…</b>", "<b>Fetching from Discogs…</b>") for q, _, _ in labels)
    assert labels[-1][0] == "<b>Fetching from Discogs…</b>"       # album_changed didn't reset the label
    assert any("Fetching from Discogs…" in t and m == 2 for _, t, m in labels)   # progress: n of 2
    assert len(seen) == 1 and not seen[0].waiting_aps
    assert enabled and not any(enabled)                                     # Apply stays off while fetching


def test_plan_fetch_failure_is_reported_without_looping(fake, qtbot, monkeypatch):
    """A failed download: the job fails, builds report it with fetch_job None for 2 minutes (no loop),
    then a later build retries."""
    (aid,) = _accepted_unfetched(fake)
    rid = fake.match.candidates(aid)[0].release_id
    fake._c.mock_fetch_fail.add(rid)
    w = ReviewWindow(fake); qtbot.addWidget(w)
    builds = []
    orig = fake.plan.build
    monkeypatch.setattr(fake.plan, "build", lambda *a, **k: builds.append(orig(*a, **k)) or builds[-1])
    seen = patch_dialog(monkeypatch, False)
    with qtbot.waitSignal(w.plan_flow_done, timeout=5000):
        w.open_apply()
    assert len(builds) == 2 and builds[1].fetch_job is None
    (d,) = seen
    assert d.waiting_aps and "could not be fetched from Discogs: release" in d.waiting_label.text()
    assert not d.waiting_spinner.isVisible() and not d.held_aps
    assert "1 release(s) could not be fetched from Discogs" in builds[1].warnings
    assert fake.jobs.get(builds[0].fetch_job).state.name == "FAILED"
    assert fake.library.album(aid).status.name == "ACCEPTED"                  # not held
    fake._c.mock_fetch_fail.clear()
    assert fake.plan.build([aid]).fetch_job is None                           # within 2 minutes: no retry
    now = fake._c.clock()
    fake._c.clock = lambda: now + 121
    again = fake.plan.build([aid])
    assert again.fetch_job is not None and "is being fetched" in again.albums[0].problems[0].message
    fake.jobs.wait(again.fetch_job, 5)
    assert not fake.plan.build([aid]).albums[0].problems


def test_stale_release_counts_as_not_fetched(fake):
    """Deviation 16: a stored release older than 6 hours needs one fetch job first."""
    (aid,) = _accepted_unfetched(fake)
    built(fake, [aid])
    assert fake.plan.build([aid]).fetch_job is None
    now = fake._c.clock()
    fake._c.clock = lambda: now + 6 * 3600 + 1
    p = fake.plan.build([aid])
    assert p.fetch_job is not None and P.waiting(p.albums[0])
    fake.jobs.wait(p.fetch_job, 5)
    assert fake.plan.build([aid]).fetch_job is None


def test_fake_jobs_wait_returns_after_job_finished(fake):
    """(3) jobs.wait returns only after job_finished was emitted (and its slots ran), as in 0666aad."""
    order = []
    fake.events.job_finished.connect(lambda j: order.append(("slot", j.id)))
    job = fake.library.scan()
    with pytest.raises(TimeoutError):
        fake.jobs.wait(job.id, 0)                    # still queued
    done = fake.jobs.wait(job.id, 5)
    order.append(("returned", job.id))
    assert done.state.name == "DONE" and order == [("slot", job.id), ("returned", job.id)]
    (aid,) = _accepted_unfetched(fake)
    fj = fake.plan.build([aid]).fetch_job
    fake.jobs.wait(fj, 5)
    assert ("slot", fj) in order
    fake.jobs.idle(5)


def test_plan_build_never_fetches(fake):
    """(4) plan.build returns at once and fetches nothing; the download happens in the background job."""
    (aid,) = _accepted_unfetched(fake)
    fetched = []
    fake.events.candidate_ready.connect(lambda a, r: fetched.append((a, r)))
    p = fake.plan.build([aid])
    assert fetched == [] and not fake.match.candidates(aid)[0].fetched
    assert fake.jobs.get(p.fetch_job).state.name == "QUEUED"
    fake.jobs.wait(p.fetch_job, 5)
    assert fetched == [(aid, p.albums[0].release_id)] and fake.match.candidates(aid)[0].fetched
