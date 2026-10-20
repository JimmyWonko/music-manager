"""Match window (PICARD-UI-PLAN v1 #1) and the clustering calls (c334bbf, API-CONTRACT "Clustering and album
match view (2026-10-20)") on the fake backend. The real-backend version is in test_real_backend.py."""
from mm_ui import problems as P
from mm_ui.match import FETCHING, KEY, MatchWindow
from mm_ui.review import ApplyDialog, ReviewWindow

from test_ui_flows import built


def _demo(fake):
    return fake._c.fake.add_loose_cluster_demo()


def _fetch_all(fake):
    core = fake._c.fake
    while core._fetch_queue:
        core._fetch_one(pace=False)


def test_mock_clusters_split_loose_tracks_by_album_tag(fake):
    _demo(fake)
    cs = fake.library.clusters()
    assert [(c.artist, c.title, len(c.files), c.album_id) for c in cs] == [
        ("Henry Rollins", "Hot Animal Machine", 3, None), ("Rollins Band", "Life Time", 1, None)]
    assert all(c.id.startswith("cl-") and len(c.id) == 15 for c in cs)
    assert fake.library.cluster(cs[0].id) == cs[0]
    v = fake.match.view(cs[0].id)
    assert (v.album_id, v.release_id, v.candidates, v.pairs, len(v.unmatched_files)) == (None, None, [], [], 3)


def test_mock_adopt_is_idempotent_and_takes_files_from_loose_row(fake):
    ids = _demo(fake)
    c = fake.library.clusters()[0]
    row = fake.library.adopt_cluster(c.id)
    assert row.cluster_id == c.id and row.status.name == "UNMATCHED" and row.track_count == 3
    assert fake.library.adopt_cluster(c.id).id == row.id
    assert fake.library.album(ids["loose"]).track_count == 1
    assert [x.album_id for x in fake.library.clusters()] == [row.id, None]


def test_match_window_lists_clusters_and_unmatched_files(fake, qtbot):
    _demo(fake)
    w = MatchWindow(fake); qtbot.addWidget(w)
    groups, loose = w.left.topLevelItem(0), w.left.topLevelItem(1)
    assert groups.child(0).text(0) == "Henry Rollins – Hot Animal Machine" and groups.child(0).childCount() == 3
    assert loose.childCount() == 1 and loose.child(0).text(0).startswith("01 Gone Fishin'")
    w.select(loose.child(0).data(0, KEY)[1])
    assert "Not adopted yet" in w.header.text() and w.adopt_btn.isEnabled()


def test_adopt_then_fetching_then_pairs_keyed_by_id(fake, qtbot):
    _demo(fake)
    w = MatchWindow(fake); qtbot.addWidget(w)
    cid = w.left.topLevelItem(0).child(0).data(0, KEY)[1]
    w.select(cid); w.adopt()
    assert w.current == cid and not w.adopt_btn.isEnabled()
    assert w.fetch_label.text() == FETCHING and not w.fetch_label.isHidden()
    assert w.item_for(cid).text(0).endswith(f"[{P.CLUSTER_BADGE}]")
    _fetch_all(fake)          # album_changed -> the view refreshes itself
    assert w.fetch_label.isHidden() and w.view.fetched
    rel = w.tracks.topLevelItem(0)
    assert rel.text(1).startswith("Release tracks (3)") and rel.isExpanded()
    assert {rel.child(i).text(3) for i in range(3)} == {f"{n}.mp3" for n in
                                                      ("01 Black And White", "02 Followed Around", "03 Lost And Found")}
    assert all(rel.child(i).text(5).endswith("%") for i in range(3))


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
    rid = fake.match.candidates(row.id)[0].release_id
    fake.match.choose(row.id, rid)
    plan = built(fake, [row.id])
    assert not plan.albums[0].problems      # moving out of the shared folder is not "inside the source"
    dlg = ApplyDialog(plan, album_row=fake.library.album); qtbot.addWidget(dlg)
    text = dlg.cluster_label.text()
    assert not dlg.cluster_label.isHidden()
    assert "is a cluster: only its 3 file(s) move" in text and "(their common folder)" in text
    assert P.NON_AUDIO_STAYS in text and str(c.folder) in text
