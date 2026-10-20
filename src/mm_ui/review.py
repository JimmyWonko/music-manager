"""Bulk Discogs review screen. Talks to the backend only through mm_ui.backend (contract calls)."""
from __future__ import annotations

import html
from pathlib import Path

from PySide6.QtCore import QAbstractListModel, QModelIndex, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut, QColor, QBrush
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListView, QListWidget, QListWidgetItem, QProgressBar, QProgressDialog, QMainWindow, QMessageBox, QPushButton, QSplitter, QTableWidget,
    QScrollArea, QSizePolicy, QTableWidgetItem, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ._meta import DISPLAY_NAME
from .backend import Backend, as_backend
from . import problems as P

DISCOGS_NOTICE = ("This application uses Discogs' API but is not affiliated with, sponsored or "
                  "endorsed by Discogs. 'Discogs' is a trademark of Zink Media, LLC.")

# Status filters/colours use MatchStatus *names* (contract-defined), so they work with either
# backend's enum class.
FILTERS = {
    "Needs review": {"NEEDS_REVIEW"},
    "Needs a look": {"HELD"},          # refused by plan.apply; AlbumRow.held_for says why
    "Matched": {"MATCHED"},
    "Accepted (queued)": {"ACCEPTED"},
    "No match": {"NO_MATCH"},
    "Skipped": {"SKIPPED"},
    "All": None,
}
STATUS_COLOR = {
    "NEEDS_REVIEW": "#c98a00", "MATCHED": "#2e8b57", "ACCEPTED": "#2b6cb0", "NO_MATCH": "#a33",
    "SKIPPED": "#777", "APPLIED": "#555", "HELD": "#c98a00",
}


# TrackPair.method (contract Literal): how a local file was paired with a Discogs track
METHOD_TEXT = {"number": "track number", "side": "side position", "title": "title",
               "position": "position (check)", "unmatched": "stays in place"}
METHOD_COLOR = {"position": "#c98a00", "unmatched": "#777"}


def is_held(album) -> bool:
    return album.status.name == "HELD"


def held_for(album) -> list:
    return list(getattr(album, "held_for", None) or []) if is_held(album) else []


class LayoutIndex:
    """Names for the lists. Loose tracks come from AlbumRow.loose_tracks (d6ac2f3) on the row itself; this index
    only remembers which folders hold loose tracks (rows seen so far, plus one small HELD query) so an album
    listed on its own below one gets a tooltip, and finds the layout problem of a held disc_folders album."""

    LIMIT = 2000

    def __init__(self, svc=None):
        self.loose: dict[Path, object] = {}
        if svc is None:
            return
        try:
            rows = svc.albums({"HELD"}, limit=self.LIMIT).items
        except Exception:
            rows = []
        self.add(rows)

    def add(self, rows):
        for r in rows:
            if P.is_loose_tracks(r):
                self.loose[Path(str(r.folder))] = r

    def problem(self, row):
        return P.layout_problem(held_for(row))

    def parent(self, row):
        """Row of the nearest folder above this album that holds loose tracks, or None."""
        for anc in Path(str(row.folder)).parents:
            if anc in self.loose:
                return self.loose[anc]
        return None

    def names(self, row) -> tuple[str, str]:
        return P.display_names(row, self.problem(row))

    def label(self, row) -> str:
        return P.row_label(*self.names(row), loose=P.is_loose_tracks(row))

    def tooltip(self, row) -> str:
        lp = self.problem(row)
        if P.is_loose_tracks(row):
            folder = Path(str(row.folder)).name
            out = (f"\n{P.loose_label(self.names(row)[0])}. {folder}/ has loose audio files and subfolders with audio; "
                   "the engine can't tell an artist folder from an album with extra folders, so nothing in it is "
                   "applied. Move the loose files (or the subfolders) and rescan.")
            dirs = P.layout_dirs(lp) if lp is not None else []
            return out + ("\nNext to: " + ", ".join(f"{d}/" for d in dirs) if dirs else "")
        if lp is not None:
            out = f"\n{P.label(lp)}: {P.layout_hint(lp)}"
            dirs = P.layout_dirs(lp)
            return out + ("\nNext to: " + ", ".join(f"{d}/" for d in dirs) if dirs else "")
        note = P.layout_note(row, self.parent(row))
        return f"\n{note}" if note else ""


def mmss(s):
    if s is None:
        return ""
    s = int(round(s)); return f"{s // 60}:{s % 60:02d}"


def disc_tooltip(conflict) -> str:
    """Review table: why a refused file has no disc, and the tracks it could be (contract "Disc conflicts")."""
    if conflict is None:     # older service: no disc_conflicts
        return ("Can't work out the disc layout: this file's track number repeats on several discs and nothing "
                "says which disc it is. It stays in place.")
    lines = [f"Can't work out the disc layout: {P.disc_reason(conflict)}."] + P.disc_conflict_detail(conflict)
    return "\n".join(lines + ["It stays in place until the files say which disc it is."])


class AlbumListModel(QAbstractListModel):
    PAGE = 200

    def __init__(self, svc: Backend):
        super().__init__()
        self.svc, self.filter, self.rows, self.total = svc, svc.album_filter(), [], 0
        self.layout = LayoutIndex()

    def set_filter(self, f):
        self.beginResetModel()
        self.filter = f
        page = self.svc.library.albums(f, self.svc.types.AlbumSort.ARTIST, 0, self.PAGE)
        self.rows, self.total = list(page.items), page.total
        self.layout = LayoutIndex(self.svc); self.layout.add(self.rows)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def canFetchMore(self, parent):
        return len(self.rows) < self.total

    def fetchMore(self, parent):
        page = self.svc.library.albums(self.filter, self.svc.types.AlbumSort.ARTIST, len(self.rows), self.PAGE)
        self.beginInsertRows(QModelIndex(), len(self.rows), len(self.rows) + len(page.items) - 1)
        self.rows += page.items
        self.layout.add(page.items)
        self.endInsertRows()

    def data(self, idx, role=Qt.DisplayRole):
        a = self.rows[idx.row()]
        if role == Qt.DisplayRole:
            conf = f"{a.confidence:.0%}" if a.confidence is not None else "—"
            return self.layout.label(a) + f"   {conf}" + ("   ⚠ Needs a look" if is_held(a) else "")
        if role == Qt.ForegroundRole:
            return QBrush(QColor(STATUS_COLOR.get(a.status.name, "#000")))
        if role == Qt.ToolTipRole:
            tip = f"{a.folder}\n{a.status.name.replace('_', ' ').title()}"
            return tip + "".join(f"\n⚠ {P.one_line(p)}" for p in held_for(a)) + self.layout.tooltip(a)
        if role == Qt.UserRole:
            return a.id

    def refresh_album(self, album_id):
        for i, a in enumerate(self.rows):
            if a.id == album_id:
                self.rows[i] = self.svc.library.album(album_id)
                self.dataChanged.emit(self.index(i), self.index(i))


class _PreviewLabel(QLabel):
    """The "After apply" text: wraps, and tells its scroll area to refit whenever the text changes."""

    def __init__(self):
        super().__init__()
        self.setWordWrap(True); self.setAlignment(Qt.AlignLeft | Qt.AlignTop)

    def setText(self, text):
        super().setText(text); self._refit()

    def clear(self):
        super().clear(); self._refit()

    def _refit(self):
        p = self.parentWidget()
        while p is not None and not isinstance(p, _FitScroll):
            p = p.parentWidget()
        if p is not None:
            p.updateGeometry()


class _FitScroll(QScrollArea):
    """As tall as its text up to MAX_H, then scrolls; never makes the window wider than the screen."""
    MAX_H = 260

    def __init__(self, label):
        super().__init__()
        self.setWidgetResizable(True); self.setFrameShape(QFrame.NoFrame); self.setWidget(label)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

    def _height(self):
        w = self.widget()
        width = max(self.viewport().width(), 200)
        h = w.heightForWidth(width) if w.hasHeightForWidth() else w.sizeHint().height()
        return min(self.MAX_H, max(h, w.minimumSizeHint().height()) + 2)

    def sizeHint(self):
        return QSize(super().sizeHint().width(), self._height())

    def minimumSizeHint(self):
        return QSize(100, self._height())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.height() != self._height():
            self.updateGeometry()


class ReviewWindow(QMainWindow):
    plan_flow_done = Signal()        # open_apply finished (dialog closed / apply started / build failed)
    def __init__(self, svc: Backend | None = None):
        super().__init__()
        self.svc, self.cand_idx, self.album_id = as_backend(svc), 0, None
        self._pending_jobs: dict[int, str] = {}   # job_id -> "apply"
        self._pending_counts: dict[int, tuple[int, int]] = {}   # job_id -> (clean, held) from the reviewed plan
        svc = self.svc
        self.setWindowTitle(f"{DISPLAY_NAME} — Discogs review")
        self.resize(1240, 760)

        # top bar
        self.counts = QLabel()
        self.filter_box = QComboBox(); self.filter_box.addItems(FILTERS)
        self.search = QLineEdit(placeholderText="Search artist or album…")
        self.accept_all = QPushButton("Accept clear winners")
        self.accept_all.setToolTip("Same rules as --auto (match.auto_rules)")
        top = QHBoxLayout()
        for w in (self.counts,):
            top.addWidget(w)
        top.addStretch(); top.addWidget(self.filter_box); top.addWidget(self.search); top.addWidget(self.accept_all)

        # album list
        self.model = AlbumListModel(svc)
        self.list = QListView(); self.list.setModel(self.model); self.list.setUniformItemSizes(True)

        # detail pane
        self.cand_title = QLabel(); self.cand_title.setStyleSheet("font-size:15px;font-weight:600")
        self.prev_btn, self.next_btn = QPushButton("◂"), QPushButton("▸")
        self.credit = QLabel(); self.credit.setOpenExternalLinks(True)
        self.credit.setStyleSheet("color:#666;font-size:11px")
        head = QHBoxLayout()
        head.addWidget(self.prev_btn); head.addWidget(self.cand_title, 1); head.addWidget(self.next_btn)
        self.release_info = QLabel(); self.release_info.setStyleSheet("color:#333")
        self.artwork = QLabel(); self.artwork.setFixedSize(96, 96); self.artwork.setAlignment(Qt.AlignCenter)
        self.artwork.setWordWrap(True)
        self.artwork.setStyleSheet("background:#e6e6e6;color:#777;border-radius:4px;font-size:11px")
        self.breakdown = QLabel(); self.breakdown.setTextFormat(Qt.RichText)
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(["#", "Local file", "Len", "Pos", "Discogs track", "Len", "", "Matched by"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.verticalHeader().hide(); self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.loading = QLabel("Fetching full release from Discogs… track alignment and artwork appear when it's ready.")
        self.loading.setStyleSheet("color:#8a6d00;background:#fff6d6;padding:6px;border-radius:4px")
        self.preview = _PreviewLabel(); self.preview.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._preview_css = "font-family:monospace;background:#f3f3f3;padding:6px;border-radius:4px;"
        self.preview.setStyleSheet(self._preview_css)
        self.skip_btn = QPushButton("Skip  (S)")
        self.other_btn = QPushButton("Other release…")
        self.submit_btn = QPushButton("Submit to Discogs")
        self.accept_btn = QPushButton("Accept  (Enter)"); self.accept_btn.setDefault(True)
        self.accept_btn.setStyleSheet("font-weight:600")
        actions = QHBoxLayout()
        for b in (self.skip_btn, self.other_btn, self.submit_btn):
            actions.addWidget(b)
        actions.addStretch(); actions.addWidget(self.accept_btn)

        detail = QWidget(); dl = QVBoxLayout(detail)
        info_row = QHBoxLayout(); info_box = QVBoxLayout()
        info_box.addWidget(self.release_info); info_box.addWidget(self.breakdown)
        info_row.addWidget(self.artwork); info_row.addLayout(info_box, 1)
        dl.addLayout(head); dl.addLayout(info_row)
        dl.addWidget(self.loading); dl.addWidget(self.table, 1)
        # long engine messages (disc conflicts, folder mismatches) scroll here instead of widening the window
        self.preview_scroll = _FitScroll(self.preview)
        dl.addWidget(QLabel("After apply:")); dl.addWidget(self.preview_scroll)
        self.naming_btn = QPushButton("Change naming template…")   # shown for disc_folder_mismatch
        self.naming_btn.setToolTip("The naming template puts some tracks into another disc's CD folder")
        self.naming_btn.clicked.connect(self.show_naming)
        self.naming_btn.hide()
        nb = QHBoxLayout(); nb.addWidget(self.naming_btn); nb.addStretch(1); dl.addLayout(nb)
        dl.addLayout(actions); dl.addWidget(self.credit)
        dl.addStretch(0)        # a short track table hands its spare height to the bottom, not a gap mid-pane

        split = QSplitter(); split.addWidget(self.list); split.addWidget(detail); split.setSizes([330, 910])

        # bottom bar
        self.queue_label = QLabel()
        self.rate_label = QLabel(); self.rate_label.setStyleSheet("color:#666")
        self.apply_btn = QPushButton("Review && apply batch…")
        bottom = QHBoxLayout(); bottom.addWidget(self.queue_label); bottom.addWidget(self.rate_label)
        bottom.addStretch(); bottom.addWidget(self.apply_btn)

        central = QWidget(); cl = QVBoxLayout(central)
        cl.addLayout(top); cl.addWidget(split, 1)
        line = QFrame(); line.setFrameShape(QFrame.HLine); cl.addWidget(line); cl.addLayout(bottom)
        self.setCentralWidget(central)
        self.menuBar().addMenu("&Library").addAction("History && undo…", self.show_history)
        self.menuBar().addMenu("&Settings").addAction("File naming…", self.show_naming)
        self.menuBar().addMenu("&Help").addAction("About", self.show_about)

        # wiring
        self.filter_box.currentTextChanged.connect(self.reload)
        self.search.textChanged.connect(self.reload)
        self.list.selectionModel().currentChanged.connect(self.on_select)
        self.prev_btn.clicked.connect(lambda: self.step_candidate(-1))
        self.next_btn.clicked.connect(lambda: self.step_candidate(1))
        self.accept_btn.clicked.connect(self.accept_current)
        self.skip_btn.clicked.connect(self.skip_current)
        self.submit_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(self.svc.provider.submit_link(self.album_id))))
        self.accept_all.clicked.connect(self.accept_confident)
        self.apply_btn.clicked.connect(self.open_apply)
        svc.events.candidate_ready.connect(self.on_candidate_ready)
        svc.events.album_changed.connect(self.on_album_changed)
        svc.events.job_finished.connect(self.on_job_finished)
        svc.events.job_progress.connect(self.on_job_progress)
        svc.events.rate_limited.connect(lambda s: self.rate_label.setText(f"   Discogs: next request in {s:.0f}s"))
        for key, fn in (("J", lambda: self.move(1)), ("K", lambda: self.move(-1)),
                        ("S", self.skip_current), ("Return", self.accept_current)):
            QShortcut(QKeySequence(key), self, fn)
        for n in range(1, 5):
            QShortcut(QKeySequence(str(n)), self, lambda n=n: self.pick_candidate(n - 1))

        self.reload()

    # ---------------------------------------------------------------- data
    def reload(self, *_):
        self.model.set_filter(self.svc.album_filter(FILTERS[self.filter_box.currentText()], self.search.text()))
        self.update_counts()
        if self.model.rowCount():
            self.list.setCurrentIndex(self.model.index(0))
        else:
            self.album_id = None; self.render()

    def update_counts(self):
        c = {s.name: n for s, n in self.svc.library.status_counts().items()}; total = sum(c.values())
        summary = P.status_summary(c)          # every non-zero status, Accepted / Held / Applied included
        self.counts.setText(f"<b>{total:,} albums</b>" + (f" · {summary}" if summary else ""))
        q, held = c.get("ACCEPTED", 0), c.get("HELD", 0)
        look = f" · <b style='color:#c98a00'>{held} need a look</b>" if held else ""
        if getattr(self, "build_progress", None) is not None:
            return      # building / fetching from Discogs: album_changed must not reset the label or re-enable Apply
        self.queue_label.setText(f"<b>{q} queued</b>{look} · nothing moves until you apply")
        self.apply_btn.setEnabled(q + held > 0)

    def on_select(self, cur, _prev=None):
        if not cur.isValid():
            return
        self.album_id, self.cand_idx = cur.data(Qt.UserRole), 0
        self.render()

    def on_candidate_ready(self, album_id, _release_id):
        if album_id == self.album_id:
            self.render()

    def on_album_changed(self, album_id):
        self.model.refresh_album(album_id); self.update_counts()

    # ---------------------------------------------------------------- render
    def render(self):
        has = self.album_id is not None
        cands = self.svc.match.candidates(self.album_id) if has else []
        for w in (self.accept_btn, self.prev_btn, self.next_btn):
            w.setEnabled(bool(cands))
        self.skip_btn.setEnabled(has)
        self.submit_btn.setVisible(has and not cands)
        self.table.setRowCount(0)
        if not has:
            self.cand_title.setText("No albums in this view"); self.release_info.clear(); self.show_artwork(None)
            self.breakdown.clear(); self.preview.clear(); self.credit.clear(); self.loading.hide()
            return
        album = self.svc.library.album(self.album_id)
        if not cands:
            name = P.row_label(*P.display_names(album, P.layout_problem(held_for(album))), loose=P.is_loose_tracks(album))
            name += " ·" if P.is_loose_tracks(album) else ":"      # "Black Flag: loose tracks … (held) · …"
            if album.status.name in ("UNMATCHED", "QUEUED"):
                self.cand_title.setText(f"{name} not matched yet")
                self.release_info.setText("Run “Find Discogs matches” from the library first.")
            else:
                self.cand_title.setText(f"{name} no Discogs match")
                self.release_info.setText("Try “Other release…” with a Discogs URL, or submit this release to Discogs.")
            self.breakdown.clear(); self.preview.setStyleSheet(self._preview_css); self.preview.setToolTip("")
            self.preview.setText(str(album.folder)); self.loading.hide()
            self.credit.clear(); self.show_artwork(None); self.fill_local_only(album); self._fit_table(); return
        c = cands[self.cand_idx]
        lead = (f"  ·  leads runner-up by {c.margin * 100:.0f} pts" if c.margin is not None
                else "  ·  only candidate" if len(cands) == 1 else "")
        auto = getattr(c, "auto_score", None)        # 4dc5e69: the --auto score "Accept clear winners" uses
        auto_txt = f"  ·  auto {auto:.0%}" if auto is not None and round(auto, 2) != round(c.score, 2) else ""
        self.cand_title.setText(f"Candidate {self.cand_idx + 1} of {len(cands)}  ·  {c.score:.0%}{lead}{auto_txt}")
        self.cand_title.setToolTip(
            "Score: the search match (album, artist, cat. no., year) plus, once the full release is fetched, "
            "track count and durations.\nAuto score: the search match alone; “Accept clear winners” uses it."
            if auto is not None else "")
        self.release_info.setText(
            f"<b>{c.artist} – {c.title}</b> &nbsp; {c.label} · {c.catno} · {c.country} {c.year} · {', '.join(c.formats)}")
        chips = []
        for p in c.breakdown:
            if p.field == "durations" and not p.weight and not p.local and not p.remote:
                continue                            # durations unknown: no share of the score, no chip
            col = "#2e8b57" if p.ok else "#b33"
            mark = "✓" if p.ok else "✗"
            tip = (f"{p.local} → {p.remote} · " if p.local or p.remote else "") + f"{p.weight:.0%} of the score"
            chips.append(f"<span style='color:{col}' title='{html.escape(tip, quote=True)}'>{p.field.replace('_', ' ')} {mark}</span>")
        self.breakdown.setText("Score: &nbsp;" + " &nbsp;·&nbsp; ".join(chips))
        self.loading.setVisible(not c.fetched)
        dest = self._destination(c)
        amb = {Path(str(x)): p for p in (dest.problems if dest is not None and not isinstance(dest, Exception) else [])
               if P.kind_of(p) == P.DISC_AMBIGUOUS for x in p.paths}     # refused file -> its problem
        if c.alignment:
            for pair in c.alignment:
                r = self.table.rowCount(); self.table.insertRow(r)
                lt, rt = pair.local, pair.remote
                vals = [(str(lt.number) if lt.number is not None else "") if lt else "", lt.title if lt else "—", mmss(lt.duration_s) if lt else "",
                        rt.position if rt else "", rt.title if rt else "—", mmss(rt.duration_s) if rt else "",
                        "✓" if pair.ok else f"✗ {pair.duration_delta_s:+.0f}s" if pair.duration_delta_s else "✗",
                        METHOD_TEXT.get(str(getattr(pair, "method", "")), str(getattr(pair, "method", "")))]
                refused = lt is not None and rt is None and Path(str(lt.path)) in amb   # disc_ambiguous: not guessed
                conflict = P.disc_conflict_for(amb[Path(str(lt.path))], lt.path) if refused else None
                if refused:
                    vals[7] = "discs disagree" if conflict is not None and conflict.reason == "sources_disagree" \
                        else "disc unknown"
                for col, v in enumerate(vals):
                    it = QTableWidgetItem(v)
                    if col == 6:
                        it.setForeground(QBrush(QColor("#2e8b57" if pair.ok else "#b33")))
                    if col == 7 and refused:
                        it.setForeground(QBrush(QColor("#b33")))
                        it.setToolTip(disc_tooltip(conflict))
                    elif col == 7 and pair.method in METHOD_COLOR:
                        it.setForeground(QBrush(QColor(METHOD_COLOR[pair.method])))
                    self.table.setItem(r, col, it)
        else:
            self.fill_local_only(album)
        self.show_destination(c, dest)
        self.credit.setText(f'<a href="{c.source.url}">{c.source.text}</a>')
        self.show_artwork(c)
        self._fit_table()

    def _fit_table(self):
        """Track table only as tall as its rows (it still scrolls when the pane is too short): a 2-track held
        album shouldn't leave a big white gap above "After apply"."""
        t = self.table
        rh = t.verticalHeader().defaultSectionSize()
        h = (t.horizontalHeader().sizeHint().height() + max(t.rowCount(), 3) * rh + 2 * t.frameWidth()
             + t.horizontalScrollBar().sizeHint().height())
        t.setMaximumHeight(h)

    def show_artwork(self, c):
        """Candidate.artwork is a local image once downloaded; the real backend never downloads (always None),
        so show a placeholder rather than fetching anything from the UI."""
        from PySide6.QtGui import QPixmap
        pm = QPixmap(str(c.artwork)) if c is not None and c.artwork else QPixmap()
        if not pm.isNull():
            self.artwork.setPixmap(pm.scaled(96, 96, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            self.artwork.setToolTip(str(c.artwork))
        else:
            self.artwork.setPixmap(QPixmap())
            self.artwork.setText("No cover\npreview" if c is not None else "")
            self.artwork.setToolTip("Cover art isn't downloaded for preview" +
                                    (" (saved as folder.jpg on apply)" if c is not None and c.artwork_url else ""))

    def _destination(self, c):
        """match.destination for the shown candidate (Destination, or the exception it raised)."""
        try:
            return self.svc.match.destination(self.album_id, c.release_id, self.svc.active_template())
        except Exception as e:      # MMError / NotFound from the backend: shown, not raised
            return e

    def show_destination(self, c, d=None):
        """'After apply' line: match.destination works before choose, with the active template."""
        d = self._destination(c) if d is None else d
        self.naming_btn.setVisible(not isinstance(d, Exception) and (
            P.has_folder_mismatch(d.problems) or P.has_folder_mismatch(held_for(self.svc.library.album(self.album_id)))))
        if isinstance(d, Exception):
            self.preview.setStyleSheet(self._preview_css + "color:#a33;")
            self.preview.setText(f"⚠ {d}")
            return
        rel = lambda p: self.svc.relative(p, d.library_root)
        lines = [rel(d.files[0][1]) if d.files else
                 f"{rel(d.folder)}/   (file names appear once the release is fetched)"]
        if d.files and len(d.files) > 1:
            lines[0] += f"   (+{len(d.files) - 1} more)"
        lines += [f"⚠ {P.one_line(p)}" for p in d.problems]
        album = self.svc.library.album(self.album_id)
        if not d.problems:
            lines += [f"⚠ Held back at apply: {P.one_line(p)}" for p in held_for(album)]
        probs = list(d.problems) or held_for(album)
        lp = P.layout_problem(probs)
        if lp is not None:          # the files involved (Problem.paths is just the album folder)
            lines += ["    " + x for x in P.layout_files(lp, album.folder, [t.path for t in album.tracks])]
        for dp in (p for p in probs if P.kind_of(p) == P.DISC_AMBIGUOUS):     # per file: why + the choices
            lines += ["    " + x for x in P.disc_conflict_lines(dp, album.folder)]
        for fp in (p for p in probs if P.kind_of(p) == P.DISC_FOLDER_MISMATCH):   # file → CDn/ (disc tag m)
            lines += ["    " + x for x in P.folder_mismatch_lines(fp, album.folder)]
        self.preview.setStyleSheet(self._preview_css + ("color:#a33;" if len(lines) > 1 else ""))
        self.preview.setText("\n".join(lines))
        self.preview.setToolTip(f"Library: {d.library_root}\n" +
                                "\n".join(f"{b.name} → {rel(a)}" for b, a in d.files[:40]))

    def fill_local_only(self, album):
        for t in album.tracks:
            r = self.table.rowCount(); self.table.insertRow(r)
            for col, v in enumerate([str(t.number) if t.number is not None else "", t.title, mmss(t.duration_s), "", "…", "", "", ""]):
                self.table.setItem(r, col, QTableWidgetItem(v))

    # ---------------------------------------------------------------- actions
    def move(self, step):
        row = self.list.currentIndex().row() + step
        if 0 <= row < self.model.rowCount():
            self.list.setCurrentIndex(self.model.index(row))

    def step_candidate(self, step):
        n = len(self.svc.match.candidates(self.album_id)) if self.album_id else 0
        if n:
            self.cand_idx = (self.cand_idx + step) % n; self.render()

    def pick_candidate(self, i):
        if self.album_id and i < len(self.svc.match.candidates(self.album_id)):
            self.cand_idx = i; self.render()

    def accept_current(self):
        cands = self.svc.match.candidates(self.album_id) if self.album_id else []
        if cands:
            row = self.list.currentIndex().row()
            self.svc.match.choose(self.album_id, cands[self.cand_idx].release_id)
            self.after_decision(row)

    def skip_current(self):
        if self.album_id:
            row = self.list.currentIndex().row()
            self.svc.match.skip(self.album_id); self.after_decision(row)

    def after_decision(self, row):
        # Keep the reviewer moving: re-filter, stay at the same position in the list.
        self.reload()
        if self.model.rowCount():
            self.list.setCurrentIndex(self.model.index(min(row, self.model.rowCount() - 1)))

    def accept_confident(self):
        """Same check as --auto: match.auto_accept with match.auto_rules(). Never overrides track_mismatch:
        require_track_count stays as the rules say, and plan.build gets no allow_track_mismatch from here."""
        rules = self.svc.match.auto_rules()
        page = self.svc.albums({"MATCHED", "NEEDS_REVIEW"}, limit=10**6)
        picks, waiting = [], 0
        for a in page.items:
            cands = self.svc.match.candidates(a.id)
            win = self.svc.match.auto_accept(cands, rules, local_track_count=a.track_count)
            if win is not None:
                picks.append((a, win))
            elif cands and rules.require_track_count and not cands[0].fetched:
                waiting += 1      # None = not a clear winner YET: its track count can't be checked until fetched
        self.last_auto_waiting = waiting
        wait_note = (f" {waiting} more can't be checked until their full release is fetched." if waiting else "")
        if not picks:
            QMessageBox.information(self, "Accept clear winners",
                                    "No album has a clear winner under the auto rules." + wait_note)
            return
        if self.confirm_accept_all(len(picks), rules, waiting):
            for a, win in picks:
                self.svc.match.choose(a.id, win.release_id)
            self.reload()

    def confirm_accept_all(self, n, rules, waiting=0) -> bool:
        return QMessageBox.question(
            self, "Accept clear winners",
            f"Queue {n} albums whose top candidate scores at least {rules.min_score:.0%} and leads the runner-up "
            f"by {rules.min_margin * 100:.0f}+ points"
            + (", with matching track counts" if rules.require_track_count else "")
            + "? Nothing moves until you apply the batch."
            + (f" ({waiting} album(s) aren't clear winners yet: their full release isn't fetched.)" if waiting else "")
            ) == QMessageBox.Yes

    def open_apply(self):
        """Build the plan OFF the Qt thread, with a progress indicator; then review it in ApplyDialog.
        plan.build never calls Discogs: when Plan.fetch_job is set, show that job's progress, wait for its
        job_finished and build again. Emits plan_flow_done when the flow ends (tests)."""
        ids = [a.id for a in self.svc.albums({"ACCEPTED", "HELD"}, limit=10**6).items]   # re-plan held ones too
        if not ids:
            return
        self._apply_ids, self._allow, self._fetch_rounds = ids, set(), 0
        self._build_plan()

    def _wait_for_fetch(self, job_id, n):
        """Plan.fetch_job: releases are downloading in a background job; rebuild on its job_finished."""
        self._fetch_rounds += 1
        self._fetch_job_id = job_id
        if getattr(self, "build_progress", None) is not None:
            self.build_progress.setLabelText(f"Fetching {n} release(s) from Discogs… the plan is built again "
                                             "when they arrive.")
        self.queue_label.setText("<b>Fetching from Discogs…</b>")
        if getattr(self, "build_progress", None) is not None:
            self.build_progress.setRange(0, 0)            # spinner until the job reports progress
        try:
            done = self.svc.jobs.get(job_id).state.name in ("DONE", "FAILED", "CANCELLED")
        except Exception:
            done = True
        if done:            # finished before we started listening
            self._fetch_job_id = None
            self._build_plan(keep_progress=True)

    def on_job_progress(self, job):
        if job.id == getattr(self, "_fetch_job_id", None) and getattr(self, "build_progress", None) is not None:
            if job.total:
                self.build_progress.setRange(0, job.total); self.build_progress.setValue(job.done)
            if job.message:
                self.build_progress.setLabelText(f"Fetching from Discogs… {job.message}")

    def _build_plan(self, keep_progress=False):
        from .worker import run_async
        ids, allow, tpl = list(self._apply_ids), frozenset(self._allow), self.svc.active_template()
        self.apply_btn.setEnabled(False)
        if keep_progress and getattr(self, "build_progress", None) is not None:
            run_async(lambda: self.svc.plan.build(ids, tpl, allow_track_mismatch=allow),
                      self._on_plan_built, self._on_plan_failed)
            return
        self._close_progress()
        self.build_progress = pd = QProgressDialog("Building the plan…", "", 0, 0, self)
        pd.setCancelButton(None); pd.setWindowTitle("Preparing batch"); pd.setWindowModality(Qt.WindowModal)
        pd.setMinimumDuration(300)
        self.queue_label.setText("<b>Building plan…</b>")
        run_async(lambda: self.svc.plan.build(ids, tpl, allow_track_mismatch=allow),   # read-only; Template.id
                  self._on_plan_built, self._on_plan_failed)

    def _close_progress(self):
        if getattr(self, "build_progress", None) is not None:
            self.build_progress.reset(); self.build_progress.close(); self.build_progress.deleteLater()
            self.build_progress = None

    def _on_plan_failed(self, e):
        self._close_progress()
        QMessageBox.warning(self, "Can't build the plan", str(e))
        self.update_counts()
        self.plan_flow_done.emit()

    def _on_plan_built(self, plan):
        fetch_job = getattr(plan, "fetch_job", None)       # contract 2026-10-17; absent on older services
        if fetch_job is not None and self._fetch_rounds < 3:
            self._wait_for_fetch(fetch_job, sum(1 for ap in plan.albums if P.waiting(ap)))
            return
        self._close_progress()
        dlg = ApplyDialog(plan, self, mock=self.svc.is_fake, album_name=self.album_name, album_row=self.album_row)
        self.last_apply_dialog = dlg
        r = dlg.exec()
        if r == ApplyDialog.OPEN_NAMING:              # disc_folder_mismatch: fix the template, then re-check
            self.show_naming()
            self._build_plan()
            return
        if r == ApplyDialog.OVERRIDE and dlg.override_album is not None:
            self._allow.add(dlg.override_album)       # one album at a time, after its own confirm
            self._build_plan()
            return
        if r == QDialog.Accepted and dlg.clean_aps:
            try:
                job = self.svc.plan.apply(plan.id)     # -> Job; result via events.job_finished
            except Exception as e:                     # nothing applicable -> MMError right away; twice -> PlanStale
                self.show_apply_error(e, dlg)
                self.reload()
            else:
                self._pending_jobs[job.id] = "apply"
                self._pending_counts[job.id] = (len(dlg.clean_aps), len(dlg.held_aps), len(dlg.waiting_aps))
                self.apply_btn.setEnabled(False)
                self.queue_label.setText("<b>Applying…</b>")
        else:
            self.update_counts()
        self.plan_flow_done.emit()

    def on_job_finished(self, job):
        if job.id == getattr(self, "_fetch_job_id", None):
            self._fetch_job_id = None
            self._build_plan(keep_progress=True)      # releases are in: build again (contract: can't loop)
            return
        what = self._pending_jobs.pop(job.id, None)
        if what is None:
            return
        n_clean, n_held, n_wait = self._pending_counts.pop(job.id, (0, 0, 0))
        if job.state.name != "DONE":
            self.show_apply_failed(job, n_clean)
        else:     # held albums are now status HELD in the library (held_for says why)
            self.show_summary(self.apply_summary(n_clean, n_held, n_wait))
        self.reload()

    def show_apply_error(self, e, dlg=None):
        """plan.apply raised before starting a job. Bassline's service raises MMError right away when nothing
        in the plan can be applied; PlanStale when the plan was already used. Nothing was changed either way."""
        self.last_apply_error = e
        T = self.svc.types
        detail = str(e).removeprefix("MMError: ")
        if isinstance(e, getattr(T, "PlanStale", ())):
            title, text = "Plan is out of date", ("This plan was already used. Nothing was changed. "
                                                  "Click “Review & apply batch…” again to build a fresh one.")
        elif isinstance(e, getattr(T, "MMError", ())):
            held = len(dlg.held_aps) if dlg is not None else 0
            wait = len(dlg.waiting_aps) if dlg is not None else 0
            why = []
            if held:
                why.append(f"{held} held back for a look")
            if wait:
                why.append(f"{wait} still fetching from Discogs")
            title = "Nothing to apply"
            text = ("None of the albums in this batch can be applied right now"
                    + (f" ({', '.join(why)})" if why else "") + ". Nothing was moved or tagged.\n\n"
                    "Fix the albums under “Needs a look”, or wait for Discogs, then review the batch again.")
        else:
            title, text = "Can't apply", "The batch couldn't be started. Nothing was changed."
        self.last_apply_message = (title, text, detail)
        QMessageBox.warning(self, title, f"{text}\n\nDetails: {detail}" if detail else text)

    def show_apply_failed(self, job, n_clean=0):
        """The apply job ended FAILED. When every album was refused during the run (files changed since the
        plan was built, or problems), the service applies nothing and they become HELD: say so plainly."""
        msg = (job.message or "").removeprefix("MMError: ")
        if P.nothing_applied(msg):
            n = P.refused_count(msg) or n_clean
            title = "Nothing was applied"
            text = (f"All {n} album(s) were refused while applying, so nothing was moved or tagged. They changed "
                    "on disk since the plan was built or have problems, and are now listed under “Needs a look”."
                    "\n\nReview the batch again to rebuild the plan.") if n else (
                    "Nothing was moved or tagged. Review the batch again to rebuild the plan.")
        else:
            title = "Apply failed"
            text = msg or f"Job {job.id} {job.state.name.lower()}"
        self.last_apply_message = (title, text, msg)
        QMessageBox.warning(self, title, f"{text}\n\nDetails: {msg}" if P.nothing_applied(msg) else text)

    @staticmethod
    def apply_summary(applied: int, held: int, waiting: int = 0) -> str:
        s = f"Applied {applied} album{'s' if applied != 1 else ''}."
        if held:
            s += f" {held} held back for a look."
        if waiting:
            s += f" {waiting} left out until Discogs sends the release."
        return s

    def show_summary(self, text: str):
        """Post-apply summary (patched in tests)."""
        self.last_summary = text
        QMessageBox.information(self, "Apply finished", text)

    def album_name(self, album_id) -> str:
        if not album_id:
            return "Album not in the library index"     # discogs-tag journals list album_id 0
        try:
            a = self.svc.library.album(album_id)
        except Exception:
            return f"Album {album_id}"
        return LayoutIndex().label(a)

    def album_row(self, album_id):
        """Library row for ApplyDialog's layout_ambiguous names (None when not in the index)."""
        try:
            return self.svc.library.album(album_id) if album_id else None
        except Exception:
            return None

    def show_history(self):
        from .history import HistoryDialog
        HistoryDialog(self.svc, self).exec(); self.reload()

    def show_naming(self):
        from .naming import NamingDialog
        r = NamingDialog(self.svc, self).exec()
        if self.album_id is not None:
            self.render()              # the After line and its problems follow the template
        return r

    def show_about(self):
        QMessageBox.about(self, f"About {DISPLAY_NAME}",
                          f"<p><b>{DISPLAY_NAME}</b> (prototype · {self.svc.label})<br><small>{self.svc.label_detail}</small></p><p>{DISCOGS_NOTICE}</p>"
                          '<p><a href="https://www.discogs.com/">Data provided by Discogs</a></p>')


GREY = "#888"


def _rel_to(path, folder) -> str:
    """A file's new path shown relative to its album's new folder (the Folder row shows that folder)."""
    try:
        return str(path.relative_to(folder))
    except (ValueError, AttributeError, TypeError):
        return str(path)


def _fit_list_height(lw: QListWidget, cap: int) -> None:
    """A QListWidget only as tall as its items (up to cap), so two held albums don't leave a white box."""
    h = sum(lw.sizeHintForRow(i) for i in range(lw.count())) + 2 * lw.frameWidth() + 4
    if lw.sizeHintForColumn(0) > lw.width() - 2 * lw.frameWidth():     # long lines: leave room for the scrollbar
        h += lw.horizontalScrollBar().sizeHint().height()
    lw.setFixedHeight(max(min(h, cap), 2 * lw.frameWidth() + lw.fontMetrics().height() + 8))


class _FitTree(QTreeWidget):
    """Change tree whose size hint is its content (capped), so the dialog can size itself to the plan."""
    CAP = 560

    def content_height(self) -> int:
        rows, it = 0, self.topLevelItem(0)
        rh = max(self.sizeHintForRow(0), self.fontMetrics().height() + 4) if it is not None else 0
        while it is not None:
            rows += 1
            it = self.itemBelow(it)
        wide = self.header().length() > self.viewport().width() > 0 or self.horizontalScrollBar().isVisible()
        sb = self.horizontalScrollBar().sizeHint().height() if wide else 0
        return self.header().sizeHint().height() + rows * rh + 2 * self.frameWidth() + sb + 2

    def sizeHint(self):
        sh = super().sizeHint()
        sh.setHeight(min(self.content_height(), self.CAP))
        return sh

    def minimumSizeHint(self):
        sh = super().minimumSizeHint()
        sh.setHeight(min(self.content_height(), 140))
        return sh


class ApplyDialog(QDialog):
    """Review a Plan (apply or undo preview).

    Apply: albums with problems are listed as held back ("Needs a look"); the clean ones apply. An album held
    only by track_mismatch offers "Apply anyway…": after confirm_mismatch() the dialog closes with OVERRIDE and
    `override_album` set, so the caller re-builds with allow_track_mismatch.
    Held and fetching albums get one greyed "stays in place" row in the change tree, never their track rows
    (a fetching album's target is built from stale metadata, so no After path is shown for it).
    Undo: file_changed etc. are listed as skipped files; only legacy_journal blocks, and "Confirm older-format
    undo…" closes with TRUST_LEGACY so the caller can confirm and re-plan with trust_legacy=True.
    The dialog sizes itself to the plan: a short plan gives a short dialog, not a tall empty tree."""
    TRUST_LEGACY = 2
    OVERRIDE = 3
    OPEN_NAMING = 4      # disc_folder_mismatch: the caller opens the naming settings, then builds again

    def __init__(self, plan, parent=None, undo=False, mock=False, trusted=False, album_name=None, album_row=None):
        super().__init__(parent)
        self.setWindowTitle("Review undo" if undo else "Review batch before applying")
        self.plan, self.undo, self.override_album = plan, undo, None
        self._name = album_name                    # library lookup by album_id (fallback only)
        self._row = album_row                      # library row by album_id (layout_ambiguous names)
        name = self.ap_name                        # AlbumPlan.artist/title first (e1eca5e), see P.album_label
        lay = QVBoxLayout(self)
        self.waiting_aps = []
        if undo:     # journal format is per operation: legacy_journal blocks the whole undo, never part of it
            self.held_aps, self.clean_aps = [], list(plan.albums)
            n_skip = getattr(plan, "skipped", 0)
            head = (f"<b>Undo restores {len(plan.albums)} album(s)</b> · {plan.files_to_move} files move back · "
                    f"{plan.tags_to_write} tags restored"
                    + (f" · {n_skip} step(s) skipped" if n_skip else "") +
                    ". Before = how files are now, After = what undo puts back. "
                    "Files changed since the apply are skipped and left alone.")
            if trusted:
                head += " <b>Older-format journal: you confirmed restoring its tags.</b>"
        else:
            self.held_aps = [ap for ap in plan.albums if P.held(ap)]
            self.waiting_aps = [ap for ap in plan.albums if P.waiting(ap)]
            self.clean_aps = [ap for ap in plan.albums if not ap.problems]
            head = (f"<b>{len(self.clean_aps)} of {len(plan.albums)} albums will apply</b> · "
                    f"{sum(len(a.files) for a in self.clean_aps)} files to move · "
                    f"{sum(len(f.tags) for a in self.clean_aps for f in a.files)} tag changes. "
                    "Every change is journaled and can be undone.")
        self.head_label = QLabel(head, wordWrap=True)
        lay.addWidget(self.head_label)
        # Plan.warnings, e.g. "folder.jpg will be saved from Discogs for 3 album(s)" (cover art is a warning,
        # not a FileChange, on the real backend). Apply: counted over the albums that apply now; the
        # "refused (held)" and "being fetched" lines are left to the held-back / fetching boxes below.
        self.warnings = (list(plan.warnings) if undo else
                         P.apply_warnings(plan, self.clean_aps, self.held_aps, self.waiting_aps))
        self.warnings_label = QLabel(wordWrap=True)
        if self.warnings:
            self.warnings_label.setText("<br>".join(f"ℹ {w}" for w in self.warnings))
            self.warnings_label.setStyleSheet("color:#555")
            lay.addWidget(self.warnings_label)
        else:
            self.warnings_label.hide()
        self.problems = P.all_problems(plan)
        self.legacy = [p for p in self.problems if P.kind_of(p) == P.LEGACY]
        self.blocking = self.legacy if undo else [p for ap in self.held_aps for p in ap.problems]
        # skipped steps stay in AlbumPlan.files with FileChange.skip_reason set
        self.skipped = [f for ap in plan.albums for f in ap.files if getattr(f, "skip_reason", None)]

        # held-back albums (apply) / blocked albums (undo)
        self.problem_label = QLabel(wordWrap=True)
        self.problem_list = QListWidget()
        shown = ([(ap, p) for ap in plan.albums for p in ap.problems if P.kind_of(p) == P.LEGACY] if undo else
                 [(ap, p) for ap in self.held_aps for p in ap.problems])
        if shown:
            self.problem_label.setText(
                "<b style='color:#a33'>This change was recorded in an older format; confirm before undo:</b>"
                if undo else f"<b style='color:#a33'>{len(self.held_aps)} album(s) held back for a look</b> "
                "(they stay where they are; the rest apply now):")
            for ap, p in shown:
                n = name(ap)       # "Black Flag: loose tracks … (held)" already has its colon
                text = f"⚠ {n}{' —' if P.LOOSE_TEXT in n else ':'} {P.label(p)}\n    {p.message}"
                if P.kind_of(p) == P.DISC_AMBIGUOUS:     # per file: why, and the disc/track choices
                    text += "".join(f"\n    {x}" for x in P.disc_conflict_lines(p, ap.folder_before))
                elif P.kind_of(p) == P.DISC_FOLDER_MISMATCH:   # "13 Mason Jar.flac → CD2/ (disc tag 1)"
                    text += "".join(f"\n    {x}" for x in P.folder_mismatch_lines(p, ap.folder_before))
                elif P.kind_of(p) == P.LAYOUT_AMBIGUOUS:   # loose files + the folders next to them
                    text += "".join(f"\n    {x}" for x in self.layout_lines(ap, p))
                else:
                    text += "".join(f"\n    {x}" for x in p.paths)
                it = QListWidgetItem(text)
                it.setToolTip("\n".join(map(str, p.paths)))
                it.setData(Qt.UserRole, ap.album_id); it.setData(Qt.UserRole + 1, P.kind_of(p))
                it.setForeground(QBrush(QColor("#a33")))
                self.problem_list.addItem(it)
            lay.addWidget(self.problem_label); lay.addWidget(self.problem_list)
            _fit_list_height(self.problem_list, 300)   # one disc conflict alone is ~9 lines
        else:
            self.problem_label.hide(); self.problem_list.hide()
        # releases still downloading / failed to download (apply): left out, status unchanged
        # RELEASE_NOT_FETCHED is not an error: a spinner and "fetching from Discogs…"; apply leaves those
        # albums out without holding them, and the next build picks them up.
        self.waiting_label = QLabel(wordWrap=True)
        self.waiting_spinner = QProgressBar(maximumHeight=8, textVisible=False)
        self.waiting_spinner.setRange(0, 0)               # indeterminate = busy spinner
        self.waiting_spinner.setFixedWidth(80)
        if self.waiting_aps:
            fetching = getattr(plan, "fetch_job", None) is not None
            lines = []
            for ap in self.waiting_aps:
                msg = "; ".join(p.message for p in ap.problems if P.kind_of(p) == P.NOT_FETCHED)
                lines.append(f"{name(ap)}" + ("" if fetching else f": {msg}"))
            self.waiting_label.setText(
                f"<b>{len(self.waiting_aps)} album(s): fetching from Discogs…</b> Left out of this apply "
                "and not held; build the plan again once they're in.<br>" + "<br>".join(lines))
            row = QHBoxLayout(); row.addWidget(self.waiting_spinner, 0, Qt.AlignTop)
            row.addWidget(self.waiting_label, 1)
            lay.addLayout(row)
            self.waiting_spinner.setVisible(fetching)
        else:
            self.waiting_label.hide(); self.waiting_spinner.hide()
        # skipped files (undo)
        self.skipped_list = QListWidget()
        if self.skipped:
            lay.addWidget(QLabel(f"<b style='color:#8a6d00'>{len(self.skipped)} file step(s) will be skipped "
                                 "and left as they are:</b>"))
            for f in self.skipped:
                it = QListWidgetItem(f"{f.path_before.name} — {f.skip_reason}")
                it.setToolTip(str(f.path_before)); it.setForeground(QBrush(QColor("#8a6d00")))
                self.skipped_list.addItem(it)
            lay.addWidget(self.skipped_list)
            _fit_list_height(self.skipped_list, 130)
        else:
            self.skipped_list.hide()

        self.tree = tree = _FitTree(); tree.setHeaderLabels(["Album / change", "Before", "After"])
        tree.header().setSectionResizeMode(QHeaderView.ResizeToContents)
        tree.setUniformRowHeights(True)
        moving = plan.albums if undo else self.clean_aps
        for ap in moving:
            self._add_album(tree, ap)
        if not undo:      # held and fetching albums stay where they are: one greyed row each, no track rows
            for ap in self.held_aps:
                self._add_still(tree, ap, "Held back", "stays in place (needs a look)",
                                "; ".join(P.one_line(p) for p in ap.problems)
                                + "".join(f"\n{x}" for p in ap.problems if P.kind_of(p) == P.DISC_AMBIGUOUS
                                          for x in P.disc_conflict_lines(p, ap.folder_before))
                                + "".join(f"\n{x}" for p in ap.problems if P.kind_of(p) == P.DISC_FOLDER_MISMATCH
                                          for x in P.folder_mismatch_lines(p, ap.folder_before))
                                + "".join(f"\n{x}" for p in ap.problems if P.kind_of(p) == P.LAYOUT_AMBIGUOUS
                                          for x in self.layout_lines(ap, p)))
            for ap in self.waiting_aps:
                self._add_still(tree, ap, "Fetching from Discogs…", "stays in place (not in this apply)",
                                "; ".join(p.message for p in ap.problems if P.kind_of(p) == P.NOT_FETCHED))
        lay.addWidget(tree, 1)
        bb = QDialogButtonBox(); bb.addButton("Cancel", QDialogButtonBox.RejectRole)
        self.trust_btn = self.anyway_btn = self.naming_btn = None
        if not undo and any(P.has_folder_mismatch(ap.problems) for ap in self.held_aps):
            self.naming_btn = bb.addButton("Change naming template…", QDialogButtonBox.ActionRole)
            self.naming_btn.setToolTip("The naming template puts some tracks into another disc's CD folder. "
                                       "Open the naming settings; the batch is checked again afterwards.")
            self.naming_btn.clicked.connect(lambda: self.done(self.OPEN_NAMING))
        if undo and self.legacy:
            self.trust_btn = bb.addButton("Confirm older-format undo…", QDialogButtonBox.ActionRole)
            self.trust_btn.clicked.connect(lambda: self.done(self.TRUST_LEGACY))
        if not undo and any(P.overridable(ap) for ap in self.held_aps):
            self.anyway_btn = bb.addButton("Apply anyway…", QDialogButtonBox.ActionRole)
            self.anyway_btn.setToolTip("Select one album held back only for a track-count mismatch")
            self.anyway_btn.clicked.connect(self.apply_anyway)
            self.problem_list.currentItemChanged.connect(lambda *_: self._sync_anyway())
            self._sync_anyway()
        ok = bb.addButton("Undo these changes" if undo else
                          ("Apply batch" if not self.held_aps and not self.waiting_aps
                           else f"Apply {len(self.clean_aps)} album(s)"),
                          QDialogButtonBox.AcceptRole)
        self.ok_btn = ok
        if not self.clean_aps or (undo and self.blocking):
            ok.setEnabled(False)
            ok.setToolTip("Every album needs a look first" if not undo else "Confirm the older-format undo first")
        elif mock:
            ok.setToolTip("Mock backend: nothing is written to disk")
        bb.rejected.connect(self.reject); bb.accepted.connect(self._accept_if_clear)
        lay.addWidget(bb)
        tree.itemExpanded.connect(lambda *_: self._fit())
        tree.itemCollapsed.connect(lambda *_: self._fit())
        self._fit(initial=True)

    # ---------------------------------------------------------------- change tree
    def ap_name(self, ap) -> str:
        return P.album_label(ap, self._name, undo=self.undo, row=self._row)

    def layout_lines(self, ap, p) -> list[str]:
        """Files involved in a layout_ambiguous album: the plan's audio, else the library row's tracks."""
        files = [f.path_before for f in ap.files if P.display_kind(f) == "track"]
        if not files and self._row is not None:
            r = self._row(ap.album_id)
            files = [t.path for t in getattr(r, "tracks", None) or []]
        return P.layout_files(p, ap.folder_before, files)

    def _add_album(self, tree, ap):
        top = QTreeWidgetItem([self.ap_name(ap), str(ap.folder_before), str(ap.folder_after)])
        top.setData(0, Qt.UserRole, ap.album_id)
        top.setToolTip(0, f"{self.ap_name(ap)}\n{P.release_note(ap)}")
        top.setToolTip(1, str(ap.folder_before)); top.setToolTip(2, str(ap.folder_after))
        f0 = top.font(0); f0.setBold(True)
        for c in range(3):
            top.setFont(c, f0)
        tree.addTopLevelItem(top)
        for f in ap.files:
            skip = getattr(f, "skip_reason", None)
            after_full = str(f.path_after)
            if self.undo and "/Trash/files/" in after_full:   # undo of an apply-created file (folder.jpg)
                after = f"Trash (≈ {f.path_after.name})"
            else:
                after = _rel_to(f.path_after, ap.folder_after)
            kind = P.display_kind(f)
            fi = QTreeWidgetItem([f"{kind} (skipped: {skip})" if skip else kind, f.path_before.name, after])
            fi.setToolTip(1, str(f.path_before)); fi.setToolTip(2, after_full)
            if skip:
                for c in range(3):
                    fi.setForeground(c, QBrush(QColor("#8a6d00")))
            top.addChild(fi)
            for t in f.tags:
                fi.addChild(QTreeWidgetItem([t.key, t.before or "(none)", t.after or "(removed)"]))
        for p in ap.problems:
            if self.undo and P.kind_of(p) != P.LEGACY:
                continue                       # shown as skipped steps instead
            top.addChild(QTreeWidgetItem(["⚠ " + P.label(p), p.message, ", ".join(map(str, p.paths))]))
        top.setExpanded(True)

    def _add_still(self, tree, ap, what, after, tip):
        """One greyed row for an album that doesn't move in this apply (no track rows, no After path)."""
        it = QTreeWidgetItem([f"{what}: {self.ap_name(ap)}", str(ap.folder_before), after])
        it.setData(0, Qt.UserRole, ap.album_id)
        it.setChildIndicatorPolicy(QTreeWidgetItem.DontShowIndicator)
        font = it.font(0); font.setItalic(True)
        for c in range(3):
            it.setForeground(c, QBrush(QColor(GREY))); it.setFont(c, font)
            it.setToolTip(c, f"{self.ap_name(ap)} · {P.release_note(ap)}\n{ap.folder_before}\n{tip}")
        tree.addTopLevelItem(it)

    def _fit(self, initial=False):
        """Height follows the content: a short plan gives a short dialog. The dialog can't be stretched past
        what it shows (no tall empty tree); a long plan keeps a scrolling tree up to the screen height."""
        QWIDGETSIZE_MAX = 16777215
        self.tree.updateGeometry()
        self.setMaximumHeight(QWIDGETSIZE_MAX)
        lay = self.layout(); lay.invalidate(); lay.activate()
        # word-wrapped labels: ask for the height at the dialog's real width, not at the labels' narrow hint
        width = self.width() if self.width() > 200 else 1000
        want = lay.totalHeightForWidth(width) if lay.hasHeightForWidth() else lay.totalSizeHint().height()
        screen = self.screen().availableGeometry().height() if self.screen() else 1000
        if self.tree.content_height() <= _FitTree.CAP:
            self.setMaximumHeight(min(max(want, self.minimumSizeHint().height()), screen))
        if initial:
            self.resize(1000, min(want, screen, 760))
        elif self.height() < min(want, screen):
            self.resize(self.width(), min(want, screen))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if e.size().width() != e.oldSize().width() and not getattr(self, "_fitting", False):
            QTimer.singleShot(0, self._refit)          # wrapped labels change height with the width

    def _refit(self):
        self._fitting = True
        try:
            self._fit()
        finally:
            self._fitting = False

    def _ap(self, album_id):
        return next((ap for ap in self.plan.albums if ap.album_id == album_id), None)

    def selected_album(self):
        it = self.problem_list.currentItem()
        return it.data(Qt.UserRole) if it else None

    def _sync_anyway(self):
        ap = self._ap(self.selected_album())
        self.anyway_btn.setEnabled(bool(ap and P.overridable(ap)))
        why = P.blocking_reasons(ap) if ap else []
        self.anyway_btn.setToolTip(f"Can't apply anyway: {', '.join(why)}" if why else
                                   "Select one album held back only for a track-count mismatch")

    def confirm_mismatch(self, ap) -> bool:
        """Per-album confirm naming the mismatch (patched in tests)."""
        msg = "; ".join(p.message for p in ap.problems)
        return QMessageBox.question(
            self, "Apply anyway?",
            f"{self.ap_name(ap)}: {msg}.\n\nApply this album anyway? Unmatched files stay in place, "
            "untagged.", QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes

    def apply_anyway(self):
        ap = self._ap(self.selected_album())
        if ap and P.overridable(ap) and self.confirm_mismatch(ap):
            self.override_album = ap.album_id
            self.done(self.OVERRIDE)

    def _accept_if_clear(self):
        if self.clean_aps and not (self.undo and self.blocking):
            self.accept()
