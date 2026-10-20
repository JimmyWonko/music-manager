"""Library list: every album in the index, lazily paged, with status filter, counts, sort and search.

Pages come straight from the contract's library.albums(filter, sort, offset, limit, descending);
counts from library.status_counts(filter). Clicking a sortable column header maps it to an AlbumSort
key; clicking it again (or the Descending toggle) flips `descending`.
On the mock backend fake_contract.py pads the list with 100k
synthetic albums to prove lazy paging stays fast.
"""
from __future__ import annotations

import time

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QTimer
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QPushButton, QStyledItemDelegate,
    QTableView, QToolButton, QVBoxLayout, QWidget,
)

from ._meta import DISPLAY_NAME
from .backend import Backend, as_backend
from . import problems as P
from .review import STATUS_COLOR, LayoutIndex

# (header, AlbumSort value or None = not sortable)
COLUMNS = [("Artist", "artist"), ("Album", "title"), ("Year", "year"), ("Cat. no.", None), ("Tracks", None),
           ("Status", "status"), ("Confidence", "confidence"), ("Added", "added"), ("Folder", "folder")]
COLS = [c for c, _ in COLUMNS]
STATUS_COL, CONF_COL, ADDED_COL, FOLDER_COL = 5, 6, 7, 8
MIN_VISIBLE_ROWS = 6       # the table never shrinks below this many rows
# label -> MatchStatus names (None = all)
STATUS_FILTERS = {"All albums": None, "Not matched yet": {"UNMATCHED"}, "Needs review": {"NEEDS_REVIEW"},
                  "Queued for matching": {"QUEUED"}, "Matched": {"MATCHED"}, "Accepted": {"ACCEPTED"},
                  "Needs a look": {"HELD"}, "Applied": {"APPLIED"},
                  "No match": {"NO_MATCH"}, "Skipped": {"SKIPPED"}}


class LibraryModel(QAbstractTableModel):
    PAGE = 500

    def __init__(self, svc: Backend):
        super().__init__()
        self.svc = svc
        self.filter, self.rows, self.total = svc.album_filter(), [], 0
        self.sort_col, self.sort_order = 0, Qt.AscendingOrder
        self.layout = LayoutIndex()

    def sort_key(self):
        return self.svc.sort(COLUMNS[self.sort_col][1])

    def remove_album(self, album_id) -> bool:
        """album_removed (1f7b340): drop the row."""
        for i, r in enumerate(self.rows):
            if r.id == album_id:
                self.beginRemoveRows(QModelIndex(), i, i)
                del self.rows[i]; self.total = max(0, self.total - 1)
                self.endRemoveRows()
                return True
        return False

    @property
    def descending(self) -> bool:
        return self.sort_order == Qt.DescendingOrder

    def _page(self, offset):
        page = self.svc.library.albums(self.filter, self.sort_key(), offset, self.PAGE, descending=self.descending)
        return list(page.items), page.total

    def set_filter(self, f):
        self.beginResetModel()
        self.filter = f
        self.rows, self.total = self._page(0)
        self.layout = LayoutIndex(self.svc)      # folders holding loose tracks (for the tooltip of albums below)
        self.layout.add(self.rows)
        self.endResetModel()

    def sort(self, column, order=Qt.AscendingOrder):
        """QTableView calls this on header clicks. Non-sortable columns are ignored."""
        if COLUMNS[column][1] is None:
            return False
        self.sort_col, self.sort_order = column, order
        self.set_filter(self.filter)
        return True

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(COLS)

    def headerData(self, s, o, role=Qt.DisplayRole):
        if o == Qt.Horizontal and role == Qt.DisplayRole:
            return COLS[s]
        if o == Qt.Horizontal and role == Qt.ToolTipRole:
            return "Click to sort" if COLUMNS[s][1] else "Not sortable (no sort key in the backend API)"

    def canFetchMore(self, parent):
        return len(self.rows) < self.total

    def fetchMore(self, parent):
        more, _ = self._page(len(self.rows))
        if not more:
            return
        self.beginInsertRows(QModelIndex(), len(self.rows), len(self.rows) + len(more) - 1)
        self.rows += more
        self.layout.add(more)
        self.endInsertRows()

    @staticmethod
    def _tuple(r):
        return (r.artist, r.title, r.year, r.catno, r.track_count, r.status, r.confidence, r.added_at, str(r.folder))

    def data(self, idx, role=Qt.DisplayRole):
        r = self.rows[idx.row()]
        t = self._tuple(r)
        c = idx.column()
        if c in (0, 1) and role in (Qt.DisplayRole, Qt.ToolTipRole):
            artist, title = self.layout.names(r)
            if role == Qt.DisplayRole:
                return artist if c == 0 else title + P.badge(r)
            tip = self.layout.tooltip(r)
            return tip.lstrip("\n") or None
        if role == Qt.DisplayRole:
            if c == STATUS_COL:
                return t[c].name.replace("_", " ").capitalize()
            if c == CONF_COL:
                return "" if t[c] is None else f"{t[c]:.0%}"
            if c == ADDED_COL:
                return t[c].astimezone().strftime("%Y-%m-%d %H:%M")     # UTC from the backend, shown local
            return str(t[c])
        if role == Qt.ToolTipRole and c == FOLDER_COL:
            return t[c]                                                  # full path; the cell elides the middle
        if role == Qt.ForegroundRole and c == STATUS_COL:
            return QBrush(QColor(STATUS_COLOR.get(t[STATUS_COL].name, "#555")))
        if role == Qt.TextAlignmentRole and c in (2, 4, CONF_COL):
            return int(Qt.AlignRight | Qt.AlignVCenter)


class MiddleElideDelegate(QStyledItemDelegate):
    """Long folder paths keep their start (library root) and end (album folder): '/music/…/[1998] Late Set'."""

    MARGIN = 10      # the style's text margins on both sides of the cell

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        # elide here, at the cell's text width: leaving it to the style elides a second time at the right end
        option.text = option.fontMetrics.elidedText(option.text, Qt.ElideMiddle,
                                                    max(option.rect.width() - self.MARGIN, 10))
        option.textElideMode = Qt.ElideNone


class LibraryWindow(QMainWindow):
    def __init__(self, svc: Backend | None = None):
        super().__init__()
        self.svc = svc = as_backend(svc)
        self.setWindowTitle(f"{DISPLAY_NAME} — Library"); self.resize(1300, 720)
        self.search = QLineEdit(placeholderText="Search artist or album…")
        self.status = QComboBox()
        self.status.addItems(STATUS_FILTERS)          # item text gets "(count)" appended
        self.match_btn = QPushButton("Find Discogs matches for selected")
        self.review_btn = QPushButton("Open review queue")
        self.clusters_btn = QPushButton("Clusters & matching")
        self.clusters_btn.setToolTip("Loose files grouped by album tag, and how they line up with a Discogs release")
        self.desc_btn = QToolButton(text="Descending", checkable=True)
        self.desc_btn.setToolTip("Reverse the sort column (ties stay in folder order)")
        top = QHBoxLayout(); top.addWidget(self.search, 1); top.addWidget(self.status); top.addWidget(self.desc_btn)
        top.addWidget(self.match_btn); top.addWidget(self.review_btn); top.addWidget(self.clusters_btn)
        self.model = LibraryModel(svc)
        self.view = QTableView(); self.view.setModel(self.model)
        self.view.setSelectionBehavior(QTableView.SelectRows)
        self.view.verticalHeader().hide(); self.view.verticalHeader().setDefaultSectionSize(22)
        h = self.view.horizontalHeader()
        for c in range(len(COLS)):
            h.setSectionResizeMode(c, QHeaderView.Interactive)
        h.setSectionResizeMode(FOLDER_COL, QHeaderView.Stretch)      # paths are the long column; elided in the middle
        for c, w in ((0, 200), (1, 220), (2, 60), (3, 100), (4, 60), (5, 100), (6, 90), (7, 130)):
            self.view.setColumnWidth(c, w)
        self.folder_delegate = MiddleElideDelegate(self.view)
        self.view.setItemDelegateForColumn(FOLDER_COL, self.folder_delegate)
        h.setSortIndicatorShown(True); h.setSectionsClickable(True)
        h.setSortIndicator(0, Qt.AscendingOrder)
        h.sortIndicatorChanged.connect(self.on_sort)
        self.desc_btn.toggled.connect(self.on_desc_toggled)
        self.info = QLabel(); self.info.setStyleSheet("color:#555")
        self.info.setToolTip(svc.label_detail)
        self.counts = QLabel(); self.counts.setStyleSheet("color:#555")
        w = QWidget(); lay = QVBoxLayout(w)
        # the table shrinks to its rows when there are few and the spare height goes below it; counts and
        # the short backend label sit in the status bar
        lay.addLayout(top); lay.addWidget(self.view, 1); lay.addStretch(0)
        self.setCentralWidget(w)
        self.counts.setContentsMargins(0, 0, 16, 0)
        self.statusBar().addWidget(self.counts)
        self.statusBar().addWidget(self.info, 1)
        self.statusBar().setSizeGripEnabled(False)
        self.model.modelReset.connect(self.fit_rows); self.model.rowsInserted.connect(self.fit_rows)
        self.search.textChanged.connect(self.reload); self.status.currentIndexChanged.connect(self.reload)
        self.review_btn.clicked.connect(self.open_review)
        self.clusters_btn.clicked.connect(self.open_match)
        self.match_btn.clicked.connect(self.find_selected)
        svc.events.job_finished.connect(self.on_job_finished)
        self.hint = QLabel(wordWrap=True); self.hint.setStyleSheet("color:#8a6d00")
        self.root_btn = QPushButton("Choose library folder…"); self.root_btn.clicked.connect(lambda: self.choose_library_root())
        hint_row = QHBoxLayout(); hint_row.addWidget(self.hint, 1); hint_row.addWidget(self.root_btn)
        lay.insertLayout(1, hint_row)
        svc.events.album_changed.connect(lambda _id: self.update_counts())
        if getattr(svc.events, "album_removed", None) is not None:
            svc.events.album_removed.connect(lambda aid: (self.model.remove_album(aid), self.update_counts()))
        self.reload()
        self.fit_rows()

    def current_status_names(self):
        return list(STATUS_FILTERS.values())[max(0, self.status.currentIndex())]

    def select_status(self, label: str):
        self.status.setCurrentIndex(self.status.findText(label, Qt.MatchStartsWith))

    def on_sort(self, column, order):
        if not self.model.sort(column, order):     # not sortable: put the indicator back
            h = self.view.horizontalHeader()
            h.blockSignals(True); h.setSortIndicator(self.model.sort_col, self.model.sort_order); h.blockSignals(False)
            return
        self.desc_btn.blockSignals(True); self.desc_btn.setChecked(self.model.descending)
        self.desc_btn.blockSignals(False)
        self.update_info()

    def on_desc_toggled(self, on):
        self.view.horizontalHeader().setSortIndicator(self.model.sort_col,
                                                      Qt.DescendingOrder if on else Qt.AscendingOrder)

    def update_counts(self):
        """One status_counts() call; the filter's status is ignored by the backend, search text isn't."""
        c = {s.name: n for s, n in self.svc.library.status_counts(self.svc.album_filter(None, self.search.text())).items()}
        for i, (label, names) in enumerate(STATUS_FILTERS.items()):
            n = sum(c.values()) if names is None else sum(c.get(x, 0) for x in names)
            self.status.setItemText(i, f"{label} ({n:,})")
        self.counts.setText(P.status_summary(c))
        self.update_hint(c)

    def on_job_finished(self, job):
        if job.kind.name in ("SCAN", "MATCH"):
            self.reload()
        # c2fcf25: library.scan doesn't regroup loose files; run the cluster scan after it
        if (job.kind.name == "SCAN" and job.state.name == "DONE"
                and getattr(self.svc.library, "scan_clusters", None) is not None):
            self.last_cluster_job = self.svc.library.scan_clusters()

    def selected_ids(self) -> list[int]:
        return [self.model.rows[i.row()].id for i in self.view.selectionModel().selectedRows()
                if i.row() < len(self.model.rows)]

    def find_selected(self):
        """match.find on the selection (or on every QUEUED album when nothing is selected: the real service
        doesn't resume albums left QUEUED by a restart, API-DEVIATIONS #8)."""
        ids = self.selected_ids() or [a.id for a in self.svc.albums({"QUEUED"}, limit=10**6).items]
        if not ids:
            return
        try:
            self.last_find_job = self.svc.match.find(ids)
        except Exception as e:      # ConfigError (no Discogs token / library root), NotFound
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "Can't search Discogs", str(e))

    def update_hint(self, counts: dict):
        hints = []
        try:
            roots = self.svc.settings.get().library_roots
        except Exception:
            roots = []
        if not roots:   # real service: no built-in library path (API-DEVIATIONS #14)
            hints.append("No library folder is set yet. Choose one to scan it.")
        self.root_btn.setVisible(not roots)
        if counts.get("QUEUED"):
            hints.append(f"{counts['QUEUED']:,} album(s) are still queued for matching. Matching doesn't resume "
                         "by itself after a restart: click “Find Discogs matches” with nothing selected to resume.")
        self.hint.setText("  ".join(hints)); self.hint.setVisible(bool(hints))

    def choose_library_root(self, path=None):
        """settings.set(library_roots=[folder]) then library.scan() (a Job; nothing is written to the music)."""
        from pathlib import Path
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        if path is None:
            path = QFileDialog.getExistingDirectory(self, "Choose your music library folder")
            if not path:
                return
        try:
            self.svc.settings.set(library_roots=[Path(path)])
            self.last_scan_job = self.svc.library.scan()
        except Exception as e:
            QMessageBox.warning(self, "Can't use that folder", str(e))
            return
        self.update_counts()

    def reload(self, *_):
        t = time.perf_counter()
        self.model.set_filter(self.svc.album_filter(self.current_status_names(), self.search.text()))
        self._ms = (time.perf_counter() - t) * 1000
        self.update_counts(); self.update_info()

    def update_info(self):
        arrow = " ↓" if self.model.descending else ""
        self.info.setText(f"{self.model.total:,} albums · sorted by {self.model.sort_key().value}{arrow} · showing first "
                          f"{len(self.model.rows):,} (more load as you scroll) · loaded in {self._ms:.0f} ms · "
                          f"{self.svc.label}")

    def fit_rows(self, *_):
        """Few rows: the table is only as tall as them (min MIN_VISIBLE_ROWS), no big white box. Many rows:
        no cap, it fills the window and pages in more as you scroll."""
        v = self.view
        n = max(len(self.model.rows), MIN_VISIBLE_ROWS)
        h = (v.horizontalHeader().sizeHint().height() + n * v.verticalHeader().defaultSectionSize()
             + 2 * v.frameWidth() + (v.horizontalScrollBar().sizeHint().height()
                                     if v.horizontalScrollBar().isVisible() else 0))
        v.setMaximumHeight(h if not self.model.canFetchMore(QModelIndex()) else 16777215)

    def open_match(self):
        from .match import MatchWindow
        self._match = MatchWindow(self.svc); self._match.show()

    def open_review(self):
        from .review import ReviewWindow
        self._review = ReviewWindow(self.svc); self._review.show()
