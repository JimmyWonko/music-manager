"""Match window (v1 #1 of PICARD-UI-PLAN.md): clusters and unmatched files on the left, the album-match view
(match.view) on the right. Read-only except Adopt (library.adopt_cluster, then match.find). No drag yet.

Built against c334bbf. Not used yet, because they are not in c334bbf: paged clusters, match.pin/unpin,
cluster signals, pinned pairs in the journal (and the per-pair `method` field)."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QMainWindow, QProgressBar, QPushButton, QSplitter,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from . import problems as P
from ._meta import DISPLAY_NAME
from .backend import as_backend

KEY = Qt.UserRole          # ("cluster", id) / ("file", cluster id) on the left
FETCHING = "Fetching from Discogs…"


def _dur(s) -> str:
    return "" if s is None else f"{int(s) // 60}:{int(s) % 60:02d}"


def _sim(x) -> str:
    return "" if x is None else f"{x:.0%}"


class MatchWindow(QMainWindow):
    def __init__(self, svc=None, parent=None):
        super().__init__(parent)
        self.svc = svc = as_backend(svc)
        self.setWindowTitle(f"{DISPLAY_NAME} — Match"); self.resize(1280, 800)
        self.clusters: dict[str, object] = {}
        self.current: str | None = None       # selected cluster id (rows are keyed by id, not position)
        self.view = None

        self.left = QTreeWidget(); self.left.setHeaderLabels(["Clusters and unmatched files", "Files"])
        self.left.setColumnWidth(0, 330)
        self.left.itemSelectionChanged.connect(self._on_select)
        self.adopt_btn = QPushButton("Adopt as album"); self.adopt_btn.setEnabled(False)
        self.adopt_btn.setToolTip("Index this cluster as its own album (nothing moves), then search Discogs")
        self.adopt_btn.clicked.connect(self.adopt)
        lw = QWidget(); ll = QVBoxLayout(lw); ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(self.left, 1); ll.addWidget(self.adopt_btn)

        self.header = QLabel(wordWrap=True); self.header.setTextFormat(Qt.RichText)
        self.spinner = QProgressBar(maximumHeight=8, textVisible=False); self.spinner.setRange(0, 0)
        self.spinner.setFixedWidth(80)
        self.fetch_label = QLabel(FETCHING)
        fr = QHBoxLayout(); fr.addWidget(self.spinner); fr.addWidget(self.fetch_label); fr.addStretch(1)
        self.tracks = QTreeWidget()
        self.tracks.setHeaderLabels(["#", "Release track", "Length", "Local file", "Length", "Similarity", "OK"])
        for c, w in ((0, 60), (1, 260), (2, 60), (3, 300), (4, 60), (5, 80)):
            self.tracks.setColumnWidth(c, w)
        self.problems = QLabel(wordWrap=True); self.problems.setStyleSheet("color:#a33")
        rw = QWidget(); rl = QVBoxLayout(rw); rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.header); rl.addLayout(fr); rl.addWidget(self.tracks, 1); rl.addWidget(self.problems)

        split = QSplitter(); split.addWidget(lw); split.addWidget(rw); split.setSizes([420, 860])
        self.setCentralWidget(split)
        ev = svc.events
        ev.album_changed.connect(self._on_album_changed)
        ev.job_finished.connect(lambda job: self.refresh())
        self.refresh()

    # ---------------------------------------------------------------- left pane
    def refresh(self):
        """Reload clusters, keeping the selection by cluster id."""
        cs = self.svc.library.clusters()
        self.clusters = {c.id: c for c in cs}
        self.left.blockSignals(True)
        self.left.clear()
        groups = QTreeWidgetItem(self.left, ["Clusters"]); loose = QTreeWidgetItem(self.left, ["Unmatched files"])
        for top in (groups, loose):
            top.setFlags(top.flags() & ~Qt.ItemIsSelectable)
        keep = None
        for c in cs:
            # A one-file cluster has nothing to group with: Picard's "unclustered files". Adopted ones
            # are albums now and sit with the clusters, badged.
            single = len(c.files) == 1 and c.album_id is None
            if single:
                it = QTreeWidgetItem(loose, [Path(c.files[0].path).name, ""])
            else:
                it = QTreeWidgetItem(groups, [self.cluster_label(c), str(len(c.files))])
                for f in c.files:
                    ch = QTreeWidgetItem(it, [Path(f.path).name, ""])
                    ch.setData(0, KEY, ("file", c.id)); ch.setToolTip(0, str(f.path))
            it.setData(0, KEY, ("cluster", c.id))
            it.setToolTip(0, f"{c.folder}\n{c.id}")
            if c.album_id is not None:
                it.setForeground(0, QBrush(QColor("#2a6")))
            if c.id == self.current:
                keep = it
        groups.setText(1, str(groups.childCount())); loose.setText(1, str(loose.childCount()))
        groups.setExpanded(True); loose.setExpanded(True)
        self.left.blockSignals(False)
        if keep is not None:
            keep.setSelected(True); self.left.setCurrentItem(keep)
        self.show_view()

    @staticmethod
    def cluster_label(c) -> str:
        discs = f" · {c.disc_count} discs" if c.disc_count > 1 else ""
        adopted = f"  [{P.CLUSTER_BADGE}]" if c.album_id is not None else ""
        return f"{c.artist} – {c.title}{discs}{adopted}"

    def _on_select(self):
        items = self.left.selectedItems()
        key = items[0].data(0, KEY) if items else None
        self.current = key[1] if key else None
        self.show_view()

    def item_for(self, cluster_id: str):
        for it in self._walk(self.left.invisibleRootItem()):
            k = it.data(0, KEY)
            if k and k[0] == "cluster" and k[1] == cluster_id:
                return it
        return None

    def _walk(self, item):
        for i in range(item.childCount()):
            ch = item.child(i); yield ch; yield from self._walk(ch)

    def select(self, cluster_id: str):
        it = self.item_for(cluster_id)
        if it is not None:
            self.left.setCurrentItem(it)

    # ---------------------------------------------------------------- right pane
    def show_view(self):
        c = self.clusters.get(self.current) if self.current else None
        self.adopt_btn.setEnabled(c is not None and c.album_id is None)
        self.tracks.clear(); self.problems.clear()
        if c is None:
            self.view = None
            self.header.setText("<i>Select a cluster or a file.</i>")
            self.spinner.hide(); self.fetch_label.hide()
            return
        v = self.view = self.svc.match.view(c.id)
        cand = next((x for x in v.candidates if x.release_id == v.release_id), None)
        if v.album_id is None:
            sub = "Not adopted yet: <b>Adopt as album</b> to search Discogs for it. Nothing moves."
        elif cand is None:
            sub = f"Album #{v.album_id} [{P.CLUSTER_BADGE}] · no Discogs candidates yet"
        else:
            sub = (f"Album #{v.album_id} [{P.CLUSTER_BADGE}] · Discogs: {cand.artist} – {cand.title}"
                   f" ({cand.year}, {cand.catno}) · score {cand.score:.0%} · {len(v.candidates)} candidate(s)")
        self.header.setText(f"<b>{c.artist} – {c.title}</b> · {len(c.files)} file(s) in {c.folder}<br>{sub}")
        waiting = v.album_id is not None and cand is not None and not v.fetched
        self.spinner.setVisible(waiting); self.fetch_label.setVisible(waiting)
        if v.fetched:
            rel = QTreeWidgetItem(self.tracks, ["", f"Release tracks ({sum(x.remote is not None for x in v.pairs)})"])
            for pr in v.pairs:
                if pr.remote is None:
                    continue
                lt, rt = pr.local, pr.remote
                it = QTreeWidgetItem(rel, [rt.position, rt.title, _dur(rt.duration_s),
                                           Path(lt.path).name if lt else "— no file", _dur(lt.duration_s) if lt else "",
                                           _sim(pr.title_similarity) if lt else "", ("✓" if pr.ok else "✗") if lt else ""])
                if lt is None or not pr.ok:
                    for col in range(7):
                        it.setForeground(col, QBrush(QColor("#a33")))
                if lt is not None:
                    it.setToolTip(3, str(lt.path))
            rel.setExpanded(True)
        if v.unmatched_files:
            un = QTreeWidgetItem(self.tracks, ["", f"Unmatched files ({len(v.unmatched_files)})"])
            for lt in v.unmatched_files:
                it = QTreeWidgetItem(un, ["", "", "", Path(lt.path).name, _dur(lt.duration_s), "", ""])
                it.setToolTip(3, str(lt.path))
            un.setExpanded(True)
        # MatchView.unmatched_tracks are the "— no file" rows above; say how many in the header row
        if v.fetched and v.unmatched_tracks:
            rel.setText(1, f"Release tracks ({sum(x.remote is not None for x in v.pairs)}) · {len(v.unmatched_tracks)} without a file")
        if v.problems:
            self.problems.setText("<br>".join(f"⚠ {P.label(p)}: {p.message}" for p in v.problems))

    def _on_album_changed(self, album_id: int):
        c = self.clusters.get(self.current) if self.current else None
        if c is not None and c.album_id == album_id:
            self.show_view()       # e.g. the release finished fetching: fetched False -> True
        elif any(x.album_id == album_id for x in self.clusters.values()) or c is None or c.album_id is None:
            self.refresh()

    # ---------------------------------------------------------------- actions
    def adopt(self):
        c = self.clusters.get(self.current) if self.current else None
        if c is None or c.album_id is not None:
            return
        row = self.svc.library.adopt_cluster(c.id)
        self.last_find_job = self.svc.match.find([row.id])
        self.refresh()
        self.select(c.id)
