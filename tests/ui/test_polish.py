"""UI polish (apply dialog, undo preview, library list, held screen) and Bassline's coming changes:
undo FileChange.kind "track" vs "extra", plan.apply raising MMError when nothing is applicable, and a run
that refuses every album (job FAILED). Fake backend; dialog exec() is patched so nothing blocks."""
from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtWidgets import QDialog

from mm_ui import problems as P
from mm_ui.history import HistoryDialog
from mm_ui.library import FOLDER_COL, LibraryWindow, MiddleElideDelegate
from mm_ui.review import ApplyDialog, ReviewWindow

from test_ui_flows import _matched, _slip, built, not_fetch, patch_dialog


def _choose(fake, *albums):
    for a in albums:
        fake.match.choose(a.id, fake.match.candidates(a.id)[0].release_id)


def _mixed_plan(fake):
    """One clean, one held (Slip It In: symlink_outside + track_mismatch), one still fetching."""
    slip, good = _slip(fake), _matched(fake)
    _choose(fake, slip, good)
    plan = built(fake, [slip.id, good.id])
    other = next(r for r in fake.albums({"MATCHED", "NEEDS_REVIEW"}, limit=40).items
                 if r.id not in (slip.id, good.id) and not fake.match.candidates(r.id)[0].fetched)
    _choose(fake, other)
    p2 = fake.plan.build([other.id])
    assert P.waiting(p2.albums[0])
    combo = replace(plan, albums=list(plan.albums) + list(p2.albums), warnings=list(plan.warnings) + list(p2.warnings),
                    fetch_job=p2.fetch_job)
    return combo, slip, good, other


# ---------------------------------------------------------------- 1 + 2: warnings
def test_apply_warnings_count_only_applying_albums_and_drop_refused_line(fake, qtbot):
    plan, slip, good, other = _mixed_plan(fake)
    assert "folder.jpg will be saved from Discogs for 2 album(s)" in plan.warnings     # backend counts held too
    assert any("will be refused (held)" in w for w in plan.warnings)
    assert any("being fetched from Discogs" in w for w in plan.warnings)
    d = ApplyDialog(plan); qtbot.addWidget(d)
    assert d.warnings == ["folder.jpg will be saved from Discogs for 1 album(s)"]
    assert "for 1 album(s)" in d.warnings_label.text() and "refused" not in d.warnings_label.text()
    assert "1 album(s) held back for a look" in d.problem_label.text()                # said once, here
    fake.jobs.wait(plan.fetch_job, 5)


def test_apply_warnings_helper_edge_cases():
    from types import SimpleNamespace as NS
    pr = NS(kind="collision", message="x", paths=[])
    clean = [NS(album_id=i, problems=[], folder_before=Path(f"/x/{i}")) for i in (1, 2)]
    held = [NS(album_id=3, problems=[pr], folder_before=Path("/x/3"))]
    plan = NS(albums=clean + held, warnings=["folder.jpg will be saved from Discogs for 3 album(s)",
                                             "1 album(s) have problems and will be refused (held) by apply",
                                             "Something unrelated"])
    # album 2 already has a cover: the backend counted 1 and 3 -> only album 1 applies with a new folder.jpg
    has = lambda f: f.name == "2"                                     # noqa: E731
    plan.warnings[0] = "folder.jpg will be saved from Discogs for 2 album(s)"
    assert P.apply_warnings(plan, clean, held, [], has) == ["folder.jpg will be saved from Discogs for 1 album(s)",
                                                           "Something unrelated"]
    # every counted album is held -> the line goes
    plan.warnings[0] = "folder.jpg will be saved from Discogs for 1 album(s)"
    assert P.apply_warnings(plan, clean, held, [], lambda f: f.name != "3") == ["Something unrelated"]
    # nothing held: warnings untouched
    plan2 = NS(albums=clean, warnings=["folder.jpg will be saved from Discogs for 2 album(s)"])
    assert P.apply_warnings(plan2, clean, [], []) == plan2.warnings


# ---------------------------------------------------------------- 3 + 4: change tree
def good_name(fake, plan, album):
    ap = next(ap for ap in plan.albums if ap.album_id == album.id)
    return f"{ap.artist} – {ap.title}"


def test_held_and_fetching_albums_get_one_greyed_row(fake, qtbot):
    plan, slip, good, other = _mixed_plan(fake)
    d = ApplyDialog(plan, album_name=lambda aid: f"#{aid}"); qtbot.addWidget(d)
    t = d.tree
    rows = [t.topLevelItem(i) for i in range(t.topLevelItemCount())]
    assert len(rows) == 3
    clean_row, held_row, wait_row = rows
    assert clean_row.text(0) == f"{good_name(fake, plan, good)}" and clean_row.childCount() > 0
    assert all(clean_row.child(i).text(0) == "track" for i in range(clean_row.childCount()))
    good_ap = next(ap for ap in plan.albums if ap.album_id == good.id)
    assert clean_row.child(0).text(2) == good_ap.files[0].path_after.name           # relative to the Folder row
    assert clean_row.child(0).toolTip(2) == str(good_ap.files[0].path_after)
    for row, what in ((held_row, "Held back: "), (wait_row, "Fetching from Discogs…: ")):
        assert row.text(0).startswith(what) and row.childCount() == 0 and "stays in place" in row.text(2)
        assert row.foreground(0).color().name() == "#888888"
    wait_ap = next(ap for ap in plan.albums if ap.album_id == other.id)
    assert str(wait_ap.folder_after) not in wait_row.text(2)                        # no stale After path
    assert wait_row.text(1) == str(wait_ap.folder_before)
    fake.jobs.wait(plan.fetch_job, 5)


# ---------------------------------------------------------------- 5: undo preview sizing
@pytest.mark.parametrize("kind", ["extra", "track"])
def test_undo_preview_kinds_and_compact_size(fake, qtbot, monkeypatch, kind):
    fake._c.undo_track_kind = kind
    h = HistoryDialog(fake); qtbot.addWidget(h)
    op = next(o for o in h.ops if o.undoable and not getattr(o, "legacy", False))
    plan = fake.journal.undo_plan(op.id)
    assert {f.kind for ap in plan.albums for f in ap.files} == {kind}
    d = ApplyDialog(plan, undo=True); qtbot.addWidget(d)
    top = d.tree.topLevelItem(0)
    assert {top.child(i).text(0).split(" ")[0] for i in range(top.childCount())} == {"track"}
    d.resize(1280, 800); d.show(); qtbot.waitExposed(d)
    assert d.height() < 600, d.height()                       # short plan: no tall empty tree
    assert d.tree.height() >= d.tree.content_height() - 4      # ...and every row is visible


def test_display_kind():
    from types import SimpleNamespace as NS
    assert P.display_kind(NS(kind="extra", path_before=Path("a/1.flac"), path_after=Path("b/1.flac"))) == "track"
    assert P.display_kind(NS(kind="track", path_before=Path("a"), path_after=Path("b"))) == "track"
    assert P.display_kind(NS(kind="extra", path_before=Path("folder.jpg"), path_after=Path("x.jpg"))) == "extra"
    assert P.display_kind(NS(kind="playlist", path_before=Path("a.m3u"), path_after=Path("b.m3u"))) == "playlist"


# ---------------------------------------------------------------- 6: library list
def test_library_folder_elides_middle_with_tooltip_and_fits_few_rows(fake, qtbot):
    w = LibraryWindow(fake); qtbot.addWidget(w)
    assert isinstance(w.view.itemDelegateForColumn(FOLDER_COL), MiddleElideDelegate)
    idx = w.model.index(0, FOLDER_COL)
    assert idx.data(Qt.ToolTipRole) == str(w.model.rows[0].folder)
    w.select_status("Applied")                                # a handful of rows
    n = len(w.model.rows)
    assert 0 < n < 6 or n == 0
    w.resize(1280, 800); w.show(); qtbot.waitExposed(w)
    assert w.view.height() < 300                               # no big white box under a few rows
    w.select_status("All albums")                              # 100k+ rows: the table fills the window
    qtbot.waitUntil(lambda: w.view.height() > 500, timeout=2000)
    assert "mock data" in w.info.text() and w.info.toolTip()


def test_footer_backend_label_is_short():
    from mm_ui.backend import Backend
    b = Backend(object(), "real", "/very/long/checkout/src/music_manager/service/__init__.py")
    assert b.label == "backend: music_manager.service" and "/very/long" in b.label_detail


# ---------------------------------------------------------------- 7: held screen
def test_held_album_track_table_has_no_big_gap(fake, qtbot):
    T = fake.types
    a = _matched(fake)
    fake._c.fake._set(a.id, status=T.MatchStatus.HELD,
                      held_for=[T.Problem(T.ProblemKind.FILE_CHANGED, "edited since the plan", [a.folder])])
    w = ReviewWindow(fake); qtbot.addWidget(w)
    w.filter_box.setCurrentText("Needs a look")
    w.list.setCurrentIndex(w.model.index(0))
    w.resize(1280, 800); w.show(); qtbot.waitExposed(w)
    rows = w.table.rowCount()
    assert 0 < rows < 10
    gap = w.preview.mapTo(w, w.preview.rect().topLeft()).y() - w.table.mapTo(w, w.table.rect().bottomLeft()).y()
    assert gap < 60, gap


# ---------------------------------------------------------------- 8: Bassline's coming changes
def test_mock_apply_raises_when_nothing_applicable(fake, qtbot):
    slip = _slip(fake); _choose(fake, slip)
    plan = built(fake, [slip.id])
    assert all(ap.problems for ap in plan.albums)
    fake._c.apply_raises_when_nothing_applicable = True
    with pytest.raises(fake.types.MMError, match="Nothing to apply: 1 album"):
        fake.plan.apply(plan.id)
    assert fake.library.album(slip.id).status.name == "HELD"      # held as usual, like a06a7c9
    fake._c.apply_raises_when_nothing_applicable = False      # 0666aad: a Job that ends FAILED
    with qtbot.waitSignal(fake.events.job_finished, check_params_cb=not_fetch, timeout=3000) as sig:
        fake.plan.apply(plan.id)
    assert sig.args[0].state.name == "FAILED" and P.nothing_applied(sig.args[0].message)


def test_ui_shows_clear_message_when_apply_raises_nothing_applicable(fake, qtbot, monkeypatch,
                                                                     _no_modal_message_boxes):
    """The dialog saw a clean album, but by apply time the service finds nothing applicable and raises."""
    good = _matched(fake); _choose(fake, good)
    fake._c.apply_raises_when_nothing_applicable = True
    w = ReviewWindow(fake); qtbot.addWidget(w)

    def exec_and_spoil(self):            # the backend's stored plan turns out to have nothing applicable
        c = fake._c
        c._plans[self.plan.id] = replace(self.plan, albums=[replace(ap, problems=[fake.types.Problem(
            fake.types.ProblemKind.COLLISION, "now collides", [ap.folder_after])]) for ap in self.plan.albums])
        return QDialog.Accepted
    monkeypatch.setattr(ApplyDialog, "exec", exec_and_spoil)
    with qtbot.waitSignal(w.plan_flow_done, timeout=5000):
        w.open_apply()
    title, text, detail = w.last_apply_message
    assert title == "Nothing to apply" and "Nothing was moved or tagged" in text and "Nothing to apply" in detail
    assert any(n == "warning" and a[1] == "Nothing to apply" for n, a in _no_modal_message_boxes)
    assert not w._pending_jobs and w.apply_btn.isEnabled()        # no crash, the batch can be reviewed again


def test_ui_shows_clear_message_when_every_album_is_refused_during_run(fake, qtbot, monkeypatch,
                                                                       _no_modal_message_boxes):
    good = _matched(fake); _choose(fake, good)
    fake._c.mock_changed_before_apply = {good.id}
    w = ReviewWindow(fake); qtbot.addWidget(w)
    seen = patch_dialog(monkeypatch, True)
    with qtbot.waitSignal(fake.events.job_finished, check_params_cb=not_fetch, timeout=5000) as sig:
        w.open_apply()
    assert seen and seen[0].clean_aps                              # looked clean when reviewed
    assert sig.args[0].state.name == "FAILED" and "refused 1" in sig.args[0].message
    qtbot.waitUntil(lambda: hasattr(w, "last_apply_message"), timeout=2000)
    title, text, _ = w.last_apply_message
    assert title == "Nothing was applied" and "All 1 album(s) were refused" in text and "Needs a look" in text
    assert any(n == "warning" and a[1] == "Nothing was applied" for n, a in _no_modal_message_boxes)
    a = fake.library.album(good.id)
    assert a.status.name == "HELD" and [p.kind.value for p in a.held_for] == ["file_changed"]


def test_partial_refusal_during_run_still_applies_the_rest(fake, qtbot, monkeypatch):
    a, b = fake.albums({"MATCHED", "NEEDS_REVIEW"}, limit=2).items
    _choose(fake, a, b)
    plan = built(fake, [a.id, b.id])
    fake._c.mock_changed_before_apply = {b.id}
    with qtbot.waitSignal(fake.events.job_finished, check_params_cb=not_fetch, timeout=3000) as sig:
        fake.plan.apply(plan.id)
    assert sig.args[0].state.name == "DONE" and "refused 1" in sig.args[0].message
    assert fake.library.album(a.id).status.name == "APPLIED" and fake.library.album(b.id).status.name == "HELD"


# ---------------------------------------------------------------- e1eca5e: names in plans and undo previews
def test_album_label_prefers_plan_names_then_library_then_folder():
    from types import SimpleNamespace as NS
    lib = {7: "Local Artist – Local Title"}.get
    fb = lambda aid: lib(aid) or (P.NOT_IN_INDEX if not aid else f"Album {aid}")   # noqa: E731  (like the windows)
    base = dict(folder_before=Path("/m/A/[1990]  Now"), folder_after=Path("/m/A/[1990]  Then"))
    assert P.album_label(NS(album_id=7, release_id=5, artist="Rel Artist", title="Rel Title", **base), fb) \
        == "Rel Artist – Rel Title"
    assert P.album_label(NS(album_id=7, release_id=5, artist="", title="Only Title", **base), fb) == "Only Title"
    assert P.album_label(NS(album_id=7, release_id=5, artist="", title="", **base), fb) == "Local Artist – Local Title"
    # older service: no artist/title attributes at all
    assert P.album_label(NS(album_id=7, release_id=5, **base), fb) == "Local Artist – Local Title"
    # old discogs-tag journal: album_id 0, release_id 0, "" names -> the folder undo puts it back into
    old = NS(album_id=0, release_id=0, artist="", title="", **base)
    assert P.album_label(old, fb, undo=True) == "[1990]  Then"
    assert P.album_label(old, fb) == "[1990]  Now"
    assert P.album_label(NS(album_id=0, release_id=42, artist="", title="", **base), fb, undo=True) \
        == "[1990]  Then (Discogs release 42)"
    assert P.album_label(NS(album_id=99, release_id=0, artist="", title="", **base), fb) == "[1990]  Now"   # "Album 99"
    bare = NS(album_id=0, release_id=0, artist="", title="", folder_before=Path("/"), folder_after=Path("/"))
    assert P.album_label(bare, fb, undo=True) == P.NOT_IN_INDEX
    assert P.release_note(bare).startswith("Discogs release unknown")


def test_mock_plans_carry_release_names_and_dialog_shows_them(fake, qtbot):
    slip, good = _slip(fake), _matched(fake)
    _choose(fake, slip, good)
    plan = built(fake, [slip.id, good.id])
    for ap in plan.albums:
        c = next(c for c in fake.match.candidates(ap.album_id) if c.release_id == ap.release_id)
        assert (ap.artist, ap.title) == (c.artist, c.title)
    d = ApplyDialog(plan, album_name=lambda aid: P.NOT_IN_INDEX); qtbot.addWidget(d)
    held = next(ap for ap in plan.albums if ap.album_id == slip.id)
    assert d.problem_list.item(0).text().startswith(f"⚠ {held.artist} – {held.title}:")
    assert P.NOT_IN_INDEX not in "".join(d.problem_list.item(i).text() for i in range(d.problem_list.count()))


def test_undo_preview_names_new_journals_and_falls_back_for_old_ones(fake, qtbot):
    h = HistoryDialog(fake); qtbot.addWidget(h)
    new = next(o for o in h.ops if o.undoable and not o.legacy)
    old = next(o for o in h.ops if o.undoable and o.legacy)
    up = fake.journal.undo_plan(new.id)
    (ap,) = up.albums
    assert ap.artist and ap.title and ap.release_id
    d = ApplyDialog(up, undo=True, album_name=h.album_name); qtbot.addWidget(d)
    assert d.tree.topLevelItem(0).text(0) == f"{ap.artist} – {ap.title}"
    assert f"Discogs release {ap.release_id}" in d.tree.topLevelItem(0).toolTip(0)
    lp = fake.journal.undo_plan(old.id, trust_legacy=True)          # older journal: release_id 0, no names
    (lap,) = lp.albums
    assert (lap.release_id, lap.artist, lap.title) == (0, "", "")
    d2 = ApplyDialog(lp, undo=True, album_name=h.album_name); qtbot.addWidget(d2)
    a = fake.library.album(lap.album_id)
    assert d2.tree.topLevelItem(0).text(0) == f"{a.artist} – {a.title}"     # library index fallback
    assert "unknown" in d2.tree.topLevelItem(0).toolTip(0)
    d3 = ApplyDialog(lp, undo=True, album_name=lambda aid: P.NOT_IN_INDEX); qtbot.addWidget(d3)
    assert d3.tree.topLevelItem(0).text(0) == lap.folder_after.name          # not in the index: folder name


# ---------------------------------------------------------------- 4dc5e69: score weights, auto_score
def test_mock_score_weighs_track_count_and_durations(fake):
    slip = _slip(fake)
    c0 = fake.match.candidates(slip.id)[0]
    assert not c0.fetched and c0.auto_score == c0.score                       # unfetched: score = --auto score
    fake._c.fake._fetch_queue = [(slip.id, c0.release_id)]; fake._c.fake._fetch_one(pace=False)
    c = next(x for x in fake.match.candidates(slip.id) if x.release_id == c0.release_id)
    parts = {p.field: p for p in c.breakdown}
    assert parts["track_count"].value == 0.0 and not parts["track_count"].ok      # one file has no Discogs track
    assert abs(sum(p.weight * p.value for p in c.breakdown) - c.score) < 1e-3
    assert abs(sum(p.weight for p in c.breakdown) - 1.0) < 1e-6
    assert c.auto_score == c0.auto_score and c.score < c.auto_score
    cands = fake.match.candidates(slip.id)
    assert [x.score for x in cands] == sorted((x.score for x in cands), reverse=True)   # sorted by score
    assert fake.library.album(slip.id).confidence == cands[0].score


def test_auto_accept_ranks_by_auto_score_not_score(fake):
    from dataclasses import replace as rep
    a = _matched(fake)
    cands = fake.match.candidates(a.id)
    rules = fake.types.AutoRules(require_track_count=False)
    # score says the runner-up is better (it was fetched and matches), auto_score says the first one wins
    x = rep(cands[0], score=0.80, auto_score=0.97)
    y = rep(cands[0], release_id=cands[0].release_id + 1000, score=0.95, auto_score=0.80)
    assert fake.match.auto_accept([y, x], rules) is x
    assert fake.match.auto_accept([rep(x, auto_score=None, score=0.97), rep(y, auto_score=None, score=0.80)],
                                  rules).score == 0.97               # no auto_score (older service): by score


def test_accept_clear_winners_uses_auto_accept(fake, qtbot, monkeypatch):
    w = ReviewWindow(fake); qtbot.addWidget(w)
    seen = []
    orig = fake.match.auto_accept
    monkeypatch.setattr(fake.match, "auto_accept", lambda *a, **k: seen.append(a[0]) or orig(*a, **k))
    monkeypatch.setattr(ReviewWindow, "confirm_accept_all", lambda self, n, rules, waiting=0: False)
    w.accept_confident()
    assert seen and all(hasattr(c, "auto_score") for cl in seen for c in cl)


def test_review_shows_auto_score_when_it_differs(fake, qtbot):
    w = ReviewWindow(fake); qtbot.addWidget(w)
    w.filter_box.setCurrentText("All")
    row = next(i for i in range(w.model.rowCount()) if "Slip It In" in w.model.index(i).data())
    w.list.setCurrentIndex(w.model.index(row))
    c0 = fake.match.candidates(w.album_id)[0]
    fake._c.fake._fetch_queue = [(w.album_id, c0.release_id)]; fake._c.fake._fetch_one(pace=False)
    cands = fake.match.candidates(w.album_id)
    w.pick_candidate(next(i for i, c in enumerate(cands) if c.release_id == c0.release_id))
    assert f"auto {c0.auto_score:.0%}" in w.cand_title.text()
    assert "of the score" in w.breakdown.text() and "track count" in w.breakdown.text()


def test_long_after_apply_text_never_widens_the_review_window(fake, qtbot):
    """Long engine messages (disc conflicts, folder mismatches) wrap/scroll in the preview; the window stays
    narrower than a 1280px screen (it used to grow to ~1800px)."""
    from mm_ui.review import ReviewWindow
    w = ReviewWindow(fake); qtbot.addWidget(w)
    w.preview.setText("⚠ " + "a long sentence from the engine " * 40 + "\n    Files (1):\n      " + "x" * 300)
    assert w.minimumSizeHint().width() < 1280
    assert 0 < w.preview_scroll.sizeHint().height() <= w.preview_scroll.MAX_H


def test_status_summary_counts_every_status_in_review_order():
    import mm_ui.fake_service as FS
    S = FS.MatchStatus
    c = {S.APPLIED: 1, S.ACCEPTED: 6, S.UNMATCHED: 1, S.HELD: 1, S.MATCHED: 0, S.NEEDS_REVIEW: 0}
    assert P.status_summary(c) == "1 held · 1 unmatched · 6 accepted · 1 applied"
    assert P.status_summary({"NEEDS_REVIEW": 2, "NO_MATCH": 1, "SKIPPED": 3}) == "2 need review · 3 skipped · 1 no match"
    assert P.status_summary({"SOMETHING_NEW": 2}) == "2 something new"       # a status this UI doesn't know yet
    assert P.status_summary({}) == ""


def test_review_header_counts_accepted_held_and_applied(fake, qtbot, monkeypatch):
    """It used to read "0 matched · 0 need review · 0 no match" with every album Accepted."""
    import mm_ui.fake_service as FS
    S = FS.MatchStatus
    monkeypatch.setattr(fake.library, "status_counts",
                        lambda f=None: {S.ACCEPTED: 6, S.HELD: 1, S.APPLIED: 1, S.UNMATCHED: 1, S.MATCHED: 0})
    w = ReviewWindow(fake); qtbot.addWidget(w)
    t = w.counts.text()
    assert t == "<b>9 albums</b> · 1 held · 1 unmatched · 6 accepted · 1 applied", t
    assert "0 matched" not in t


def test_library_footer_uses_the_same_status_words(fake, qtbot):
    lib = LibraryWindow(fake); qtbot.addWidget(lib)
    c = {s.name: n for s, n in fake.library.status_counts(fake.album_filter(None, "")).items()}
    assert lib.counts.text() == P.status_summary(c) and "need review" in lib.counts.text()
