"""ProblemKind.DISC_AMBIGUOUS (fc27fcf, API-CONTRACT "Multi-disc pairing") on the fake backend: the mock pairs
multi-disc tracks on (disc, track number), the disc taken from the tags, then CD1/CD2 folders, then "1-01"
names; a folder it can't place is held with "Can't work out the disc layout", listing the files, and
"Apply anyway…" / allow_track_mismatch never clear it. The real-backend versions are in test_real_backend.py."""
from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from mm_ui import problems as P
from mm_ui import fake_service as FS
from mm_ui.review import ApplyDialog, ReviewWindow

from test_ui_flows import built, not_fetch, patch_dialog

TRACKS = [(1, 1, "One", 200), (1, 2, "Two", 210), (1, 3, "Three", 220), (2, 1, "Four", 230), (2, 2, "Five", 240)]
LAYOUTS = {
    "tags":    [("01 One", 1, 200), ("02 Two", 1, 210), ("03 Three", 1, 220), ("01 Four", 2, 230), ("02 Five", 2, 240)],
    "folders": [("CD1/01 One", None, 200), ("CD1/02 Two", None, 210), ("CD1/03 Three", None, 220),
                ("CD2/01 Four", None, 230), ("CD2/02 Five", None, 240)],
    "names":   [("1-01 One", None, 200), ("1-02 Two", None, 210), ("1-03 Three", None, 220),
                ("2-01 Four", None, 230), ("2-02 Five", None, 240)],
    "titles":  [("01 One", None, 200), ("02 Two", None, 210), ("03 Three", None, 220),
                ("01 Four", None, 230), ("02 Five", None, 240)],
}
AMBIGUOUS = [("01 a", None, 200), ("02 b", None, 210), ("03 c", None, 220), ("01 d", None, 230), ("02 e", None, 240)]


def _album(fake, files, title="Double Trouble"):
    core = fake._c.fake
    aid = core.add_multi_disc_album("Disc Band", title, "1999", files, TRACKS)
    rid = fake.match.candidates(aid)[0].release_id
    core._fetch_queue = [(aid, rid)]; core._fetch_one(pace=False)
    fake.match.choose(aid, rid)
    return aid, rid


def _disc_problems(ap):
    return [p for p in ap.problems if P.kind_of(p) == P.DISC_AMBIGUOUS]


def test_disc_ambiguous_is_in_the_mock_enum_and_labelled():
    assert FS.ProblemKind.DISC_AMBIGUOUS.value == "disc_ambiguous"
    assert P.LABELS["disc_ambiguous"] == "Can't work out the disc layout"
    assert len(set(P.LABELS.values())) == len(P.LABELS)
    pr = FS.Problem(FS.ProblemKind.DISC_AMBIGUOUS, "m", [])
    assert P.label(pr) == "Can't work out the disc layout"
    assert not P.overridable(type("AP", (), {"problems": [pr]})())


@pytest.mark.parametrize("layout", sorted(LAYOUTS))
def test_mock_pairs_multi_disc_by_disc_and_number(fake, layout):
    aid, rid = _album(fake, LAYOUTS[layout])
    remotes, amb = fake._c.fake.disc_pairing(aid, rid)
    assert amb == []
    assert [(rt.disc, rt.number, rt.title) for rt in remotes] == [(d, n, t) for d, n, t, _s in TRACKS]
    c = fake.match.candidates(aid)[0]
    assert all(p.ok and p.remote is not None for p in c.alignment)
    plan = fake.plan.build([aid])
    (ap,) = plan.albums
    assert ap.problems == []
    tags = {fc.path_before.name: {t.key: t.after for t in fc.tags} for fc in ap.files if fc.kind == "track"}
    four = next(v for k, v in tags.items() if "Four" in k)
    assert four["TRACKNUMBER"] == "1" and four["DISCNUMBER"] == "2" and four["DISCOGS_POSITION"] == "2-1"


def test_mock_disc_tags_win_over_folder_and_name_hints(fake):
    """Tags are used when they fit the whole album, even if a stray name hint disagrees."""
    files = [("1-01 One", 1, 200), ("1-02 Two", 1, 210), ("1-03 Three", 1, 220), ("1-01 Four", 2, 230),
             ("1-02 Five", 2, 240)]
    aid, rid = _album(fake, files)
    remotes, amb = fake._c.fake.disc_pairing(aid, rid)
    assert amb == [] and [rt.disc for rt in remotes] == [1, 1, 1, 2, 2]


def test_mock_partial_disc_tags_fall_back_to_folders(fake):
    files = [("CD1/01 One", 1, 200), ("CD1/02 Two", None, 210), ("CD1/03 Three", 1, 220),
             ("CD2/01 Four", None, 230), ("CD2/02 Five", 2, 240)]
    aid, rid = _album(fake, files)
    remotes, amb = fake._c.fake.disc_pairing(aid, rid)
    assert amb == [] and [rt.position for rt in remotes] == ["1-1", "1-2", "1-3", "2-1", "2-2"]


def test_mock_single_disc_releases_are_unchanged(fake):
    a = fake.albums({"MATCHED"}, limit=1).items[0]
    rid = fake.match.candidates(a.id)[0].release_id
    assert fake._c.fake.disc_pairing(a.id, rid) is None
    remotes, amb = FS.pair_tracks([FS.LocalTrack(1, Path("/x/01 A.flac"), None, 1, "A", 1.0)],
                                  [FS.RemoteTrack("1", 1, 1, "A", "x", 1.0)])
    assert amb == [] and remotes[0].title == "A"


def test_ambiguous_layout_is_held_with_files_and_not_overridable(fake, qtbot):
    aid, rid = _album(fake, AMBIGUOUS)
    album = fake.library.album(aid)
    remotes, amb = fake._c.fake.disc_pairing(aid, rid)
    names = sorted(p.name for p in amb)
    assert names == ["01 a.flac", "01 d.flac", "02 b.flac", "02 e.flac"] and remotes[2].position == "1-3"
    c = fake.match.candidates(aid)[0]
    assert {p.local.path.name for p in c.alignment if p.local is not None and p.remote is None} == set(names)
    plan = fake.plan.build([aid])
    (ap,) = plan.albums
    (pr,) = _disc_problems(ap)
    assert sorted(x.name for x in pr.paths) == names and "disc" in pr.message
    assert P.held(ap) and not P.overridable(ap)
    # allow_track_mismatch never clears it
    (ap2,) = fake.plan.build([aid], allow_track_mismatch={aid}).albums
    assert _disc_problems(ap2) and P.held(ap2)
    # match.destination says so before apply
    d = fake.match.destination(aid, rid, fake.active_template())
    assert any(P.kind_of(p) == P.DISC_AMBIGUOUS for p in d.problems)
    # apply holds it
    with qtbot.waitSignal(fake.events.job_finished, check_params_cb=not_fetch, timeout=3000) as sig:
        fake.plan.apply(plan.id)
    assert sig.args[0].state.name == "FAILED"
    held = fake.library.album(aid)
    assert held.status.name == "HELD" and {p.kind.value for p in held.held_for} == {"disc_ambiguous"}
    assert album.folder == held.folder                                   # nothing moved


def test_apply_dialog_shows_disc_ambiguous_files_without_apply_anyway(fake, qtbot):
    aid, rid = _album(fake, AMBIGUOUS)
    good = fake.albums({"MATCHED"}, limit=1).items[0]
    fake.match.choose(good.id, fake.match.candidates(good.id)[0].release_id)
    plan = built(fake, [aid, good.id])
    d = ApplyDialog(plan); qtbot.addWidget(d)
    assert [ap.album_id for ap in d.held_aps] == [aid] and len(d.clean_aps) == 1
    assert d.anyway_btn is None                                           # no "Apply anyway…" for this kind
    assert d.ok_btn.isEnabled() and d.ok_btn.text() == "Apply 1 album(s)"
    (item,) = [d.problem_list.item(i) for i in range(d.problem_list.count())]
    text = item.text()
    assert "Can't work out the disc layout" in text and "Files (4):" in text
    for n in ("01 a.flac", "01 d.flac", "02 b.flac", "02 e.flac"):
        assert f"\n      {n}" in text                                    # relative to the album folder
    row = next(d.tree.topLevelItem(i) for i in range(d.tree.topLevelItemCount())
               if d.tree.topLevelItem(i).data(0, Qt.UserRole) == aid)
    assert row.text(0) == "Held back: Disc Band – Double Trouble" and row.childCount() == 0
    assert "Can't work out the disc layout" in row.toolTip(0) and "01 d.flac" in row.toolTip(0)


def test_apply_anyway_button_explains_disc_ambiguous(fake, qtbot):
    """Mixed batch: one album held only for track_mismatch (overridable) and the disc_ambiguous one, which
    also has a track_mismatch. Only the first can be applied anyway; the second says why not."""
    T = fake.types
    aid, _ = _album(fake, AMBIGUOUS)
    other = fake.albums({"MATCHED", "NEEDS_REVIEW"}, limit=1).items[0]
    fake.match.choose(other.id, fake.match.candidates(other.id)[0].release_id)
    for x in (aid, other.id):
        fake._c.mock_problems[x] = [T.Problem(T.ProblemKind.TRACK_MISMATCH, "Discogs lists 6 tracks, folder has 5",
                                              [fake.library.album(x).folder])]
    plan = built(fake, [aid, other.id])
    d = ApplyDialog(plan); qtbot.addWidget(d)
    assert d.anyway_btn is not None and not d.ok_btn.isEnabled()
    rows = {(d.problem_list.item(i).data(Qt.UserRole), d.problem_list.item(i).data(Qt.UserRole + 1)): i
            for i in range(d.problem_list.count())}
    d.problem_list.setCurrentRow(rows[(aid, "track_mismatch")])
    assert not d.anyway_btn.isEnabled() and "Can't work out the disc layout" in d.anyway_btn.toolTip()
    d.problem_list.setCurrentRow(rows[(other.id, "track_mismatch")])
    assert d.anyway_btn.isEnabled()
    # allow_track_mismatch for both: the mismatch goes, the disc problem stays and the album stays held
    plan2 = fake.plan.build([aid, other.id], allow_track_mismatch={aid, other.id})
    by = {ap.album_id: ap for ap in plan2.albums}
    assert by[other.id].problems == []
    assert [P.kind_of(p) for p in by[aid].problems] == ["disc_ambiguous"] and P.held(by[aid])


def test_review_marks_refused_files_and_previews_the_problem(fake, qtbot):
    aid, _ = _album(fake, AMBIGUOUS)
    w = ReviewWindow(fake); qtbot.addWidget(w)
    w.filter_box.setCurrentText("All")
    row = next(i for i in range(w.model.rowCount()) if "Double Trouble" in w.model.index(i).data())
    w.list.setCurrentIndex(w.model.index(row))
    assert w.album_id == aid
    t = w.table
    marks = [t.item(r, 7).text() for r in range(t.rowCount()) if t.item(r, 7) is not None]
    assert marks.count("disc unknown") == 4
    assert "Can't work out the disc layout" in w.preview.text()


def test_ui_apply_holds_disc_ambiguous_album_and_applies_the_rest(fake, qtbot, monkeypatch):
    aid, _ = _album(fake, AMBIGUOUS)
    good = fake.albums({"MATCHED"}, limit=1).items[0]
    fake.match.choose(good.id, fake.match.candidates(good.id)[0].release_id)
    w = ReviewWindow(fake); qtbot.addWidget(w)
    seen = patch_dialog(monkeypatch, True)
    summaries = []
    monkeypatch.setattr(ReviewWindow, "show_summary", lambda self, t: summaries.append(t))
    with qtbot.waitSignal(fake.events.job_finished, check_params_cb=not_fetch, timeout=3000):
        w.open_apply()
    (dlg,) = seen
    assert aid in [ap.album_id for ap in dlg.held_aps]
    qtbot.waitUntil(lambda: bool(summaries), timeout=1000)
    assert "held back for a look" in summaries[0]
    assert fake.library.album(aid).status.name == "HELD"
    assert fake.library.album(good.id).status.name == "APPLIED"
