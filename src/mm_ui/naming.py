"""File-naming settings: layouts + toggles + live preview, raw template behind Advanced.

Contract calls only:
- layouts: templates.list() (built-ins first, then user templates); active one from settings.get()
- preview: templates.preview(text, album_ids) -> TemplatePreview(moves, collisions, warnings)
- Save:    the text matches an existing template -> settings.set(active_template=<id>, sequential_sides=...)
           otherwise templates.save(name, text) first (ValidationError is shown, dialog stays open)
- Delete:  templates.delete(id) for user templates
"Name vinyl and cassette tracks by side" is Settings.sequential_sides (inverted), not template text.
"Keep catalog numbers exactly as on Discogs" swaps %catno% (contract: SST 023 -> SST023) for
%catno_raw% (contract: first catalog number exactly as on Discogs). The toggle is enabled only when
the backend's templates.preview accepts the field (older backends reject it with ValidationError).
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QInputDialog, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QVBoxLayout,
)

from .backend import Backend, as_backend

CATNO_BITS = (" [%catno%]", "%catno% • ", "[%catno%] ")
CUSTOM = "Custom"


def apply_toggles(tpl: str, catno: bool, reissue: bool, raw_catno: bool = False) -> str:
    if not catno:
        for bit in CATNO_BITS:
            tpl = tpl.replace(bit, "")
    if not reissue:
        tpl = tpl.replace(" [%reissueyear%]", "")
    if raw_catno:
        tpl = tpl.replace("%catno%", "%catno_raw%")
    return tpl


class NamingDialog(QDialog):
    def __init__(self, svc: Backend | None = None, parent=None):
        super().__init__(parent)
        self.svc = as_backend(svc)
        self.settings = self.svc.settings.get()
        self.sample_ids = self.svc.sample_album_ids(6)
        self.setWindowTitle("File naming"); self.resize(900, 600)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.preset = QComboBox()
        form.addRow("Layout:", self.preset)
        self.sides = QCheckBox("Name vinyl and cassette tracks by side (A1, A2, B1…)")
        self.catno = QCheckBox("Catalog number in the album folder")
        self.reissue = QCheckBox("Reissue year in the album folder (only for reissues)")
        self.raw_catno = QCheckBox("Keep catalog numbers exactly as on Discogs")
        self.raw_catno.setToolTip("Off: SST 023 becomes SST023 (today's rule). On: the exact Discogs text, "
                                  "with only characters that can't be in a file name replaced.")
        self.raw_catno_supported = self._supports_field("catno_raw")
        if not self.raw_catno_supported:
            self.raw_catno.setEnabled(False)
            self.raw_catno.setToolTip("This backend doesn't support %catno_raw% yet.")
        self.discs = QCheckBox("Disc subfolders for multi-disc CDs")
        self.discs.setEnabled(False)
        self.discs.setToolTip("Needs a conditional disc-folder field; API contract v0 only has "
                              "%discnumber%/%disctotal%, which would add a folder to every album.")
        self.sides.setChecked(not self.settings.sequential_sides)
        for b in (self.catno, self.reissue):
            b.setChecked(True)
        for b in (self.sides, self.catno, self.raw_catno, self.reissue, self.discs):
            form.addRow("", b)
        lay.addLayout(form)

        lay.addWidget(QLabel("<b>Preview</b> (your albums, nothing is renamed here)"))
        self.preview = QPlainTextEdit(readOnly=True)
        self.preview.setStyleSheet("font-family:monospace")
        lay.addWidget(self.preview, 1)
        self.issues = QLabel(wordWrap=True); self.issues.setStyleSheet("color:#a33")
        self.note = QLabel(wordWrap=True); self.note.setStyleSheet("color:#8a6d00")
        lay.addWidget(self.issues); lay.addWidget(self.note)

        self.adv = QGroupBox("Advanced: edit the template"); self.adv.setCheckable(True); self.adv.setChecked(False)
        al = QVBoxLayout(self.adv)
        self.tpl = QLineEdit(); self.tpl.setStyleSheet("font-family:monospace")
        al.addWidget(self.tpl)
        al.addWidget(QLabel("Fields: %albumartist% %album% %title% %artist% %leadyear% %originalyear% "
                            "%reissueyear% %catno% %label% %format% %tracknumber% %position% %discnumber% "
                            "%disctotal% $num(field,width). Empty fields drop with their brackets; an empty "
                            "%label% in a folder becomes “Unknown label”.", wordWrap=True))
        lay.addWidget(self.adv)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.reset = bb.addButton("Reset to default", QDialogButtonBox.ResetRole)
        self.delete = bb.addButton("Delete layout", QDialogButtonBox.DestructiveRole)
        bb.accepted.connect(self.save); bb.rejected.connect(self.reject)
        self.reset.clicked.connect(self.reset_default)
        self.delete.clicked.connect(self.delete_current)
        lay.addWidget(bb)

        self.load_templates(self.settings.active_template)
        for b in (self.sides, self.catno, self.raw_catno, self.reissue):
            b.toggled.connect(self.from_controls)
        self.catno.toggled.connect(lambda on: self.raw_catno.setEnabled(on and self.raw_catno_supported))
        self.preset.currentIndexChanged.connect(self.from_controls)
        self.adv.toggled.connect(self.on_adv)
        self.tpl.textEdited.connect(self.on_custom)
        self.from_controls()

    def _supports_field(self, name: str) -> bool:
        """Contract-only probe: templates.preview validates fields (ValidationError if unknown)."""
        try:
            self.svc.templates.preview(f"%{name}%", [])
            return True
        except Exception:
            return False

    # ------------------------------------------------------------ templates
    def load_templates(self, select_id: str | None):
        self.templates = {t.id: t for t in self.svc.templates.list()}
        self.preset.blockSignals(True)
        self.preset.clear()
        for t in self.templates.values():
            self.preset.addItem(t.name + ("" if t.builtin else "  (yours)"), t.id)
        self.preset.addItem(CUSTOM, None)
        i = self.preset.findData(select_id)
        self.preset.setCurrentIndex(i if i >= 0 else 0)
        self.preset.blockSignals(False)
        self.sync_delete()

    def current_template(self):
        return self.templates.get(self.preset.currentData())

    def sync_delete(self):
        t = self.current_template()
        self.delete.setEnabled(bool(t and not t.builtin))

    def from_controls(self, *_):
        self.sync_delete()
        t = self.current_template()
        if t is None:            # Custom: keep the user's text
            self.update_preview(); return
        self.tpl.setText(apply_toggles(t.text, self.catno.isChecked(), self.reissue.isChecked(),
                                       self.raw_catno.isChecked() and self.raw_catno.isEnabled()))
        self.update_preview()

    def on_adv(self, on):
        for b in (self.catno, self.reissue, self.raw_catno):
            b.setEnabled((not on or self.current_template() is not None)
                         and (b is not self.raw_catno or (self.raw_catno_supported and self.catno.isChecked())))

    def on_custom(self, _):
        self.preset.blockSignals(True); self.preset.setCurrentIndex(self.preset.findData(None))
        self.preset.blockSignals(False)
        for b in (self.catno, self.reissue, self.raw_catno):
            b.setEnabled(False)
        self.sync_delete(); self.update_preview()

    # ------------------------------------------------------------ preview
    def _rel(self, p: Path) -> str:
        for r in self.settings.library_roots:
            if r in p.parents:
                return str(p.relative_to(r))
        return str(p)

    def preview_text(self) -> str:
        """sequential_sides is a setting, not template text, and templates.preview has no parameter
        for an unsaved value; %tracknumber% is the contract's always-numeric equivalent."""
        text = self.tpl.text()
        return text.replace("%position%", "%tracknumber%") if not self.sides.isChecked() else text

    def update_preview(self):
        sides_pending = self.sides.isChecked() and self.settings.sequential_sides
        self.note.setText("Side names (A1, B1…) show in the preview after you save." if sides_pending else "")
        try:
            pv = self.svc.templates.preview(self.preview_text(), self.sample_ids)
        except Exception as e:   # ValidationError (bad field) etc.: show it, don't crash
            self.preview.setPlainText(""); self.issues.setText(f"⚠ {e}")
            self.last_preview = None
            return
        self.last_preview = pv
        self.preview.setPlainText("\n".join(self._rel(after) for _before, after in pv.moves))
        issues = []
        for c in pv.collisions:
            names = ", ".join(self._album_name(a) for a in c.album_ids)
            what = "files in one album" if len(c.album_ids) == 1 else "albums"
            issues.append(f"⚠ Collision ({what}): {self._rel(c.target)} ← {names}. "
                          "Apply will refuse these albums.")
        issues += [f"⚠ {w}" for w in pv.warnings]
        self.issues.setText("\n".join(issues))

    def _album_name(self, album_id):
        try:
            a = self.svc.library.album(album_id)
            return f"{a.artist} – {a.title}"
        except Exception:
            return f"album {album_id}"

    # ------------------------------------------------------------ actions
    def reset_default(self):
        self.adv.setChecked(False)
        for b in (self.catno, self.reissue):
            b.blockSignals(True); b.setChecked(True); b.blockSignals(False)
        self.raw_catno.blockSignals(True); self.raw_catno.setChecked(False); self.raw_catno.blockSignals(False)
        self.raw_catno.setEnabled(self.raw_catno_supported)
        self.sides.blockSignals(True); self.sides.setChecked(True); self.sides.blockSignals(False)
        i = self.preset.findData("archivist")
        if i == self.preset.currentIndex():
            self.from_controls()
        else:
            self.preset.setCurrentIndex(i if i >= 0 else 0)

    def save(self):
        text = self.tpl.text()
        err = getattr(self.svc.types, "MMError", Exception)
        match = next((t for t in self.svc.templates.list() if t.text == text), None)
        try:
            if match is None:
                base = self.current_template()
                default = f"{base.name} (custom)" if base else "My layout"
                name, ok = QInputDialog.getText(self, "Save layout", "Name for this layout:", text=default)
                if not ok:
                    return
                match = self.svc.templates.save(name, text)
            self.settings = self.svc.settings.set(active_template=match.id,
                                                  sequential_sides=not self.sides.isChecked())
        except err as e:
            QMessageBox.warning(self, "Can't save layout", str(e))
            return
        self.saved_template = match
        self.accept()

    def delete_current(self):
        t = self.current_template()
        if not t or t.builtin:
            return
        if QMessageBox.question(self, "Delete layout", f"Delete “{t.name}”?") != QMessageBox.Yes:
            return
        try:
            self.svc.templates.delete(t.id)
        except getattr(self.svc.types, "MMError", Exception) as e:
            QMessageBox.warning(self, "Can't delete layout", str(e)); return
        self.settings = self.svc.settings.get()
        self.load_templates(self.settings.active_template)
        self.from_controls()
