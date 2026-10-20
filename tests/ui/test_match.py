"""Match window (PICARD-UI-PLAN v1 #1 and #2) on the fake backend, against the c2fcf25 contract ("Clustering and
album match view" + "Additions of 2026-10-20 (afternoon)"). The real-backend version is in test_real_backend.py."""
from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from mm_ui import problems as P
from mm_ui.match import FETCHING, KEY, MANUAL, MatchWindow
from mm_ui.review import ApplyDialog, ReviewWindow

from test_ui_flows import built

HAM = ("01 Black And White", "02 Followed Around", "03 Lost And Found")


def _demo(fake):
    return fake._c.fake.add_loose_cluster_demo()


def _fetch_all(fake):
    core = fake._c.fake
    while core._fetch_queue:
        core._fetch_one(pace=False)


def _window(fake, qtbot):
    w = MatchWindow(fake); qtbot.addWidget(w)
    w.asked, w.told = [], []
    w.confirm = lambda title, text: w.asked.append(text) or w.answer
    w.inform = lambda title, text: w.told.append(text)
    w.answer = True
    return w


def _group(w, kind):
    return next(w.left.topLevelItem(i) for i in range(w.left.topLevelItemCount())
                if w.left.topLevelItem(i).data(0, KEY) == ("group", kind))


def _adopted(fake, qtbot):
    _demo(fake)
    w = _window(fake, qtbot)
    cid = _group(w, "tag_cluster").child(0).data(0, KEY)[1]
    w.select(cid); w.adopt()
    _fetch_all(fake)
    fake.match.choose(w.view.album_id, w.view.release_id)
    w.show_view()
    return w


def _track(w, position):
    rel = w.tracks.topLevelItem(0)
    return next(rel.child(i) for i in range(rel.childCount()) if rel.child(i).text(0) == position)


def _file_path(w, name):
    v = w.view
    return next(Path(t.path) for t in [p.local for p in v.pairs if p.local] + v.unmatched_files if Path(t.path).stem == name)


# ---------------------------------------------------------------- the calls (mock)
def test_mock_match_clusters_lists_kinds_in_one_page(fake):
    _demo(fake)
    rows = fake.match.clusters(None, 0, 500).items
    tags = [r for r in rows if r.kind == "tag_cluster"]
    assert [(r.artist, r.title, r.file_count, r.status) for r in tags] == [
        ("Henry Rollins", "Hot Animal Machine", 3, None), ("Rollins Band", "Life Time", 1, None)]
    assert all(r.id.startswith("cl-") for r in tags)
    folder = [r for r in rows if r.kind == "folder"]
    assert folder and all(r.id == f"album-{r.album_id}" for r in folder)
    assert not any(r.loose_tracks and r.kind == "folder" for r in rows)       # loose rows aren't listed
    page = fake.match.clusters(fake.types.AlbumFilter(text="rollins"), 0, 1)
    assert page.total == 3 and len(page.items) == 1      # 2 tag clusters + the Hot Animal Machine folder album


def test_mock_adopt_unadopt_round_trip(fake):
    ids = _demo(fake)
    c = fake.library.clusters()[0]
    row = fake.library.adopt_cluster(c.id)
    assert row.cluster_id == c.id and row.status.name == "UNMATCHED" and row.track_count == 3
    assert fake.library.adopt_cluster(c.id).id == row.id
    assert fake.library.album(ids["loose"]).track_count == 1
    back = fake.library.unadopt_cluster(row.id)
    assert back.id == c.id and back.album_id is None
    assert fake.library.album(ids["loose"]).track_count == 4


def test_mock_move_out_and_back_changes_ids(fake, qtbot):
    _demo(fake)
    ham = fake.library.clusters()[0]
    seen = []
    fake.events.cluster_changed.connect(lambda old, new: seen.append((old, new)))
    out = fake.library.move_to_cluster(ham.files[2].path, None)
    assert out.kind == "unclustered" and out.id.startswith("un-") and (out.artist, out.title) == ("", "")
    assert seen[0] == ("", out.id) and seen[1][0] == ham.id and seen[1][1] not in ("", ham.id)
    back = fake.library.move_to_cluster(ham.files[2].path, seen[1][1])
    assert back.id == ham.id


# ---------------------------------------------------------------- v1 #1: the window
def test_left_pane_groups_rows_by_kind(fake, qtbot):
    _demo(fake)
    w = _window(fake, qtbot)
    tags = _group(w, "tag_cluster")
    assert [tags.child(i).text(0) for i in range(tags.childCount())] == [
        "Henry Rollins – Hot Animal Machine", "Rollins Band – Life Time"]
    assert tags.child(0).childCount() == 3
    assert any("Hot Animal Machine" in _group(w, "folder").child(i).text(0) for i in range(_group(w, "folder").childCount()))
    w.select(tags.child(1).data(0, KEY)[1])
    assert "Not adopted yet" in w.header.text() and w.adopt_btn.isEnabled() and not w.unadopt_btn.isEnabled()


def test_adopt_then_fetching_then_pairs_keyed_by_id(fake, qtbot):
    _demo(fake)
    w = _window(fake, qtbot)
    cid = _group(w, "tag_cluster").child(0).data(0, KEY)[1]
    w.select(cid); w.adopt()
    assert w.current == cid and not w.adopt_btn.isEnabled() and w.unadopt_btn.isEnabled()
    assert w.fetch_label.text() == FETCHING and not w.fetch_label.isHidden()
    assert f"[{P.CLUSTER_BADGE}]" in w.item_for(cid).text(0)
    _fetch_all(fake)          # album_changed / candidate_ready -> the view refreshes itself
    assert w.fetch_label.isHidden() and w.view.fetched
    rel = w.tracks.topLevelItem(0)
    assert rel.text(1).startswith("Release tracks (3)") and rel.isExpanded()
    assert {rel.child(i).text(3) for i in range(3)} == {f"{n}.mp3" for n in HAM}


def test_unadopt_returns_to_the_cluster(fake, qtbot):
    w = _adopted(fake, qtbot)
    cid = w.current
    w.unadopt()
    assert w.current == cid and w.view.album_id is None and w.adopt_btn.isEnabled()
    assert f"[{P.CLUSTER_BADGE}]" not in w.item_for(cid).text(0)


# ---------------------------------------------------------------- v1 #2: drag / Pair / unpin
def test_drop_on_empty_track_pins_with_manual_badge(fake, qtbot):
    w = _adopted(fake, qtbot)
    f3 = _file_path(w, HAM[2])
    fake.match.pin(w.view.album_id, _file_path(w, HAM[1]), "B9")      # unknown position: shown, not used
    w.show_view()
    assert any(P.kind_of(p) == P.PIN_CONFLICT for p in w.view.problems)
    assert P.LABELS["pin_conflict"] in w.problems.text()
    fake.match.unpin(w.view.album_id)
    w.drop_right(str(f3), _track(w, "1"))      # "1" had file 01: a swap
    assert _track(w, "1").text(3) == f3.name and _track(w, "1").text(6) == f"✓ {MANUAL}"
    assert _track(w, "3").text(3) == f"{HAM[0]}.mp3" and _track(w, "3").text(6) == f"✓ {MANUAL}"
    assert {Path(p.path).name for p in fake.match.pins(w.view.album_id)} == {f3.name, f"{HAM[0]}.mp3"}
    assert not w.asked


def test_keyboard_pair_and_unpin(fake, qtbot):
    w = _adopted(fake, qtbot)
    it = _track(w, "2"); w.tracks.setCurrentItem(it)
    assert w.pair_btn.isEnabled() and not w.unpin_btn.isEnabled()
    assert w.pair_selected("2")                    # already there: nothing to do
    assert w.pair_selected("3")
    assert _track(w, "3").text(3) == f"{HAM[1]}.mp3" and _track(w, "2").text(3) == f"{HAM[2]}.mp3"
    w.tracks.setCurrentItem(_track(w, "3"))
    assert w.unpin_btn.isEnabled()
    w.unpin_selected()
    assert _track(w, "3").text(6) != f"✓ {MANUAL}"


def test_drop_on_unclustered_unpins_then_moves_out(fake, qtbot):
    w = _adopted(fake, qtbot)
    aid = w.view.album_id
    f1 = _file_path(w, HAM[0])
    w.drop_right(str(f1), _track(w, "2"))
    assert fake.match.pins(aid)
    w.drop_left(str(f1), _group(w, "unclustered"))       # pinned: only unpins
    assert f1 not in [p.path for p in fake.match.pins(aid)] and fake.library.album(aid).track_count == 3
    w.drop_left(str(f1), _group(w, "unclustered"))       # not pinned: moves it out of the cluster
    assert fake.library.album(aid).track_count == 2
    un = _group(w, "unclustered")
    assert un.childCount() == 1 and un.child(0).child(0).text(0) == f1.name
    assert w.view is not None and w.view.album_id == aid         # followed the cluster's new id


def _held_disc_album(fake):
    core = fake._c.fake
    tracks = [(1, 1, "One", 200), (1, 2, "Two", 210), (1, 3, "Three", 220),
              (2, 1, "Four", 230), (2, 2, "Five", 240), (2, 3, "Six", 250)]
    files = [("CD1/01 One", None, 200), ("CD1/02 Two", None, 210), ("CD1/03 Three", None, 220),
             ("CD2/01 Four,Five", None, 470), ("CD2/02 Six", None, 250)]
    aid = core.add_multi_disc_album("Disc Band", "Medley", "1999", files, tracks)
    rid = fake.match.candidates(aid)[0].release_id
    core._fetch_queue = [(aid, rid)]; core._fetch_one(pace=False)
    fake.match.choose(aid, rid)
    return aid


@pytest.mark.parametrize("answer", [True, False])
def test_pin_over_a_disc_hold_asks_first(fake, qtbot, answer):
    aid = _held_disc_album(fake)
    w = _window(fake, qtbot); w.answer = answer
    w.select(f"album-{aid}")
    medley = next(Path(t.path) for t in w.view.unmatched_files if "Four,Five" in Path(t.path).name)
    w.drop_right(str(medley), _track(w, "2-1"))
    assert len(w.asked) == 1 and "Override the disc hold" not in w.asked[0] and medley.name in w.asked[0]
    pins = fake.match.pins(aid)
    if answer:
        assert [(p.position, p.override_hold) for p in pins] == [("2-1", True)]
        assert _track(w, "2-1").text(6) == f"✓ {MANUAL}"
        assert not any(P.kind_of(p) == P.PIN_CONFLICT for p in w.view.problems)
    else:
        assert pins == []


def test_never_overrides_across_disc_folders(fake, qtbot):
    aid = _held_disc_album(fake)
    w = _window(fake, qtbot)
    w.select(f"album-{aid}")
    one = next(Path(p.local.path) for p in w.view.pairs if p.local and Path(p.local.path).name.startswith("01 One"))
    w.drop_right(str(one), _track(w, "2-3"))
    assert not w.asked and len(w.told) == 1 and "another disc's folder" in w.told[0]
    assert fake.match.pins(aid) == []


# ---------------------------------------------------------------- badges, apply dialog, history
def test_cluster_badge_in_library_and_review(fake, qtbot):
    from mm_ui.library import LibraryWindow
    _demo(fake)
    c = fake.library.clusters()[0]
    row = fake.library.adopt_cluster(c.id)
    lib = LibraryWindow(fake); qtbot.addWidget(lib)
    titles = [lib.model.data(lib.model.index(r, 1)) for r in range(lib.model.rowCount())]
    assert f"Hot Animal Machine  [{P.CLUSTER_BADGE}]" in titles
    fake.match.find([row.id]); _fetch_all(fake)
    rw = ReviewWindow(fake); qtbot.addWidget(rw)
    names = [rw.model.data(rw.model.index(r, 0)) for r in range(rw.model.rowCount())]
    assert any(f"[{P.CLUSTER_BADGE}]" in n for n in names)


def test_apply_dialog_says_only_cluster_files_move(fake, qtbot):
    _demo(fake)
    c = fake.library.clusters()[0]
    row = fake.library.adopt_cluster(c.id)
    fake.match.find([row.id]); _fetch_all(fake)
    fake.match.choose(row.id, fake.match.candidates(row.id)[0].release_id)
    plan = built(fake, [row.id])
    assert not plan.albums[0].problems      # moving out of the shared folder is not "inside the source"
    dlg = ApplyDialog(plan, album_row=fake.library.album); qtbot.addWidget(dlg)
    text = dlg.cluster_label.text()
    assert "is a cluster: only its 3 file(s) move" in text and "(their common folder)" in text
    assert P.NON_AUDIO_STAYS in text and str(c.folder) in text


def test_pinned_pairs_reach_plan_and_history(fake, qtbot):
    from mm_ui.history import HistoryDialog
    w = _adopted(fake, qtbot)
    aid = w.view.album_id
    w.drop_right(str(_file_path(w, HAM[2])), _track(w, "1"))
    plan = built(fake, [aid])
    assert {Path(f.path_before).stem for f in plan.albums[0].files if f.pinned} == {HAM[2], HAM[0]}
    job = fake.plan.apply(plan.id); fake.jobs.wait(job.id, 5)
    assert fake.match.pins(aid) == []                     # used up by the apply
    h = HistoryDialog(fake); qtbot.addWidget(h)
    h.table.selectRow(0)
    assert not h.manual_label.isHidden()
    assert f"{HAM[2]}.mp3 → 1" in h.manual_label.text() and "Paired by you" in h.manual_label.text()


def test_file_rows_drag_their_path_and_tracks_accept_drops(fake, qtbot):
    from mm_ui.match import MIME
    w = _adopted(fake, qtbot)
    it = _track(w, "2")
    md = w.tracks.mimeData([it])
    assert bytes(md.data(MIME)).decode() == str(_file_path(w, HAM[1]))
    assert it.flags() & Qt.ItemIsDragEnabled and it.flags() & Qt.ItemIsDropEnabled
    rel = w.tracks.topLevelItem(0)
    assert not rel.flags() & Qt.ItemIsDragEnabled
    f = _group(w, "tag_cluster").child(1).child(0)          # a loose file on the left drags too
    assert bytes(w.left.mimeData([f]).data(MIME)).decode().endswith("Gone Fishin'.mp3")


# ---------------------------------------------------------------- 1f7b340: pin_conflicts, album_removed
def test_every_contract_reason_has_a_message():
    import re
    from test_backend import CONTRACT
    if not CONTRACT.is_file():
        pytest.skip("contract not present")
    block = re.search(r"class PinConflict:.*?reason: Literal\[(.*?)\]", CONTRACT.read_text(), re.S).group(1)
    assert set(re.findall(r'"(\w+)"', block)) == set(P.PIN_REASONS)


def test_conflicts_are_explained_by_reason_and_only_hold_asks(fake, qtbot):
    w = _adopted(fake, qtbot)
    aid = w.view.album_id
    f2 = _file_path(w, HAM[1])
    assert not w.pair(f2, "9") and not w.asked                 # unknown position: told, pin undone
    assert "has no track 9" in w.told[-1] and fake.match.pins(aid) == []
    fake.match.pin(aid, f2, "9"); w.show_view()
    (c,) = w.conflict_for(f2) and [w.conflict_for(f2)]
    assert c.reason == "unknown_position"
    assert P.pin_conflict_text(c) in w.problems.text()


def test_album_removed_drops_rows_everywhere(fake, qtbot):
    from mm_ui.library import LibraryWindow
    w = _adopted(fake, qtbot)
    aid = w.view.album_id
    lib = LibraryWindow(fake); qtbot.addWidget(lib)
    rw = ReviewWindow(fake); qtbot.addWidget(rw); rw.filter_box.setCurrentText("All")
    ids = lambda m: [r.id for r in m.rows]     # noqa: E731
    assert aid in ids(lib.model) and aid in ids(rw.model)
    removed = []
    fake.events.album_removed.connect(removed.append)
    w.unadopt()
    assert removed == [aid] and aid not in ids(lib.model) and aid not in ids(rw.model)
    assert w.view is not None and w.view.album_id is None
