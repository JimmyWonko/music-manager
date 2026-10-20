"""ProblemKind.DISC_TRACK_COUNT (1566b36, API-CONTRACT "Behaviour changes of 2026-10-19" item 2) on the fake
backend: a disc whose file count differs from its track count, with titles that don't pair every file, is left
unpaired and holds the album. Not overridable. The real-backend version is in test_real_backend.py."""
from mm_ui import problems as P
from mm_ui import fake_service as FS
from mm_ui.review import ApplyDialog, ReviewWindow

from test_ui_flows import built

TRACKS = [(1, 1, "One", 200), (1, 2, "Two", 210), (1, 3, "Three", 220),
          (2, 1, "Four", 230), (2, 2, "Five", 240), (2, 3, "Six", 250)]
DISC1 = [("CD1/01 One", None, 200), ("CD1/02 Two", None, 210), ("CD1/03 Three", None, 220)]
MEDLEY = DISC1 + [("CD2/01 Four,Five", None, 470), ("CD2/02 Six", None, 250)]     # 2 files, 3 tracks
MISSING = DISC1 + [("CD2/01 Four", None, 230), ("CD2/03 Six", None, 250)]          # a file is missing


def _album(fake, files, title):
    core = fake._c.fake
    aid = core.add_multi_disc_album("Disc Band", title, "1999", files, TRACKS)
    rid = fake.match.candidates(aid)[0].release_id
    core._fetch_queue = [(aid, rid)]; core._fetch_one(pace=False)
    fake.match.choose(aid, rid)
    return aid, rid


def _count(ap):
    return [p for p in ap.problems if P.kind_of(p) == P.DISC_TRACK_COUNT]


def test_kind_appended_last_and_labelled():
    assert [m.value for m in FS.ProblemKind][-1] == "disc_track_count"
    assert P.LABELS["disc_track_count"] == "A disc's file count doesn't match its tracks"
    assert len(set(P.LABELS.values())) == len(P.LABELS)


def test_medley_disc_is_held_unpaired_and_not_overridable(fake, qtbot):
    aid, rid = _album(fake, MEDLEY, "Medley Night")
    folder = fake.library.album(aid).folder
    plan = built(fake, [aid])
    (ap,) = plan.albums
    (p,) = _count(ap)
    assert [x.relative_to(folder).as_posix() for x in p.paths] == ["CD2/01 Four,Five.flac", "CD2/02 Six.flac"]
    assert "disc 2 (CD2/) has 2 files but the release lists 3 tracks" in p.message
    assert P.held(ap) and not P.overridable(ap) and P.label(p) in P.blocking_reasons(ap)
    assert _count(built(fake, [aid], allow_track_mismatch={aid}).albums[0])          # never overridden
    assert any(P.kind_of(x) == P.DISC_TRACK_COUNT for x in fake.match.destination(aid, rid, fake.active_template()).problems)
    d = ApplyDialog(plan); qtbot.addWidget(d)
    t = d.problem_list.item(0).text()
    assert t.startswith("⚠ Disc Band – Medley Night: A disc's file count doesn't match its tracks")
    assert "Files on that disc, none paired (2):\n      CD2/01 Four,Five.flac\n      CD2/02 Six.flac" in t
    assert P.DISC_COUNT_HINT in t and d.anyway_btn is None and not d.ok_btn.isEnabled()
    # disc 1 still pairs; disc 2's files are left unpaired, never shifted
    c = fake.match.candidates(aid)[0]
    unpaired = {pr.local.path.name for pr in c.alignment if pr.local is not None and pr.remote is None}
    assert unpaired == {"01 Four,Five.flac", "02 Six.flac"}
    w = ReviewWindow(fake); qtbot.addWidget(w)
    w.filter_box.setCurrentText("All")
    i = next(i for i in range(w.model.rowCount()) if "Medley Night" in w.model.index(i).data())
    w.list.setCurrentIndex(w.model.index(i))
    marks = [w.table.item(r, 7).text() for r in range(w.table.rowCount()) if w.table.item(r, 7) is not None]
    assert marks.count("disc count differs") == 2, marks
    assert "A disc's file count doesn't match its tracks" in w.preview.text()


def test_missing_file_with_matching_titles_still_pairs(fake):
    aid, _rid = _album(fake, MISSING, "One Short")
    (ap,) = built(fake, [aid]).albums
    assert not _count(ap)
