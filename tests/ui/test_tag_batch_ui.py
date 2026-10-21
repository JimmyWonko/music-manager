"""Tag batch (contract "Tag batch (2026-10-21)") on the fake backend: ID3v2.3 / Latin-1 / verify hash settings,
Latin-1 losses and junk-to-trash warnings in the plan preview. Nothing is written before Apply."""
import pytest
from test_ui_flows import built

from mm_ui import tagsettings as TS
from mm_ui.review import JUNK_TEXT, ApplyDialog, latin1_written, loss_html
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
    assert not l1.isEnabled()
    mp3.setChecked(True); assert l1.isEnabled()
    mp3.setChecked(False); assert not l1.isEnabled()
    assert d.hash_combo.currentData() == "sha256"
    assert "WAV and AIFF" in TS.V23_NOTE
    assert d.changes() == {}


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


def test_xxhash_disabled_with_hint_when_missing(fake, qtbot, monkeypatch):
    monkeypatch.setattr(TS, "xxhash_available", lambda: False)
    d = TagSettingsDialog(fake); qtbot.addWidget(d)
    assert not d.hash_combo.model().item(1).isEnabled()
    assert not d.hash_hint.isHidden() and "pip install xxhash" in d.hash_hint.text()


def test_xxhash_selectable_when_installed(fake, qtbot, monkeypatch):
    monkeypatch.setattr(TS, "xxhash_available", lambda: True)
    d = TagSettingsDialog(fake); qtbot.addWidget(d)
    assert d.hash_combo.model().item(1).isEnabled() and d.hash_hint.isHidden()
    d.hash_combo.setCurrentIndex(1)
    assert d.changes() == {"verify_hash": "xxhash"}
    errors = []
    d.warn = lambda t, x: errors.append(x)
    d.save()
    import importlib.util
    if importlib.util.find_spec("xxhash") is None:     # fake mirrors the real ValidationError
        assert errors and "xxhash" in errors[0]
    else:
        assert fake.settings.get().verify_hash == "xxhash"


def test_latin1_helpers():
    assert latin1_written("Café ★", "★") == "Café ?"
    h = loss_html("a<★", "★")
    assert "&lt;" in h and "<b" in h and "★</b>" in h


def test_no_losses_without_latin1_but_junk_listed(fake, qtbot, album):
    d = ApplyDialog(built(fake, [album])); qtbot.addWidget(d)
    assert d.loss_label.isHidden() and not d.losses
    assert not d.junk_label.isHidden() and JUNK_TEXT in d.junk_label.text() and "Trash" in d.junk_label.text()
    junk = [r for r in _rows(d) if r.data(0, 256) == "junk"]
    assert {r.text(1) for r in junk} == {".DS_Store", "Thumbs.db", "._01 Track.wav"}
    assert all(r.text(0).endswith(JUNK_TEXT) for r in junk)


def test_latin1_losses_shown_with_highlight(fake, qtbot, album):
    fake.settings.set(id3_latin1=True)
    plan = built(fake, [album])
    d = ApplyDialog(plan); qtbot.addWidget(d)
    assert len(d.losses) == 2 and not d.loss_label.isHidden()
    t = d.loss_label.text()
    assert "01 Track.wav" in t and "title" in t and "Latin-1" in t and "background:#fdd" in t
    rows = {r.text(0): (r.text(1), r.text(2)) for r in _rows(d) if r.data(0, 256) == "encoding_loss"}
    assert rows["⚠ Latin-1 loses “→★”: title"] == ("Ångström → Café ★", "Ångström ? Café ?")
    assert any("lose characters as Latin-1" in w for w in plan.warnings)
