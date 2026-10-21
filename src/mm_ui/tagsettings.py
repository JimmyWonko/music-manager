"""Tag settings (contract "Picard tag names (2026-10-21)"): three opt-in switches, all off by default.

- migrate_tag_keys: also remove keys earlier versions wrote (a Vorbis YEAR); shows in the plan first.
- write_styles: write Discogs styles as STYLE (off: genres only, an existing STYLE is left alone).
- styles_in_genre: append the styles to GENRE. The contract keeps it independent of write_styles.
Saving only changes settings: plan.build reads them, nothing is written to any file before Apply."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMessageBox,
    QVBoxLayout,
)

from .backend import as_backend

LABELS = {
    "migrate_tag_keys": "Rename old tag keys to Picard names (shown in preview first)",
    "write_styles": "Write Discogs styles",
    "styles_in_genre": "Add styles to Genre",
}
TIPS = {
    "migrate_tag_keys": "Also removes tag keys earlier versions wrote, such as a Vorbis YEAR. Every key change "
                        "shows in the plan before Apply; undo restores the old keys.",
    "write_styles": "Discogs styles (Southern Rock, Classic Rock…) as a STYLE tag. Off: GENRE gets the Discogs "
                    "genres only and an existing STYLE is left alone.",
    "styles_in_genre": "Append the styles to GENRE after the genres, for players that only read Genre. Works "
                       "whether or not “Write Discogs styles” is on.",
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
        note = QLabel("Nothing is written to your files until you Apply a plan.", wordWrap=True)
        note.setStyleSheet("color:#666")
        lay.addWidget(note)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.save); bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        # tests replace this (no modal dialogs in tests)
        self.warn = lambda title, text: QMessageBox.warning(self, title, text)

    def changes(self) -> dict[str, bool]:
        st = self.svc.settings.get()
        return {k: b.isChecked() for k, b in self.boxes.items()
                if b.isEnabled() and b.isChecked() != bool(getattr(st, k, False))}

    def save(self):
        ch = self.changes()
        if ch:
            try:
                self.settings = self.svc.settings.set(**ch)
            except self.svc.types.MMError as e:
                self.warn("Can't save tag settings", str(e))
                return
        self.accept()
