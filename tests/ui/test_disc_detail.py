"""Structured Problem detail (ccfa4a8 / 2b82859) on the fake backend:
- Problem.disc_conflicts (DiscConflict + DiscChoice) for disc_ambiguous: a per-file reason in plain words and
  the disc/track choices with their title similarity, incl. sources_disagree (folder vs tag vs cue/m3u);
- Problem.layout_reason / blocking_subdirs / loose_files for layout_ambiguous (no message parsing);
- ProblemKind.DISC_FOLDER_MISMATCH + Problem.folder_mismatches: "13 Mason Jar.flac → CD2/ (disc tag 1)", held,
  never overridden, and a button that opens the naming settings;
- nothing depends on ProblemKind's position. Real-backend versions: test_real_backend.py."""
from enum import StrEnum
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog

from mm_ui import fake_service as FS
from mm_ui import problems as P
from mm_ui.naming import NamingDialog
from mm_ui.review import ApplyDialog, ReviewWindow, disc_tooltip

from test_multidisc import AMBIGUOUS, LAYOUTS, TRACKS
from test_ui_flows import built, not_fetch


def _album(fake, files, tracks=TRACKS, title="Double Trouble", sidecars=()):
    core = fake._c.fake
    aid = core.add_multi_disc_album("Disc Band", title, "1999", files, tracks, sidecars=sidecars)
    rid = fake.match.candidates(aid)[0].release_id
    core._fetch_queue = [(aid, rid)]; core._fetch_one(pace=False)
    fake.match.choose(aid, rid)
    return aid, rid


def _problem(fake, aid, kind):
    (ap,) = fake.plan.build([aid]).albums
    return next((p for p in ap.problems if P.kind_of(p) == kind), None), ap


def _reasons(p):
    return {c.path.name: c.reason for c in p.disc_conflicts}


# ---------------------------------------------------------------- types / enum position
def test_new_problem_fields_default_empty_and_kinds_appended_last():
    import dataclasses
    pr = FS.Problem(FS.ProblemKind.COLLISION, "m", [])
    assert (pr.disc_conflicts, pr.layout_reason, pr.blocking_subdirs, pr.loose_files, pr.folder_mismatches) == (
        [], "", [], [], [])
    assert [f.name for f in dataclasses.fields(FS.Problem)] == [
        "kind", "message", "paths", "disc_conflicts", "layout_reason", "blocking_subdirs", "loose_files",
        "folder_mismatches"]
    assert [f.name for f in dataclasses.fields(FS.DiscConflict)] == [
        "path", "number", "title", "reason", "same_number", "title_matches", "folder_disc", "tag_disc",
        "sidecar_disc", "sidecar"]
    assert [f.name for f in dataclasses.fields(FS.DiscChoice)] == ["disc", "position", "number", "title",
                                                                  "title_similarity"]
    assert [f.name for f in dataclasses.fields(FS.DiscFolderMismatch)] == ["path", "destination", "folder",
                                                                          "folder_disc", "tag_disc"]
    assert [m.value for m in FS.ProblemKind][-3:] == ["disc_ambiguous", "layout_ambiguous", "disc_folder_mismatch"]
    assert P.LABELS["disc_folder_mismatch"] == "Tracks would go into another disc's folder"
    assert len(set(P.LABELS.values())) == len(P.LABELS)


def test_ui_matches_kinds_by_value_not_position():
    """A backend whose enum lists the kinds in another order (or sends plain strings) gets the same labels,
    holds and overrides."""
    members = [(m.name, m.value) for m in FS.ProblemKind]
    Shuffled = StrEnum("Shuffled", list(reversed(members)))
    for name, value in members:
        for kind in (Shuffled[name], value):
            pr = NS(kind=kind, message="m", paths=[])
            assert P.kind_of(pr) == value and P.label(pr) == P.LABELS.get(value, P.label(pr))
            ap = NS(problems=[pr])
            assert P.overridable(ap) == (value == "track_mismatch")
            assert P.held(ap) == (value != "release_not_fetched") and P.waiting(ap) == (value == "release_not_fetched")
    import inspect
    for mod in (P,):
        src = inspect.getsource(mod)
        assert "list(ProblemKind" not in src and ".index(" not in src


# ---------------------------------------------------------------- disc conflicts: reasons and choices
def test_mock_no_title_match_lists_same_number_tracks_with_similarity(fake):
    aid, _ = _album(fake, AMBIGUOUS)
    p, _ap = _problem(fake, aid, P.DISC_AMBIGUOUS)
    assert [c.path for c in p.disc_conflicts] == p.paths                       # same files, file order
    assert set(_reasons(p).values()) == {"no_title_match"}
    c = next(c for c in p.disc_conflicts if c.path.name == "01 d.flac")
    assert (c.number, c.title) == (1, "d") and c.title_matches == []
    assert [(x.disc, x.position, x.number, x.title) for x in c.same_number] == [(1, "1-1", 1, "One"), (2, "2-1", 1, "Four")]
    assert all(0.0 <= x.title_similarity < 0.8 for x in c.same_number)


def test_mock_several_title_matches_and_same_track_and_held_with_group(fake):
    tracks = [(1, 1, "Intro", 60), (1, 2, "Two", 60), (2, 1, "Intro", 60), (2, 2, "Five", 60)]
    aid, _ = _album(fake, [("01 Intro", None, 60), ("02 Two", None, 60), ("01 intro", None, 60),
                           ("02 Five", None, 60)], tracks, title="Twice Intro")
    p, _ = _problem(fake, aid, P.DISC_AMBIGUOUS)
    assert _reasons(p) == {"01 Intro.flac": "several_title_matches", "01 intro.flac": "several_title_matches"}
    c = p.disc_conflicts[0]
    assert [x.position for x in c.title_matches] == ["1-1", "2-1"] and c.title_matches[0].title_similarity == 1.0
    tracks = [(1, 1, "One", 60), (2, 1, "Four", 60), (3, 1, "Seven", 60)]
    aid, _ = _album(fake, [("01 One", None, 60), ("01 Four", None, 60), ("01 zzz", None, 60)], tracks, title="Three")
    p, _ = _problem(fake, aid, P.DISC_AMBIGUOUS)
    assert _reasons(p) == {"01 One.flac": "held_with_group", "01 Four.flac": "held_with_group",
                           "01 zzz.flac": "no_title_match"}
    held = next(c for c in p.disc_conflicts if c.path.name == "01 Four.flac")
    assert [x.position for x in held.title_matches] == ["2-1"]
    tracks = [(1, 1, "One", 60), (2, 1, "Four", 60)]
    aid, _ = _album(fake, [("01 One", None, 60), ("01 One.", None, 60)], tracks, title="Same")
    p, _ = _problem(fake, aid, P.DISC_AMBIGUOUS)
    assert set(_reasons(p).values()) == {"same_track"}


def test_mock_numbering_when_the_folder_does_not_restart_like_the_release(fake):
    files = LAYOUTS["titles"] + [("02 Extra", None, 99)]
    aid, _ = _album(fake, files, title="Extra Track")
    p, _ = _problem(fake, aid, P.DISC_AMBIGUOUS)
    r = _reasons(p)
    assert set(r.values()) == {"numbering"} and "03 Three.flac" not in r and "02 Extra.flac" in r


def test_mock_sources_disagree_folder_tag_and_cue(fake):
    files = [("CD1/01 One", 1, 200), ("CD1/02 Two", 1, 210), ("CD1/03 Three", 1, 220), ("CD2/01 Four", 1, 230),
             ("CD2/02 Five", 2, 240)]
    aid, _ = _album(fake, files, title="Mason Jar", sidecars=[("CD1/Disc Band - CD1.cue", 1, ["CD2/01 Four"])])
    p, ap = _problem(fake, aid, P.DISC_AMBIGUOUS)
    (c,) = p.disc_conflicts
    assert c.path.name == "01 Four.flac" and c.reason == "sources_disagree" and c.title_matches == []
    assert (c.folder_disc, c.tag_disc, c.sidecar_disc) == (2, 1, 1) and c.sidecar.name == "Disc Band - CD1.cue"
    assert [x.position for x in c.same_number] == ["1-1", "2-1"]
    assert "disc folder, disc tag and cue/m3u of 1 file(s) don't agree: CD2/01 Four.flac" in p.message
    assert P.held(ap) and not P.overridable(ap)
    # a CD2 folder of files tagged disc 1 used to pair by the tags; now held (behaviour change 1)
    files2 = [("CD1/01 One", 1, 200), ("CD1/02 Two", 1, 210), ("CD1/03 Three", 1, 220), ("CD2/01 Four", 1, 230),
              ("CD2/02 Five", 1, 240)]
    aid2, _ = _album(fake, files2, title="All Tagged One")
    p2, _ = _problem(fake, aid2, P.DISC_AMBIGUOUS)
    assert set(_reasons(p2)) == {"01 Four.flac", "02 Five.flac"}
    assert {(c.folder_disc, c.tag_disc, c.sidecar_disc, c.sidecar) for c in p2.disc_conflicts} == {(2, 1, None, None)}


def test_disc_conflict_text_in_plain_words():
    c = FS.DiscConflict(Path("/m/A/1. Billy The Bum.flac"), 1, "Billy The Bum", "no_title_match",
                        [FS.DiscChoice(1, "1-1", 1, "Flashback Blues", 0.286), FS.DiscChoice(2, "2-1", 1, "Forbidden Jimmy", 0.214)],
                        [FS.DiscChoice(1, "1-2", 2, "Billy The Bum", 1.0)])
    s = FS.DiscConflict(Path("/m/A/CD2/13 Mason Jar.flac"), 13, "Mason Jar", "sources_disagree",
                        [FS.DiscChoice(1, "1-13", 13, "Mason Jar", 1.0), FS.DiscChoice(2, "2-13", 13, "Indigo Night", 0.19)],
                        [], 2, 1, 1, Path("/m/A/CD1/John Prine - Anthology CD1.cue"))
    pr = FS.Problem(FS.ProblemKind.DISC_AMBIGUOUS, "m", [c.path, s.path], disc_conflicts=[c, s])
    assert P.disc_conflict_lines(pr, Path("/m/A")) == [
        "Files (2):",
        "  1. Billy The Bum.flac — its title doesn't match any of the release's tracks with this number",
        "      Track 1 on each disc: 1-1 “Flashback Blues” (29% title match); 2-1 “Forbidden Jimmy” (21% title match)",
        "      Its title matches: 1-2 “Billy The Bum” (100% title match)",
        "  CD2/13 Mason Jar.flac — its disc folder, disc tag and cue/m3u say different discs",
        "      folder says disc 2; disc tag says disc 1; John Prine - Anthology CD1.cue says disc 1",
        "      Track 13 on each disc: 1-13 “Mason Jar” (100% title match); 2-13 “Indigo Night” (19% title match)"]
    for r in ("numbering", "several_title_matches", "same_track", "held_with_group"):
        assert P.disc_reason(NS(reason=r)) == P.DISC_REASONS[r] and P.DISC_REASONS[r][0].islower()
    assert P.disc_reason(NS(reason="new_reason")) == "new reason"
    assert P.sources_text(NS(folder_disc=None, tag_disc=2, sidecar=None, sidecar_disc=None)) == \
        "folder says nothing; disc tag says disc 2; no cue/m3u lists it"
    assert "disagree" in P.sources_text(NS(folder_disc=1, tag_disc=2, sidecar=Path("/a.cue"), sidecar_disc=None))
    assert "Can't work out the disc layout: its disc folder" in disc_tooltip(s) and "Track 13 on each disc" in disc_tooltip(s)
    # older service: no disc_conflicts -> just the files
    old = NS(kind="disc_ambiguous", message="m", paths=[Path("/m/A/01 a.flac")])
    assert P.disc_conflict_lines(old, Path("/m/A")) == ["Files (1):", "  01 a.flac"]
    assert "nothing says which disc" in disc_tooltip(None)


def test_dialog_and_review_show_reason_and_choices_per_file(fake, qtbot):
    files = [("CD1/01 One", 1, 200), ("CD1/02 Two", 1, 210), ("CD1/03 Three", 1, 220), ("CD2/01 Four", 1, 230),
             ("CD2/02 Five", 2, 240)]
    aid, _ = _album(fake, files, title="Mason Jar", sidecars=[("CD1/Disc Band - CD1.cue", 1, ["CD2/01 Four"])])
    aid2, _ = _album(fake, AMBIGUOUS)
    plan = built(fake, [aid, aid2])
    d = ApplyDialog(plan); qtbot.addWidget(d)
    assert d.anyway_btn is None and d.naming_btn is None
    text = {d.problem_list.item(i).data(Qt.UserRole): d.problem_list.item(i).text() for i in range(d.problem_list.count())}
    assert "CD2/01 Four.flac — its disc folder, disc tag and cue/m3u say different discs" in text[aid]
    assert "folder says disc 2; disc tag says disc 1; Disc Band - CD1.cue says disc 1" in text[aid]
    assert "01 d.flac — its title doesn't match any of the release's tracks with this number" in text[aid2]
    assert "Track 1 on each disc: 1-1 “One” (" in text[aid2] and "2-1 “Four” (" in text[aid2]
    w = ReviewWindow(fake); qtbot.addWidget(w)
    w.filter_box.setCurrentText("All")
    row = next(i for i in range(w.model.rowCount()) if "Mason Jar" in w.model.index(i).data())
    w.list.setCurrentIndex(w.model.index(row))
    marks = {w.table.item(r, 7).text(): w.table.item(r, 7).toolTip() for r in range(w.table.rowCount())
             if w.table.item(r, 7) is not None}
    assert "discs disagree" in marks and "folder says disc 2" in marks["discs disagree"]
    assert "CD2/01 Four.flac — its disc folder" in w.preview.text()


# ---------------------------------------------------------------- layout: structured fields, no message parsing
def test_mock_layout_problems_carry_structured_fields(fake, qtbot, monkeypatch):
    core = fake._c.fake
    ids = core.add_loose_layouts()
    for k in ("stray", "mixed"):
        rid = fake.match.candidates(ids[k])[0].release_id
        core._fetch_queue = [(ids[k], rid)]; core._fetch_one(pace=False)
        fake.match.choose(ids[k], rid)
    by = {ap.album_id: ap for ap in built(fake, [ids["stray"], ids["mixed"]]).albums}
    st, mx = P.layout_problem(by[ids["stray"]].problems), P.layout_problem(by[ids["mixed"]].problems)
    stray_dir, mixed_dir = (fake.library.album(ids[k]).folder for k in ("stray", "mixed"))
    assert st.layout_reason == "album_folders" and [d.parent for d in st.blocking_subdirs] == [stray_dir] * 2
    assert st.loose_files == [stray_dir / "demo take.mp3"] and st.paths == [stray_dir]
    assert mx.layout_reason == "disc_folders" and [d.name for d in mx.blocking_subdirs] == ["CD1", "CD2"]
    assert mx.loose_files == [mixed_dir / "01 Split Myself in Two.mp3"]
    # the UI uses the fields, whatever the message says
    monkeypatch.setitem(core._layout_problems, ids["stray"], ("reworded message", "album_folders",
                                                             st.blocking_subdirs))
    w = ReviewWindow(fake); qtbot.addWidget(w)
    plan = built(fake, [ids["stray"]])
    d = ApplyDialog(plan, album_name=w.album_name, album_row=w.album_row); qtbot.addWidget(d)
    t = d.problem_list.item(0).text()
    assert "reworded message" in t and "Next to subfolders with audio (2):" in t and "[1983]  Buzz or Howl" in t


# ---------------------------------------------------------------- disc_folder_mismatch
CD_TEMPLATE = "%albumartist%/%album%/CD%disctotal%/$num(%tracknumber%,2) %title%"


def _cd_template(fake, text=CD_TEMPLATE):
    t = fake.templates.save("CD folder per total", text)
    fake.settings.set(active_template=t.id)
    return t


def test_mock_disc_folder_mismatch_holds_and_is_never_overridden(fake, qtbot):
    aid, rid = _album(fake, LAYOUTS["folders"], title="Folder Discs")
    assert _problem(fake, aid, P.DISC_FOLDER_MISMATCH)[0] is None                  # archivist: no CD folders
    _cd_template(fake)
    p, ap = _problem(fake, aid, P.DISC_FOLDER_MISMATCH)
    assert [m.path.name for m in p.folder_mismatches] == ["01 One.flac", "02 Two.flac", "03 Three.flac"]
    assert p.paths == [m.path for m in p.folder_mismatches]
    m = p.folder_mismatches[0]
    assert (m.folder.name, m.folder_disc, m.tag_disc) == ("CD2", 2, 1) and m.destination.parent == m.folder
    assert "the destination puts tracks into another disc's folder (3 file(s)): 01 One.flac -> CD2/ (disc tag 1)" in p.message
    assert P.held(ap) and not P.overridable(ap)
    (ap2,) = fake.plan.build([aid], allow_track_mismatch={aid}).albums
    assert P.has_folder_mismatch(ap2.problems)
    d = fake.match.destination(aid, rid, fake.active_template())
    assert P.has_folder_mismatch(d.problems)
    assert P.folder_mismatch_lines(p, ap.folder_before) == [
        "Files (3):", "  CD1/01 One.flac → CD2/ (disc tag 1)", "  CD1/02 Two.flac → CD2/ (disc tag 1)",
        "  CD1/03 Three.flac → CD2/ (disc tag 1)", P.NAMING_HINT]
    # single-disc albums: CD1 matches disc 1, nothing to flag; a fixed CD2 folder would be flagged
    one = fake.albums({"MATCHED"}, limit=1).items[0]
    c = fake.match.candidates(one.id)[0]
    fake._c.fake._fetch_queue = [(one.id, c.release_id)]; fake._c.fake._fetch_one(pace=False)
    fake.match.choose(one.id, c.release_id)
    assert _problem(fake, one.id, P.DISC_FOLDER_MISMATCH)[0] is None
    _cd_template(fake, "%albumartist%/%album%/CD2/%title%")
    assert _problem(fake, one.id, P.DISC_FOLDER_MISMATCH)[0] is not None
    # apply holds it
    _cd_template(fake)
    plan = fake.plan.build([aid])
    with qtbot.waitSignal(fake.events.job_finished, check_params_cb=not_fetch, timeout=3000) as sig:
        fake.plan.apply(plan.id)
    assert sig.args[0].state.name == "FAILED"
    a = fake.library.album(aid)
    assert a.status.name == "HELD" and {p.kind.value for p in a.held_for} == {"disc_folder_mismatch"}


def test_folder_mismatch_text_and_fallback():
    m = FS.DiscFolderMismatch(Path("/M/A/13 Mason Jar.flac"), Path("/M/B/CD2/13 Mason Jar.flac"), Path("/M/B/CD2"), 2, 1)
    assert P.folder_mismatch_text(m) == "13 Mason Jar.flac → CD2/ (disc tag 1)"
    assert P.folder_mismatch_text(m, Path("/M/A")) == "13 Mason Jar.flac → CD2/ (disc tag 1)"
    old = NS(kind="disc_folder_mismatch", message="m", paths=[Path("/M/A/13 Mason Jar.flac")])
    assert P.folder_mismatch_lines(old, Path("/M/A")) == ["Files (1):", "  13 Mason Jar.flac", P.NAMING_HINT]


def test_apply_dialog_offers_naming_settings_and_rebuilds(fake, qtbot, monkeypatch):
    aid, _ = _album(fake, LAYOUTS["folders"], title="Folder Discs")
    _cd_template(fake)
    w = ReviewWindow(fake); qtbot.addWidget(w)
    dialogs, naming = [], []

    def fake_exec(self):
        dialogs.append(self)
        if len(dialogs) == 1:
            assert self.naming_btn is not None and self.anyway_btn is None and not self.ok_btn.isEnabled()
            t = next(self.problem_list.item(i).text() for i in range(self.problem_list.count())
                     if self.problem_list.item(i).data(Qt.UserRole + 1) == "disc_folder_mismatch")
            assert "Tracks would go into another disc's folder" in t
            assert "CD1/01 One.flac → CD2/ (disc tag 1)" in t and P.NAMING_HINT in t
            self.naming_btn.click()
            return self.result()
        assert self.naming_btn is None and not self.held_aps        # template fixed: the album is clean
        return QDialog.Rejected
    monkeypatch.setattr(ApplyDialog, "exec", fake_exec)

    def naming_exec(self):
        naming.append(self)
        fake.settings.set(active_template="archivist")          # the user picks a layout without CD folders
        return QDialog.Accepted
    monkeypatch.setattr(NamingDialog, "exec", naming_exec)
    with qtbot.waitSignal(w.plan_flow_done, timeout=5000):
        w.open_apply()
    qtbot.waitUntil(lambda: len(dialogs) == 2, timeout=5000)
    assert len(naming) == 1 and dialogs[0].result() == ApplyDialog.OPEN_NAMING


def test_review_screen_shows_mismatch_and_naming_button(fake, qtbot, monkeypatch):
    aid, _ = _album(fake, LAYOUTS["folders"], title="Folder Discs")
    _cd_template(fake)
    opened = []
    monkeypatch.setattr(NamingDialog, "exec", lambda self: opened.append(self) or QDialog.Rejected)
    w = ReviewWindow(fake); qtbot.addWidget(w)
    w.filter_box.setCurrentText("All")
    row = next(i for i in range(w.model.rowCount()) if "Folder Discs" in w.model.index(i).data())
    w.list.setCurrentIndex(w.model.index(row))
    assert not w.naming_btn.isHidden() and w.naming_btn.text() == "Change naming template…"
    assert "Tracks would go into another disc's folder" in w.preview.text()
    assert "CD1/01 One.flac → CD2/ (disc tag 1)" in w.preview.text()
    w.naming_btn.click()
    assert len(opened) == 1
    fake.settings.set(active_template="archivist"); w.render()
    assert w.naming_btn.isHidden()
