"""History: every apply from the journal (journal.list), each undoable after a preview
(journal.undo_plan -> ApplyDialog -> journal.undo(op, plan_id) -> events.job_finished)."""
from __future__ import annotations

from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel, QPushButton, QTableWidget,
    QMessageBox, QTableWidgetItem, QVBoxLayout,
)

from .backend import Backend, as_backend


LEGACY_STATUS = "Older format, undo needs confirmation"
LEGACY_CONFIRM = ("This change was recorded by an older version in a format that can't be fully verified. "
                  "File moves undo normally, but restoring its tags means trusting that old record.\n\n"
                  "Restore the old tags as well?")


def when(dt) -> str:
    return dt.astimezone().strftime("%b %d, %I:%M %p")   # contract datetimes may be UTC: show local


class HistoryDialog(QDialog):
    def __init__(self, svc: Backend | None = None, parent=None):
        super().__init__(parent)
        self.svc = as_backend(svc)
        self._pending_jobs: set[int] = set()
        self.setWindowTitle("History"); self.resize(900, 420)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Every change this app makes is recorded here. Undo shows exactly what it will "
                             "restore before anything moves.", wordWrap=True))
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["When", "What", "Files", "Status"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers); self.table.verticalHeader().hide()
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.undo_btn = QPushButton("Preview undo…"); self.undo_btn.setStyleSheet("font-weight:600")
        row.addStretch(); row.addWidget(self.undo_btn)
        lay.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.Close); bb.rejected.connect(self.reject); lay.addWidget(bb)
        self.undo_btn.clicked.connect(self.preview_undo)
        self.table.itemSelectionChanged.connect(self.sync)
        self.svc.events.job_finished.connect(self.on_job_finished)
        self.finished.connect(lambda *_: self._disconnect())
        self.load()

    def _disconnect(self):
        try:
            self.svc.events.job_finished.disconnect(self.on_job_finished)
        except (RuntimeError, TypeError):
            pass

    def load(self):
        self.ops = list(self.svc.journal.list(0, 500).items)
        self.table.setRowCount(0)
        for op in self.ops:
            r = self.table.rowCount(); self.table.insertRow(r)
            status = self.status_text(op)
            for c, v in enumerate([when(op.started), op.summary, str(op.files), status]):
                it = QTableWidgetItem(v)
                if op.undone or op.kind == "undo":
                    it.setForeground(QBrush(QColor("#777")))
                self.table.setItem(r, c, it)
        if self.ops:
            self.table.selectRow(0)
        self.sync()

    def status_text(self, op) -> str:
        """Contract Operation fields: kind / undoable / undone / undone_by / undo_of."""
        by_id = {o.id: o for o in self.ops}
        if op.kind == "undo":
            orig = by_id.get(op.undo_of)
            return (f"Undo of “{orig.summary}” ({when(orig.started)})" if orig else
                    f"Undo of operation #{op.undo_of}" if op.undo_of is not None else "Undo")
        if op.undone:
            u = by_id.get(op.undone_by)
            return f"Undone {when(u.started)}" if u else "Undone"
        if op.undoable and getattr(op, "legacy", False):
            return LEGACY_STATUS
        if op.undoable:
            return "Can undo"
        return "Can't undo (journal missing)"

    def current(self):
        rows = self.table.selectionModel().selectedRows()
        return self.ops[rows[0].row()] if rows else None

    def sync(self):
        op = self.current()
        self.undo_btn.setEnabled(bool(op and op.undoable))

    def album_name(self, album_id) -> str:
        if not album_id:
            return "Album not in the library index"     # discogs-tag journals list album_id 0
        try:
            a = self.svc.library.album(album_id); return f"{a.artist} – {a.title}"
        except Exception:
            return f"Album {album_id}"

    def confirm_legacy(self, op) -> bool:
        """Explicit user confirm before trust_legacy=True (patched in tests)."""
        return QMessageBox.question(self, "Undo older-format change", LEGACY_CONFIRM,
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes

    def preview_undo(self):
        """undo_plan(trust_legacy=False) first; LEGACY_JOURNAL problems block until the user confirms,
        then re-plan with trust_legacy=True and review that preview before anything moves."""
        from .review import ApplyDialog
        op = self.current()
        if not op:
            return
        try:
            plan = self.svc.journal.undo_plan(op.id, trust_legacy=False)
        except Exception as e:
            QMessageBox.warning(self, "Can't undo", str(e)); return
        trusted = False
        while True:
            dlg = ApplyDialog(plan, self, undo=True, mock=self.svc.is_fake, trusted=trusted,
                              album_name=self.album_name)
            self.last_dialog = dlg
            r = dlg.exec()
            if r == ApplyDialog.TRUST_LEGACY and not trusted:
                if not self.confirm_legacy(op):
                    return
                plan = self.svc.journal.undo_plan(op.id, trust_legacy=True)
                trusted = True
                continue
            if r == QDialog.Accepted and not dlg.blocking:
                try:
                    job = self.svc.journal.undo(op.id, plan.id)
                except Exception as e:   # real: PlanStale unless this is the latest preview of the current state
                    QMessageBox.warning(self, "Undo needs a fresh preview",
                                        f"{e}\n\nThe files changed or another preview was made. Preview the undo again.")
                    return
                self._pending_jobs.add(job.id)
                self.undo_btn.setEnabled(False)
            return

    def on_job_finished(self, job):
        if job.id not in self._pending_jobs:
            return
        self._pending_jobs.discard(job.id)
        if job.state.name != "DONE":
            QMessageBox.warning(self, "Undo failed", job.message or f"Job {job.id} {job.state.name.lower()}")
        self.load()
