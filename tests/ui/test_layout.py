"""ProblemKind.LAYOUT_AMBIGUOUS (68cf4d7, API-CONTRACT "Album folders with loose audio and subfolders") on the
fake backend: loose audio next to disc folders / other album folders holds the album ("Loose tracks next to
album folders"), lists the files involved, is never overridable, and the lists name the loose-files "album"
by its folder with a hint while its subfolders are listed as albums of their own. Real backend: in
test_real_backend.py."""
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from PySide6.QtCore import Qt

from mm_ui import fake_service as FS
from mm_ui import problems as P
from mm_ui.library import LibraryWindow
from mm_ui.review import ApplyDialog, LayoutIndex, ReviewWindow

from test_ui_flows import built, not_fetch, patch_dialog


@pytest.fixture
def lay(fake):
    ids = fake._c.fake.add_loose_layouts()
    core = fake._c.fake
    for k in ("stray", "buzz", "tie", "mixed", "bonus"):
        rid = fake.match.candidates(ids[k])[0].release_id
        core._fetch_queue = [(ids[k], rid)]; core._fetch_one(pace=False)
        fake.match.choose(ids[k], rid)
    return ids


def _lp(ap):
    return [p for p in ap.problems if P.kind_of(p) == P.LAYOUT_AMBIGUOUS]


def _apply(fake, qtbot, ids):
    plan = built(fake, ids)
    with qtbot.waitSignal(fake.events.job_finished, check_params_cb=not_fetch, timeout=3000) as sig:
        fake.plan.apply(plan.id)
    return plan, sig.args[0]


def test_layout_ambiguous_kind_label_and_helpers():
    assert FS.ProblemKind.LAYOUT_AMBIGUOUS.value == "layout_ambiguous"
    assert P.LABELS["layout_ambiguous"] == "Loose tracks next to album folders"
    assert len(set(P.LABELS.values())) == len(P.LABELS)
    bf = Path("/m/Black Flag")
    stray = FS.Problem(FS.ProblemKind.LAYOUT_AMBIGUOUS, FS.STRAY_AUDIO.format(folders="whatever/"), [bf],
                       layout_reason="album_folders", blocking_subdirs=[bf / "[1984]  My War [SST023]", bf / "Live (85)"],
                       loose_files=[bf / "stray.mp3"])
    x = Path("/m/x")
    mixed = FS.Problem(FS.ProblemKind.LAYOUT_AMBIGUOUS, "any wording", [x], layout_reason="disc_folders",
                       blocking_subdirs=[x / "CD1", x / "CD2"], loose_files=[x / "01 One.flac"])
    # ccfa4a8: structured fields; the message is never parsed
    assert P.layout_dirs(stray) == ["[1984]  My War [SST023]", "Live (85)"] and P.layout_shape(stray) == "folders"
    assert P.layout_dirs(mixed) == ["CD1", "CD2"] and P.layout_shape(mixed) == "discs"
    assert P.loose_files(stray) == [bf / "stray.mp3"]
    assert not P.overridable(NS(problems=[stray])) and P.blocking_reasons(NS(problems=[stray])) == [P.label(stray)]
    # names (d6ac2f3): keyed on AlbumRow.loose_tracks, never on the folder's names or the held state
    row = NS(folder=bf, artist="Black Flag", title="", loose_tracks=True)
    assert P.display_names(row) == P.display_names(row, stray) == ("Black Flag", "Loose tracks, not an album")
    assert P.row_label(*P.display_names(row), loose=True) == "Black Flag: Loose tracks, not an album"
    old_shape = NS(folder=bf, artist="m", title="Black Flag")          # pre-d6ac2f3 names, no field: no guessing
    assert P.display_names(old_shape, stray) == ("m", "Black Flag")
    real_album = NS(folder=Path("/m/HD/[1984]  Zen Arcade [SST027]"), artist="HD", title="", loose_tracks=False)
    assert P.display_names(real_album) == ("HD", "")                   # an empty title alone means nothing
    assert P.display_names(NS(folder=x, artist="X", title="Y", loose_tracks=False), mixed) == (
        "X", "Y · loose tracks next to disc folders")
    bonus = NS(folder=Path("/m/HD/[1984]  Zen Arcade [SST027]/Bonus"), artist="", title="Bonus")
    assert P.display_names(bonus) == ("", "[1984]  Zen Arcade [SST027]/Bonus")
    files = [x / "01 One.flac", x / "CD1/02 Two.flac"]
    assert P.layout_files(mixed, x, files) == ["Loose files (1):", "  01 One.flac", "In the subfolders (1):",
                                               "  CD1/02 Two.flac", "Next to disc folders (2):", "  CD1/", "  CD2/"]


def test_layout_fallback_when_fields_are_empty():
    """An older service (68cf4d7..1ad0940) sends only kind/message/paths: no subfolder names are read out of
    the message; the shape and loose files come from the album's files, and the message still names them."""
    x = Path("/m/x")
    old_mixed = NS(kind="layout_ambiguous", message=FS.MIXED_DISCS.format(discs="CD1/, CD2/"), paths=[x])
    files = [x / "01 One.flac", x / "CD1/02 Two.flac"]
    assert P.layout_dirs(old_mixed) == [] and P.layout_shape(old_mixed) == "folders"     # no files: no guess
    assert P.layout_shape(old_mixed, files) == "discs" and P.loose_files(old_mixed, files) == [x / "01 One.flac"]
    assert P.layout_files(old_mixed, x, files) == ["Loose files (1):", "  01 One.flac", "In the subfolders (1):",
                                                   "  CD1/02 Two.flac"]
    old_stray = NS(kind="layout_ambiguous", message=FS.STRAY_AUDIO.format(folders="[1984]  My War [SST023]/"),
                   paths=[Path("/m/Black Flag")])
    assert P.layout_dirs(old_stray) == [] and P.layout_shape(old_stray, [Path("/m/Black Flag/stray.mp3")]) == "folders"
    import inspect
    src = inspect.getsource(P)
    assert "other album folders (" not in src and "disc folders with audio (" not in src   # no message parsing


def test_mock_scan_lists_subfolders_as_their_own_albums(fake, lay):
    core = fake._c.fake
    a = {k: fake.library.album(v) for k, v in lay.items()}
    assert a["stray"].folder.name == "Minutemen" and [t.path.name for t in a["stray"].tracks] == ["demo take.mp3"]
    assert a["stray"].extras == []                                       # no extras: the albums aren't moved with it
    assert a["buzz"].folder.parent == a["stray"].folder and a["tie"].folder.parent == a["stray"].folder
    assert [t.path.relative_to(a["mixed"].folder).as_posix() for t in a["mixed"].tracks] == [
        "01 Split Myself in Two.mp3", "CD1/02 Magic Toy Missing.mp3", "CD2/01 Lost.mp3"]   # loose first, then discs
    assert a["bonus_dir"].folder.parent == a["bonus"].folder and a["bonus_dir"].title == "Bonus"
    assert core.layout_problem(lay["buzz"]) == "" and "other album folders" in core.layout_problem(lay["stray"])


def test_mock_plan_holds_layout_ambiguous_and_never_overrides(fake, qtbot, lay):
    ids = [lay["stray"], lay["mixed"], lay["bonus"], lay["buzz"], lay["tie"]]
    plan = built(fake, ids)
    by = {ap.album_id: ap for ap in plan.albums}
    for k in ("stray", "mixed", "bonus"):
        (p,) = _lp(by[lay[k]])
        assert p.paths == [fake.library.album(lay[k]).folder] and P.held(by[lay[k]]) and not P.overridable(by[lay[k]])
        assert by[lay[k]].problems[0] is p                                # first, like album_target
    assert P.layout_shape(_lp(by[lay["mixed"]])[0]) == "discs"
    assert not _lp(by[lay["buzz"]]) and not _lp(by[lay["tie"]])
    plan2 = built(fake, ids, allow_track_mismatch=set(ids))
    assert all(_lp(ap) for ap in plan2.albums if ap.album_id in (lay["stray"], lay["mixed"], lay["bonus"]))
    d = fake.match.destination(lay["mixed"], fake.match.candidates(lay["mixed"])[0].release_id, fake.active_template())
    assert _lp(d)
    folders = {k: fake.library.album(lay[k]).folder for k in lay}
    _plan, job = _apply(fake, qtbot, [lay["mixed"], lay["buzz"]])
    assert job.state.name == "DONE"
    m = fake.library.album(lay["mixed"])
    assert m.status.name == "HELD" and {p.kind.value for p in m.held_for} == {"layout_ambiguous"}
    assert m.folder == folders["mixed"]                                  # never moved or renamed
    assert fake.library.album(lay["buzz"]).status.name == "APPLIED"


def test_apply_dialog_lists_loose_files_without_apply_anyway(fake, qtbot, lay):
    w = ReviewWindow(fake); qtbot.addWidget(w)
    plan = built(fake, [lay["stray"], lay["mixed"], lay["tie"]])
    d = ApplyDialog(plan, album_name=w.album_name, album_row=w.album_row); qtbot.addWidget(d)
    assert {ap.album_id for ap in d.held_aps} == {lay["stray"], lay["mixed"]}
    assert d.anyway_btn is None and d.ok_btn.isEnabled() and d.ok_btn.text() == "Apply 1 album(s)"
    items = {(d.problem_list.item(i).data(Qt.UserRole), d.problem_list.item(i).data(Qt.UserRole + 1)):
             d.problem_list.item(i).text() for i in range(d.problem_list.count())}
    st = items[(lay["stray"], "layout_ambiguous")]
    assert st.startswith("⚠ Minutemen: Loose tracks, not an album — Loose tracks next to album folders"), st
    assert "Loose files (1):\n      demo take.mp3" in st
    assert "Next to album folders (2):" in st and "[1985]  3-Way Tie (For Last) [SST058]/" in st
    mx = items[(lay["mixed"], "layout_ambiguous")]
    assert "Meat Puppets – Meat Puppets II · loose tracks next to disc folders" in mx
    assert "In the subfolders (2):" in mx and "CD2/01 Lost.mp3" in mx and "Next to disc folders (2):" in mx
    rows = {d.tree.topLevelItem(i).data(0, Qt.UserRole): d.tree.topLevelItem(i) for i in range(d.tree.topLevelItemCount())}
    assert rows[lay["stray"]].text(0) == "Held back: Minutemen: Loose tracks, not an album"
    assert rows[lay["stray"]].childCount() == 0 and "demo take.mp3" in rows[lay["stray"]].toolTip(0)
    # no library lookup: AlbumPlan.artist (the folder's name, title "") + the plan's album_folders reason
    ap = next(ap for ap in plan.albums if ap.album_id == lay["stray"])
    assert (ap.artist, ap.title) == ("Minutemen", "")
    d2 = ApplyDialog(plan); qtbot.addWidget(d2)
    assert any(d2.tree.topLevelItem(i).text(0) == "Held back: Minutemen: Loose tracks, not an album"
               for i in range(d2.tree.topLevelItemCount()))


def test_lists_name_held_loose_folder_and_its_subfolder_albums(fake, qtbot, lay):
    _apply(fake, qtbot, [lay["stray"], lay["bonus"]])
    assert fake.library.album(lay["stray"]).status.name == "HELD"
    idx = LayoutIndex(fake)
    assert set(idx.loose) == {fake.library.album(lay["stray"]).folder, fake.library.album(lay["bonus"]).folder}
    lib = LibraryWindow(fake); qtbot.addWidget(lib)
    m = lib.model
    shown = {m.rows[i].id: (m.index(i, 0).data(), m.index(i, 1).data(), m.index(i, 1).data(Qt.ToolTipRole))
             for i in range(m.rowCount())}
    assert shown[lay["stray"]][:2] == ("Minutemen", "Loose tracks, not an album")
    assert "Next to: [1983]  Buzz or Howl" in shown[lay["stray"]][2]
    assert shown[lay["buzz"]][:2] == ("Minutemen", "Buzz or Howl Under the Influence of Heat")
    assert "Listed as its own album" in shown[lay["buzz"]][2] and "Minutemen/" in shown[lay["buzz"]][2]
    assert shown[lay["bonus"]][:2] == ("[1984]  Zen Arcade [SST027]", "Loose tracks, not an album")
    assert shown[lay["bonus_dir"]][:2] == ("", "[1984]  Zen Arcade [SST027]/Bonus")
    assert shown[lay["mixed"]][:2] == ("Meat Puppets", "Meat Puppets II")         # not held yet: as scanned
    # review list: "Needs a look"
    w = ReviewWindow(fake); qtbot.addWidget(w)
    w.filter_box.setCurrentText("Needs a look")
    texts = [w.model.index(i).data() for i in range(w.model.rowCount())]
    assert any(t.startswith("Minutemen: Loose tracks, not an album") and t.endswith("⚠ Needs a look") for t in texts)
    row = next(i for i, t in enumerate(texts) if t.startswith("Minutemen: Loose"))
    w.list.setCurrentIndex(w.model.index(row))
    assert "Loose tracks next to album folders" in w.preview.text() and "demo take.mp3" in w.preview.text()
    assert "Loose tracks next to album folders" in w.model.index(row).data(Qt.ToolTipRole)
    w.filter_box.setCurrentText("All")
    texts = [w.model.index(i).data() for i in range(w.model.rowCount())]
    assert any(t.startswith("[1984]  Zen Arcade [SST027]/Bonus") for t in texts)


def test_loose_tracks_named_before_any_plan(fake, qtbot, lay):
    """d6ac2f3: the scan already says loose_tracks, so every screen names it before a plan exists or it's held."""
    a = fake.library.album(lay["stray"])
    assert a.loose_tracks and (a.artist, a.title, a.year, a.catno) == ("Minutemen", "", "", "")
    assert a.status.name != "HELD" and not a.held_for
    assert not fake.library.album(lay["mixed"]).loose_tracks and not fake.library.album(lay["buzz"]).loose_tracks
    lib = LibraryWindow(fake); qtbot.addWidget(lib)
    m = lib.model
    i = next(i for i in range(m.rowCount()) if m.rows[i].id == lay["stray"])
    assert (m.index(i, 0).data(), m.index(i, 1).data()) == ("Minutemen", "Loose tracks, not an album")
    assert m.index(i, 2).data() in ("", None) and m.index(i, 3).data() in ("", None)      # no year / catno
    w = ReviewWindow(fake); qtbot.addWidget(w)
    w.filter_box.setCurrentText("All")
    texts = [w.model.index(j).data() for j in range(w.model.rowCount())]
    t = next(t for t in texts if t.startswith("Minutemen: Loose tracks, not an album"))
    assert "Needs a look" not in t
    assert w.album_name(lay["stray"]) == "Minutemen: Loose tracks, not an album"
    assert w.album_name(lay["buzz"]) == "Minutemen – Buzz or Howl Under the Influence of Heat"


def test_review_preview_names_files_before_apply(fake, qtbot, lay):
    w = ReviewWindow(fake); qtbot.addWidget(w)
    w.filter_box.setCurrentText("All")
    row = next(i for i in range(w.model.rowCount()) if "Meat Puppets II" in w.model.index(i).data())
    w.list.setCurrentIndex(w.model.index(row))
    t = w.preview.text()
    assert "Loose tracks next to album folders" in t and "CD1/02 Magic Toy Missing.mp3" in t and "CD1/" in t


def test_ui_apply_holds_layout_album_and_applies_the_rest(fake, qtbot, monkeypatch, lay):
    for k in ("buzz", "mixed", "bonus"):          # only the loose-files folder and one clean album are accepted
        fake.match.skip(lay[k])
    w = ReviewWindow(fake); qtbot.addWidget(w)
    seen = patch_dialog(monkeypatch, True)
    summaries = []
    monkeypatch.setattr(ReviewWindow, "show_summary", lambda self, t: summaries.append(t))
    with qtbot.waitSignal(fake.events.job_finished, check_params_cb=not_fetch, timeout=3000):
        w.open_apply()
    (dlg,) = seen
    assert lay["stray"] in [ap.album_id for ap in dlg.held_aps] and lay["tie"] in [ap.album_id for ap in dlg.clean_aps]
    qtbot.waitUntil(lambda: bool(summaries), timeout=1000)
    assert "held back for a look" in summaries[0]
    assert fake.library.album(lay["stray"]).status.name == "HELD"
    assert fake.library.album(lay["tie"]).status.name == "APPLIED"
