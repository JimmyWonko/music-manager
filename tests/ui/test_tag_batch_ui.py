"""Tag batch (contract "Tag batch (2026-10-21)") on the fake backend: ID3v2.3 / Latin-1 / verify hash settings,
Latin-1 severities (transliterated amber / dropped red + ENCODING_DROPPED hold), junk to trash and junk moving
with the album in the plan preview. Nothing is written before Apply."""
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from test_ui_flows import built

from mm_ui import problems as P
from mm_ui import tagsettings as TS
from mm_ui.review import (
    DROPPED_TEXT,
    JUNK_TEXT,
    LOSS,
    MOVING_TEXT,
    TRANSLIT,
    ApplyDialog,
    ReviewWindow,
    loss_html,
)
from mm_ui.tagsettings import TagSettingsDialog


@pytest.fixture
def album(fake):
    aid = next(a.id for a in fake.albums(None, limit=50).items if a.status.name == "MATCHED"
               and not built(fake, [a.id]).albums[0].problems)
    fake._c.add_tag_batch_demo(aid)
    return aid


def _rows(dlg):
    def walk(it):
        for i in range(it.childCount()):
            yield it.child(i); yield from walk(it.child(i))
    return list(walk(dlg.tree.invisibleRootItem()))


def test_settings_defaults_labels_and_latin1_follows_v23(fake, qtbot):
    d = TagSettingsDialog(fake); qtbot.addWidget(d)
    mp3, l1 = d.boxes["id3v23_mp3"], d.boxes["id3_latin1"]
    assert mp3.text() == "Write ID3v2.3 for MP3 (older players)" and not mp3.isChecked()
    assert l1.text() == "Use Latin-1 instead of UTF-16 for v2.3" and not l1.isChecked()
    assert not l1.isEnabled() and d.v23_label.isHidden()
    mp3.setChecked(True); assert l1.isEnabled()
    mp3.setChecked(False); assert not l1.isEnabled()
    assert d.hash_combo.currentData() == "sha256" and "WAV and AIFF" in TS.V23_NOTE
    assert d.changes() == {}


def test_latin1_enabled_by_v23_files_in_last_plan(fake, qtbot):
    d = TagSettingsDialog(fake, v23_files=3); qtbot.addWidget(d)
    assert d.boxes["id3_latin1"].isEnabled() and not d.boxes["id3v23_mp3"].isChecked()
    assert not d.v23_label.isHidden() and d.v23_label.text().startswith("3 track(s)")


def test_window_passes_id3v23_files_of_last_plan(fake, qtbot, album, monkeypatch):
    w = ReviewWindow(fake); qtbot.addWidget(w)
    w.last_apply_dialog = ApplyDialog(built(fake, [album])); qtbot.addWidget(w.last_apply_dialog)
    seen = {}
    monkeypatch.setattr(TagSettingsDialog, "exec", lambda self: seen.setdefault("n", self.v23_files) and 0)
    w.show_tag_settings()
    assert seen["n"] == 2


def test_settings_save_round_trip(fake, qtbot):
    d = TagSettingsDialog(fake); qtbot.addWidget(d)
    d.boxes["id3v23_mp3"].setChecked(True); d.boxes["id3_latin1"].setChecked(True)
    assert d.changes() == {"id3v23_mp3": True, "id3_latin1": True}
    d.save()
    st = fake.settings.get()
    assert st.id3v23_mp3 and st.id3_latin1 and st.verify_hash == "sha256"
    d2 = TagSettingsDialog(fake); qtbot.addWidget(d2)      # Latin-1 already on stays reachable
    d2.boxes["id3v23_mp3"].setChecked(False)
    assert d2.boxes["id3_latin1"].isEnabled()


def test_xxhash_disabled_with_hint_from_service(fake, qtbot):
    assert fake.settings.verify_hashes_available() == ["sha256"]
    d = TagSettingsDialog(fake); qtbot.addWidget(d)
    assert not d.hash_combo.model().item(1).isEnabled()
    assert not d.hash_hint.isHidden() and "pip install xxhash" in d.hash_hint.text()


def test_xxhash_selectable_when_service_offers_it(fake, qtbot, monkeypatch):
    monkeypatch.setattr(fake._c.settings, "verify_hashes_available", lambda: ["sha256", "xxhash"])
    d = TagSettingsDialog(fake); qtbot.addWidget(d)
    assert d.hash_combo.model().item(1).isEnabled() and d.hash_hint.isHidden()
    d.hash_combo.setCurrentIndex(1)
    assert d.changes() == {"verify_hash": "xxhash"}


def test_loss_html_escapes_and_colours():
    h = loss_html("a<★", "★", TRANSLIT)
    assert "&lt;" in h and f"color:{TRANSLIT}" in h and "★</b>" in h


def test_junk_trash_and_moving_without_latin1(fake, qtbot, album):
    d = ApplyDialog(built(fake, [album])); qtbot.addWidget(d)
    assert d.loss_label.isHidden() and d.drop_banner.isHidden() and not d.losses
    assert not d.junk_label.isHidden() and JUNK_TEXT in d.junk_label.text()
    assert not d.moving_label.isHidden() and MOVING_TEXT in d.moving_label.text()
    rows = _rows(d)
    assert {r.text(1) for r in rows if r.data(0, Qt.UserRole) == "junk"} == {".DS_Store", "Thumbs.db"}
    assert {r.text(1) for r in rows if r.data(0, Qt.UserRole) == "junk_moving"} == {".DS_Store", "._01 Track.wav"}


def test_dropped_holds_album_with_banner_totals_and_allow_drop(fake, qtbot, album):
    fake.settings.set(id3_latin1=True)
    plan = built(fake, [album])
    (ap,) = plan.albums
    assert (ap.encoding_transliterated, ap.encoding_dropped, ap.id3v23_files) == (1, 2, 2)
    assert [P.kind_of(p) for p in ap.problems] == [P.ENCODING_DROPPED]
    d = ApplyDialog(plan); qtbot.addWidget(d)
    assert not d.drop_banner.isHidden() and "2 tag value(s)" in d.drop_banner.text()
    t = d.loss_label.text()
    assert "1 transliterated" in t and "2 dropped" in t and 'Lódz -- &quot;Live&quot;' in t and DROPPED_TEXT in t
    assert not d.ok_btn.isEnabled()                      # held: nothing to apply
    assert d.drop_btn is not None and not d.drop_btn.isEnabled()
    d.problem_list.setCurrentRow(0)
    assert d.drop_btn.isEnabled()
    d.confirm_drop = lambda ap: True
    d.allow_drop()
    assert d.result() == ApplyDialog.ALLOW_DROP and d.drop_album == album
    # the caller re-builds with allow_encoding_drop: the album applies, dropped fields unwritten
    plan2 = built(fake, [album], allow_encoding_drop={album})
    d2 = ApplyDialog(plan2); qtbot.addWidget(d2)
    assert d2.clean_aps and d2.ok_btn.isEnabled()
    rows = {r.text(0): r for r in _rows(d2) if r.data(0, Qt.UserRole) == "encoding_loss"}
    tr, dr = rows["⚠ Latin-1 transliterated: title"], rows["⚠ Latin-1 dropped: artist"]
    assert tr.text(2) == 'Lódz -- "Live"' and tr.foreground(1).color().name() == QColor(TRANSLIT).name()
    assert dr.text(2) == DROPPED_TEXT and dr.foreground(1).color().name() == QColor(LOSS).name()
    assert "->" in tr.toolTip(1) and DROPPED_TEXT in dr.toolTip(1)


def test_window_rebuilds_with_allow_encoding_drop(fake, qtbot, album, monkeypatch):
    import mm_ui.worker as W
    fake.settings.set(id3_latin1=True)
    w = ReviewWindow(fake); qtbot.addWidget(w)
    calls, results = [], iter([ApplyDialog.ALLOW_DROP, 0])
    real_build = fake._c.plan.build
    monkeypatch.setattr(fake._c.plan, "build", lambda *a, **kw: (calls.append(kw), real_build(*a, **kw))[1])
    monkeypatch.setattr(W, "run_async", lambda fn, ok, err: ok(fn()))
    plans = []

    def fake_exec(dlg):
        plans.append(dlg.plan)
        dlg.drop_album = album
        return next(results)
    monkeypatch.setattr(ApplyDialog, "exec", fake_exec)
    w._apply_ids, w._allow, w._allow_drop, w._fetch_rounds = [album], set(), set(), 0
    w._build_plan()
    assert calls[0].get("allow_encoding_drop") is None                     # first build: nothing allowed
    assert calls[-1]["allow_encoding_drop"] == frozenset({album})
    assert plans[0].albums[0].problems and not plans[-1].albums[0].problems


def test_held_album_keeps_losses_totals_and_real_warning_wording(fake, qtbot, album):
    fake.settings.set(id3_latin1=True)
    plan = built(fake, [album])
    (ap,) = plan.albums
    assert P.held(ap) and len(ap.encoding_losses) == 3                 # held: losses still listed
    assert (ap.encoding_transliterated, ap.encoding_dropped, ap.id3v23_files) == (1, 2, 2)
    (w,) = [w for w in plan.warnings if "Latin-1" in w]
    assert w == (f"{ap.folder_before.name}: Latin-1 (Settings.id3_latin1): 1 tag value(s) transliterated, 2 dropped "
                 "and not written (existing value kept) (album held: allow_encoding_drop to apply): "
                 "see AlbumPlan.encoding_losses")
    assert "(artist, album)" in ap.problems[0].message or "(album, artist)" in ap.problems[0].message
    d = ApplyDialog(plan); qtbot.addWidget(d)
    assert len(d.losses) == 3 and "2 dropped" in d.loss_label.text()
    (w2,) = [w for w in built(fake, [album], allow_encoding_drop={album}).warnings if "Latin-1" in w]
    assert "album held" not in w2 and "2 dropped and not written" in w2
