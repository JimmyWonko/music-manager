"""Tag settings (contract "Picard tag names (2026-10-21)"): three opt-in switches, all off by default.

- migrate_tag_keys: remove keys Picard doesn't write (today a Vorbis YEAR); shows in the plan first. Alias
  renames (TOTALTRACKS -> TRACKTOTAL) happen regardless.
- write_styles: write Discogs styles as STYLE (off: genres only, an existing STYLE is left alone).
- styles_in_genre: append the styles to GENRE. The contract keeps it independent of write_styles.
Tag batch (2026-10-21):
- id3v23_mp3: MP3s as ID3v2.3 too. WAV/AIFF are always v2.3.
- id3_latin1: v2.3 text as Latin-1 instead of UTF-16; enabled only when v2.3 applies here (MP3 v2.3 is on, or
  it is already on so it can be turned off). The plan lists every loss before Apply.
- verify_hash: SHA-256 (default) or xxHash; xxHash is disabled with a hint when the xxhash package is missing.
Saving only changes settings: plan.build reads them, nothing is written to any file before Apply."""
from __future__ import annotations

import importlib.util

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMessageBox,
    QVBoxLayout,
)

from .backend import as_backend

LABELS = {
    "migrate_tag_keys": "Remove tag keys Picard doesn't write (e.g. Vorbis YEAR)",
    "write_styles": "Write Discogs styles",
    "styles_in_genre": "Add styles to Genre",
    "id3v23_mp3": "Write ID3v2.3 for MP3 (older players)",
    "id3_latin1": "Use Latin-1 instead of UTF-16 for v2.3",
}
HASHES = [("sha256", "SHA-256"), ("xxhash", "xxHash")]
XXHASH_HINT = "xxHash needs the optional xxhash package (pip install xxhash)"
V23_NOTE = "WAV and AIFF files are always written as ID3v2.3."


def xxhash_available() -> bool:
    return importlib.util.find_spec("xxhash") is not None
TIPS = {
    "migrate_tag_keys": "Removes keys earlier versions wrote that Picard doesn't (today a Vorbis YEAR). Each "
                        "removal shows in the plan before Apply; undo restores them. Alias keys such as "
                        "TOTALTRACKS are renamed to the Picard name either way.",
    "write_styles": "Discogs styles (Southern Rock, Classic Rock…) as a STYLE tag. Off: GENRE gets the Discogs "
                    "genres only and an existing STYLE is left alone.",
    "styles_in_genre": "Append the styles to GENRE after the genres, for players that only read Genre. Works "
                       "whether or not “Write Discogs styles” is on.",
    "id3v23_mp3": "Write MP3 tags as ID3v2.3 instead of v2.4, for older players and car stereos. " + V23_NOTE,
    "id3_latin1": "ID3v2.3 has no UTF-8; text is UTF-16 by default. Latin-1 is read by more old players but "
                  "can't hold every character: those become “?”, and the plan lists each one before Apply.",
}


class TagSettingsDialog(QDialog):
    def __init__(self, svc=None, parent=None):
        super().__init__(parent)
        self.svc = as_backend(svc)
        self.setWindowTitle("Tags")
        st = self.svc.settings.get()
        lay = QVBoxLayout(self)
        self.boxes: dict[str, QCheckBox] = {}
        for key, label in LABELS.items():
            b = self.boxes[key] = QCheckBox(label)
            b.setToolTip(TIPS[key])
            b.setChecked(bool(getattr(st, key, False)))
            b.setEnabled(hasattr(st, key))           # an older backend without the setting
            lay.addWidget(b)
        v23 = QLabel(V23_NOTE, wordWrap=True); v23.setStyleSheet("color:#666")
        lay.addWidget(v23)
        self.latin1_always = bool(getattr(st, "id3_latin1", False))
        self.boxes["id3v23_mp3"].toggled.connect(self._sync_latin1)
        self._sync_latin1()
        self.hash_combo = QComboBox()
        for key, label in HASHES:
            self.hash_combo.addItem(label, key)
        self.xxhash_ok = xxhash_available()
        if not self.xxhash_ok:
            item = self.hash_combo.model().item(1)
            item.setEnabled(False); item.setToolTip(XXHASH_HINT)
            self.hash_combo.setItemText(1, "xxHash (not installed)")
        self.hash_combo.setCurrentIndex(max(0, self.hash_combo.findData(getattr(st, "verify_hash", "sha256"))))
        self.hash_combo.setEnabled(hasattr(st, "verify_hash"))
        self.hash_combo.setToolTip("Hash used to check a copy to another drive. " + ("" if self.xxhash_ok else XXHASH_HINT))
        form = QFormLayout(); form.addRow("Verify copies with", self.hash_combo)
        lay.addLayout(form)
        self.hash_hint = QLabel(XXHASH_HINT, wordWrap=True); self.hash_hint.setStyleSheet("color:#666")
        self.hash_hint.setVisible(not self.xxhash_ok)
        lay.addWidget(self.hash_hint)
        note = QLabel("Nothing is written to your files until you Apply a plan.", wordWrap=True)
        note.setStyleSheet("color:#666")
        lay.addWidget(note)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.save); bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        # tests replace this (no modal dialogs in tests)
        self.warn = lambda title, text: QMessageBox.warning(self, title, text)

    def _sync_latin1(self):
        """Latin-1 only matters where v2.3 is written: MP3 v2.3 on (WAV/AIFF always are, but the UI can't tell if
        the library holds any, so it stays reachable once on)."""
        st = self.svc.settings.get()
        b = self.boxes["id3_latin1"]
        b.setEnabled(hasattr(st, "id3_latin1") and (self.boxes["id3v23_mp3"].isChecked() or self.latin1_always))

    def changes(self) -> dict:
        st = self.svc.settings.get()
        ch = {k: b.isChecked() for k, b in self.boxes.items()
              if b.isEnabled() and b.isChecked() != bool(getattr(st, k, False))}
        h = self.hash_combo.currentData()
        if self.hash_combo.isEnabled() and h != getattr(st, "verify_hash", "sha256"):
            ch["verify_hash"] = h
        return ch

    def save(self):
        ch = self.changes()
        if ch:
            try:
                self.settings = self.svc.settings.set(**ch)
            except self.svc.types.MMError as e:
                self.warn("Can't save tag settings", str(e))
                return
        self.accept()
