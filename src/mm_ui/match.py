"""Match window (PICARD-UI-PLAN.md v1 #1 and #2), built on feat/clustering c2fcf25.

Left: match.clusters (paged ClusterRow): folder albums, tag clusters and unclustered files, told apart by
`kind`, keyed by row id. Right: match.view of the selected row: release tracks with their files, similarity,
ok, manual pairs, unmatched files and tracks, problems.
Actions: Adopt / Un-adopt (library.adopt_cluster / unadopt_cluster), Scan clusters (library.scan_clusters),
Pair… (keyboard: Ctrl+P), Unpin, and drag:
- a file onto a release track pins it there (match.pin); onto a track that already has a file, swaps them;
- a file onto "Unclustered files" unpins it, or (not pinned) moves it out of its cluster (move_to_cluster None);
- a loose file onto another cluster of its folder moves it there.
A pin refused with PinConflict.reason "override_hold_required" asks first and is retried with override_hold=True;
every other reason (disc_folder_mismatch included) is explained and the pin is undone.

Discogs sub-tracks (contract "Subtracks (2026-10-21)", MatchView.subtrack_groups): a group (2-9a + 2-9b) is ONE
grouped row: "2-9a + 2-9b", the joined title "A / B", "lengths unknown" when Discogs lists none. Merged
automatically it carries the badge "merged 9a + 9b" and can be split (match.split_subtracks; Undo split is
match.unsplit_subtracks); not merged, its tracks are listed under it and nothing is guessed: dropping a file on
the group row pins it to the whole group (match.pin(positions=[...])), which shows as manual. The "Merge Discogs
sub-tracks automatically…" box is Settings.merge_subtracks."""
from __future__ import annotations

import html
import re
from pathlib import Path

from PySide6.QtCore import QMimeData, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QHBoxLayout, QInputDialog, QLabel, QMainWindow, QMessageBox,
                               QProgressBar, QPushButton, QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget)

from . import problems as P
from ._meta import DISPLAY_NAME
from .backend import as_backend

KEY = Qt.UserRole            # left: ("row", row id) | ("file", row id, path) | ("group", kind)
TRACK = Qt.UserRole          # right: release-track position ("" for non-track rows)
FILE = Qt.UserRole + 1       # right: the file's path (str) when the row has one
GROUP = Qt.UserRole + 2      # right: a subtrack group row's base position ("2-9")
MIME = "application/x-mm-file"
FETCHING = "Fetching from Discogs…"
MANUAL = "manual"
PAGE = 200
MERGE_LABEL = "Merge Discogs sub-tracks automatically when lengths confirm it"
# SubtrackGroup.reason -> why the group is (not) one track, for the row tooltip and the note under the tracks
SUBTRACK_REASONS = {
    "pinned": "you pinned one file to the whole group",
    "durations_fit": "merged automatically: the file is as long as the sub-tracks together",
    "not_needed": "not merged: the disc's file and track counts already match",
    "ambiguous": "not merged: merging wouldn't make the disc's file and track counts equal",
    "durations_unknown": "not merged: Discogs lists no lengths, so it can't be confirmed",
    "durations_differ": "not merged: the file's length doesn't match the sub-tracks together",
    "still_held": "not merged: the disc would still be held",
    "split": "split by you: kept as separate tracks",
    "merge_off": "not merged: automatic merging is off in settings",
}
GROUPS = (("folder", "Albums"), ("tag_cluster", "Clusters"), ("unclustered", "Unclustered files"))


def _dur(s) -> str:
    return "" if s is None else f"{int(s) // 60}:{int(s) % 60:02d}"


def _short(position: str) -> str:
    """ "2-9a" -> "9a" (the disc prefix is the same for a whole group)."""
    return position.split("-", 1)[1] if "-" in position else position


def _sim(x) -> str:
    return "" if x is None else f"{x:.0%}"



def _mime(path) -> QMimeData:
    md = QMimeData(); md.setData(MIME, str(path).encode()); md.setText(str(path))
    return md


def _dropped(event) -> str | None:
    md = event.mimeData()
    return bytes(md.data(MIME)).decode() if md.hasFormat(MIME) else None


class _Tree(QTreeWidget):
    """A tree whose file rows drag their path and whose drop targets call back the window."""

    def __init__(self, path_of, on_drop, target_of):
        super().__init__()
        self._path_of, self._on_drop, self._target_of = path_of, on_drop, target_of
        self.setDragEnabled(True); self.setAcceptDrops(True); self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDrop); self.setDefaultDropAction(Qt.MoveAction)

    def mimeData(self, items):
        path = self._path_of(items[0]) if items else None
        return _mime(path) if path else QMimeData()

    def mimeTypes(self):
        return [MIME]

    def dragEnterEvent(self, e):
        e.acceptProposedAction() if _dropped(e) else e.ignore()

    def dragMoveEvent(self, e):
        it = self.itemAt(e.position().toPoint())
        e.acceptProposedAction() if _dropped(e) and it is not None else e.ignore()

    def dropEvent(self, e):
        path, it = _dropped(e), self.itemAt(e.position().toPoint())
        e.setDropAction(Qt.CopyAction); e.accept()      # the window changes the index; never let Qt move rows
        if path and it is not None:       # read the target now: the rows are rebuilt after the drop
            target = self._target_of(it)
            QTimer.singleShot(0, lambda: self._on_drop(path, target))


class MatchWindow(QMainWindow):
    def __init__(self, svc=None, parent=None):
        super().__init__(parent)
        self.svc = svc = as_backend(svc)
        self.setWindowTitle(f"{DISPLAY_NAME} — Match"); self.resize(1280, 800)
        self.rows: dict[str, object] = {}     # ClusterRow by id
        self.current: str | None = None       # selected row id
        self.view = None
        self.split_bases: set[str] = set()    # match.subtrack_splits of the album shown
        # tests replace these two (no modal dialogs in tests)
        self.confirm = lambda title, text: QMessageBox.question(self, title, text) == QMessageBox.Yes
        self.inform = lambda title, text: QMessageBox.information(self, title, text)

        self.left = _Tree(self._left_path, self.drop_left, lambda it: it.data(0, KEY))
        self.left.setHeaderLabels(["Albums, clusters and unclustered files", "Files"])
        self.left.setColumnWidth(0, 360)
        self.left.itemSelectionChanged.connect(self._on_select)
        self.adopt_btn = QPushButton("Adopt as album"); self.adopt_btn.clicked.connect(self.adopt)
        self.adopt_btn.setToolTip("Index this cluster as its own album (nothing moves), then search Discogs")
        self.unadopt_btn = QPushButton("Un-adopt"); self.unadopt_btn.clicked.connect(self.unadopt)
        self.unadopt_btn.setToolTip("Turn this album back into a cluster: its candidates, choice and manual pairs "
                                    "are dropped. Nothing moves.")
        self.scan_btn = QPushButton("Scan clusters"); self.scan_btn.clicked.connect(self.scan_clusters)
        lw = QWidget(); ll = QVBoxLayout(lw); ll.setContentsMargins(0, 0, 0, 0); ll.addWidget(self.left, 1)
        lb = QHBoxLayout(); [lb.addWidget(b) for b in (self.adopt_btn, self.unadopt_btn, self.scan_btn)]
        ll.addLayout(lb)

        self.header = QLabel(wordWrap=True); self.header.setTextFormat(Qt.RichText)
        self.spinner = QProgressBar(maximumHeight=8, textVisible=False); self.spinner.setRange(0, 0)
        self.spinner.setFixedWidth(80)
        self.fetch_label = QLabel(FETCHING)
        fr = QHBoxLayout(); fr.addWidget(self.spinner); fr.addWidget(self.fetch_label); fr.addStretch(1)
        self.tracks = _Tree(lambda it: it.data(0, FILE), self.drop_right, lambda it: it.data(0, TRACK))
        self.tracks.setHeaderLabels(["#", "Release track", "Length", "Local file", "Length", "Similarity", "Pair"])
        for c, w in ((0, 110), (1, 210), (2, 100), (3, 230), (4, 45), (5, 70), (6, 120)):
            self.tracks.setColumnWidth(c, w)
        self.tracks.itemSelectionChanged.connect(self._sync_buttons)
        self.pair_btn = QPushButton("Pair with track…"); self.pair_btn.clicked.connect(lambda: self.pair_selected())
        self.pair_btn.setToolTip("Pair the selected file with a release track (Ctrl+P); same as dragging it there")
        self.unpin_btn = QPushButton("Unpin"); self.unpin_btn.clicked.connect(self.unpin_selected)
        self.split_btn = QPushButton("Split"); self.split_btn.clicked.connect(lambda: self.split_selected())
        self.split_btn.setToolTip("Keep this merged group's sub-tracks as separate tracks for this album")
        self.unsplit_btn = QPushButton("Undo split"); self.unsplit_btn.clicked.connect(lambda: self.unsplit_selected())
        self.unsplit_btn.setToolTip("Let this group merge again when its lengths confirm it")
        QShortcut(QKeySequence("Ctrl+P"), self, activated=lambda: self.pair_selected())
        self.hint = QLabel("Drag a file onto a release track to pair it by hand; onto a track with a file to swap; "
                           "onto “Unclustered files” to unpin or move it out.", wordWrap=True)
        self.hint.setStyleSheet("color:#666")
        pr = QHBoxLayout(); pr.addWidget(self.hint, 1); pr.addWidget(self.pair_btn); pr.addWidget(self.unpin_btn)
        pr.addWidget(self.split_btn); pr.addWidget(self.unsplit_btn)
        self.subtracks_note = QLabel(wordWrap=True); self.subtracks_note.setStyleSheet("color:#555")
        self.subtracks_note.setTextFormat(Qt.PlainText)
        self.merge_box = QCheckBox(MERGE_LABEL)
        self.merge_box.setToolTip("Off: sub-tracks ripped as one file are never merged on their own; drop the file "
                                  "on the group to pin it. Nothing is written to your files until Apply.")
        st = svc.settings.get()
        self.merge_box.setChecked(bool(getattr(st, "merge_subtracks", True)))
        self.merge_box.setEnabled(hasattr(st, "merge_subtracks"))
        self.merge_box.toggled.connect(self.set_merge_subtracks)
        self.problems = QLabel(wordWrap=True); self.problems.setStyleSheet("color:#a33")
        rw = QWidget(); rl = QVBoxLayout(rw); rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.header); rl.addLayout(fr); rl.addWidget(self.tracks, 1); rl.addLayout(pr)
        rl.addWidget(self.subtracks_note); rl.addWidget(self.merge_box); rl.addWidget(self.problems)

        split = QSplitter(); split.addWidget(lw); split.addWidget(rw); split.setSizes([420, 860])
        self.setCentralWidget(split)
        self._refresh_timer = QTimer(self, singleShot=True, interval=0, timeout=self.refresh)
        ev = svc.events
        self._conns = [(ev.album_changed, self._on_album), (ev.candidate_ready, lambda aid, rid: self._on_album(aid)),
                       (ev.job_finished, self._on_job)]
        for name, slot in (("album_removed", self._on_removed), ("match_view_changed", self._on_album), ("clusters_changed", lambda ids: self.refresh_soon()),
                           ("cluster_changed", self._on_cluster_changed)):
            sig = getattr(ev, name, None)
            if sig is not None:
                self._conns.append((sig, slot))
        for sig, slot in self._conns:
            sig.connect(slot)
        self.destroyed.connect(lambda *_: None)
        self.refresh()

    def closeEvent(self, e):
        for sig, slot in self._conns:
            try:
                sig.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        super().closeEvent(e)

    # ---------------------------------------------------------------- left pane
    def load_rows(self) -> list:
        out, offset = [], 0
        while True:
            page = self.svc.match.clusters(None, offset, PAGE)
            out += list(page.items); offset += len(page.items)
            if not page.items or offset >= page.total:
                return out

    def refresh_soon(self):
        self._refresh_timer.start()

    def refresh(self):
        """Reload the rows, keeping the selection and expanded rows by id."""
        rows = self.load_rows()
        self.rows = {r.id: r for r in rows}
        open_ids = {it.data(0, KEY)[1] for it in self._walk(self.left.invisibleRootItem())
                    if it.isExpanded() and it.data(0, KEY) and it.data(0, KEY)[0] == "row"}
        self.left.blockSignals(True)
        self.left.clear()
        tops = {}
        for kind, label in GROUPS:
            top = tops[kind] = QTreeWidgetItem(self.left, [label]); top.setData(0, KEY, ("group", kind))
            top.setFlags(top.flags() & ~Qt.ItemIsSelectable & ~Qt.ItemIsDragEnabled)
        keep = None
        for r in rows:
            it = QTreeWidgetItem(tops.get(r.kind, tops["folder"]), [self.row_label(r), str(r.file_count)])
            it.setData(0, KEY, ("row", r.id)); it.setToolTip(0, f"{r.folder}\n{r.id}")
            it.setFlags(it.flags() & ~Qt.ItemIsDragEnabled)
            if r.kind == "tag_cluster" and r.album_id is not None:
                it.setForeground(0, QBrush(QColor("#2a6")))
            if r.kind != "folder":                       # loose files: draggable children
                for f in self.svc.library.cluster(r.id).files:
                    ch = QTreeWidgetItem(it, [Path(f.path).name, ""])
                    ch.setData(0, KEY, ("file", r.id, str(f.path))); ch.setToolTip(0, str(f.path))
                    ch.setFlags(ch.flags() | Qt.ItemIsDragEnabled)
                it.setExpanded(r.id in open_ids or r.kind == "unclustered")
            if r.id == self.current:
                keep = it
        for kind, top in tops.items():
            top.setText(1, str(top.childCount())); top.setExpanded(True)
        self.left.blockSignals(False)
        if keep is not None:
            keep.setSelected(True); self.left.setCurrentItem(keep)
        elif self.current not in self.rows:
            self.current = None
        self.show_view()

    @staticmethod
    def row_label(r) -> str:
        if r.kind == "unclustered":
            return f"{Path(r.folder).name}/ (not in a cluster)"
        badge = f"  [{P.CLUSTER_BADGE}]" if r.kind == "tag_cluster" and r.album_id is not None else ""
        status = f" · {r.status.name.replace('_', ' ').lower()}" if r.status is not None else ""
        return f"{r.artist} – {r.title}{badge}{status}"

    def _left_path(self, item):
        k = item.data(0, KEY)
        return k[2] if k and k[0] == "file" else None

    def _on_select(self):
        items = self.left.selectedItems()
        key = items[0].data(0, KEY) if items else None
        self.current = key[1] if key and key[0] in ("row", "file") else None
        self.show_view()

    def _walk(self, item):
        for i in range(item.childCount()):
            ch = item.child(i); yield ch; yield from self._walk(ch)

    def item_for(self, row_id: str):
        return next((it for it in self._walk(self.left.invisibleRootItem())
                     if it.data(0, KEY) == ("row", row_id)), None)

    def select(self, row_id: str):
        it = self.item_for(row_id)
        if it is not None:
            self.left.setCurrentItem(it)

    # ---------------------------------------------------------------- right pane
    def show_view(self):
        r = self.rows.get(self.current) if self.current else None
        self.adopt_btn.setEnabled(r is not None and r.kind == "tag_cluster" and r.album_id is None)
        self.unadopt_btn.setEnabled(r is not None and r.kind == "tag_cluster" and r.album_id is not None)
        self.tracks.clear(); self.problems.clear(); self.subtracks_note.clear()
        if r is None:
            self.view = None
            self.header.setText("<i>Select an album, a cluster or a file.</i>")
            self.spinner.hide(); self.fetch_label.hide(); self._sync_buttons()
            return
        try:
            v = self.view = self.svc.match.view(r.id)
        except self.svc.types.NotFound:     # removed meanwhile (album_removed is on its way): reload the rows
            self.view = None
            self.header.setText("<i>This changed; reloading…</i>")
            self.spinner.hide(); self.fetch_label.hide(); self._sync_buttons()
            self.refresh_soon()
            return
        cand = next((x for x in v.candidates if x.release_id == v.release_id), None)
        badge = f" [{P.CLUSTER_BADGE}]" if v.cluster_id else ""
        if r.kind == "unclustered":
            sub = "Files moved out of every cluster: drag one onto a cluster of this folder to group it."
        elif v.album_id is None:
            sub = "Not adopted yet: <b>Adopt as album</b> to search Discogs for it. Nothing moves."
        elif cand is None:
            sub = f"Album #{v.album_id}{badge} · no Discogs candidates yet"
        else:
            sub = (f"Album #{v.album_id}{badge} · Discogs: {cand.artist} – {cand.title}"
                   f" ({cand.year}, {cand.catno}) · score {cand.score:.0%} · {len(v.candidates)} candidate(s)")
        name = Path(r.folder).name if r.kind == "unclustered" else f"{r.artist} – {r.title}"
        self.header.setText(f"<b>{name}</b> · {r.file_count} file(s) in {r.folder}<br>{sub}")
        waiting = v.album_id is not None and cand is not None and not v.fetched
        self.spinner.setVisible(waiting); self.fetch_label.setVisible(waiting)
        pinned = {Path(p.path) for p in getattr(v, "pins", [])}
        if v.fetched:
            n = sum(x.remote is not None for x in v.pairs)
            rel = QTreeWidgetItem(self.tracks, ["", f"Release tracks ({n})"
                                                + (f" · {len(v.unmatched_tracks)} without a file" if v.unmatched_tracks else "")])
            rel.setFlags(rel.flags() & ~Qt.ItemIsDragEnabled & ~Qt.ItemIsSelectable)
            groups = list(getattr(v, "subtrack_groups", None) or [])
            self.split_bases = set(self.splits()) if groups else set()
            merged = {tuple(g.positions): g for g in groups if g.merged}
            unmerged = {p: g for g in groups if not g.merged for p in g.positions}
            group_rows: dict[str, QTreeWidgetItem] = {}
            for pr in v.pairs:
                if pr.remote is None:
                    continue
                subs = tuple(getattr(pr.remote, "subtracks", None) or getattr(pr, "positions", None) or ())
                if subs and subs in merged:
                    self._group_row(rel, merged[subs], pr)
                elif pr.remote.position in unmerged:
                    g = unmerged[pr.remote.position]
                    if g.position not in group_rows:
                        group_rows[g.position] = self._group_row(rel, g, None)
                    self._track_row(group_rows[g.position], pr)
                else:
                    self._track_row(rel, pr)
            self.subtracks_note.setText("\n".join(
                self._group_note(g) + (" A split is stored for it; your pin wins (Undo split to drop it)."
                                       if g.position in self.split_bases and g.reason != "split" else "")
                for g in groups if g.reason != "not_needed" or g.position in self.split_bases))
            rel.setExpanded(True)
        if v.unmatched_files:
            un = QTreeWidgetItem(self.tracks, ["", f"Unmatched files ({len(v.unmatched_files)})"])
            un.setFlags(un.flags() & ~Qt.ItemIsDragEnabled & ~Qt.ItemIsSelectable)
            for lt in v.unmatched_files:
                it = QTreeWidgetItem(un, ["", "", "", Path(lt.path).name, _dur(lt.duration_s), "",
                                          "pinned (not used)" if Path(lt.path) in pinned else ""])
                it.setData(0, FILE, str(lt.path)); it.setToolTip(3, str(lt.path))
                it.setFlags(it.flags() | Qt.ItemIsDragEnabled)
            un.setExpanded(True)
        if v.problems:
            lines = []
            for p in v.problems:
                cs = P.pin_conflicts(p)
                lines += ([f"⚠ {P.label(p)}: {P.pin_conflict_text(c)}" for c in cs] if cs
                          else [f"⚠ {P.label(p)}: {p.message}"])
            self.problems.setText("<br>".join(html.escape(x, quote=False) for x in lines))
        self._sync_buttons()

    def _track_row(self, parent, pr):
        lt, rt = pr.local, pr.remote
        manual = pr.method == MANUAL
        okc = (("✓ " + MANUAL) if manual else "✓" if pr.ok else "✗") if lt else ""
        it = QTreeWidgetItem(parent, [rt.position, rt.title, _dur(rt.duration_s),
                                      Path(lt.path).name if lt else "— no file", _dur(lt.duration_s) if lt else "",
                                      _sim(pr.title_similarity) if lt else "", okc])
        it.setData(0, TRACK, rt.position)
        flags = it.flags() | Qt.ItemIsDropEnabled
        if lt is not None:
            it.setData(0, FILE, str(lt.path)); it.setToolTip(3, str(lt.path)); flags |= Qt.ItemIsDragEnabled
        else:
            flags &= ~Qt.ItemIsDragEnabled
        it.setFlags(flags)
        if manual:
            it.setToolTip(6, "Paired by you (drag-and-drop or Pair…)")
            self._paint(it, "#1f5fa8")
        elif lt is None or not pr.ok:
            self._paint(it, "#a33")
        return it

    @staticmethod
    def _paint(it, color):
        for col in range(7):
            it.setForeground(col, QBrush(QColor(color)))

    @staticmethod
    def group_badge(g) -> str:
        """Pair-column text of a subtrack group row."""
        parts = " + ".join(_short(p) for p in g.positions)
        if g.merged:
            return f"✓ {MANUAL} {parts}" if g.reason == "pinned" else f"merged {parts}"
        return "split" if g.reason == "split" else "not merged"

    @staticmethod
    def _group_note(g) -> str:
        why = SUBTRACK_REASONS.get(g.reason, g.reason)
        what = f"Sub-tracks {' + '.join(g.positions)} ({' / '.join(g.titles)})"
        if g.merged:
            return f"{what}: one file, {why}."
        tail = " Drop the file on the group row to pin it to both." if g.reason not in ("not_needed",) else ""
        return f"{what}: {why}.{tail}"

    def _group_row(self, parent, g, pr):
        """ONE row for a subtrack group: positions "2-9a + 2-9b", title "A / B", its file when merged."""
        lt = pr.local if pr is not None else None
        title = pr.remote.title if pr is not None else " / ".join(g.titles)
        length = _dur(g.duration_s) if g.duration_s is not None else "lengths unknown"
        it = QTreeWidgetItem(parent, [" + ".join(g.positions), title, length,
                                      Path(lt.path).name if lt else "— no file", _dur(lt.duration_s) if lt else "",
                                      _sim(pr.title_similarity) if lt else "", self.group_badge(g)])
        it.setData(0, TRACK, "+".join(g.positions)); it.setData(0, GROUP, g.position)
        it.setToolTip(6, SUBTRACK_REASONS.get(g.reason, g.reason))
        it.setToolTip(1, "\n".join(f"{p}  {t}" for p, t in zip(g.positions, g.titles)))
        flags = it.flags() | Qt.ItemIsDropEnabled
        if lt is not None:
            it.setData(0, FILE, str(lt.path)); it.setToolTip(3, str(lt.path)); flags |= Qt.ItemIsDragEnabled
        else:
            flags &= ~Qt.ItemIsDragEnabled
        it.setFlags(flags)
        font = it.font(1); font.setBold(True); it.setFont(1, font)
        self._paint(it, "#1f5fa8" if g.reason == "pinned" else "#2a6" if g.merged else "#8a6d00")
        it.setExpanded(True)
        return it

    def group_of(self, base: str):
        return next((g for g in (getattr(self.view, "subtrack_groups", None) or []) if g.position == base), None)

    def selected_group(self):
        items = self.tracks.selectedItems()
        base = items[0].data(0, GROUP) if items else None
        return self.group_of(base) if base else None

    def _sync_buttons(self):
        f = self.selected_file()
        can = self.view is not None and self.view.fetched and self.view.album_id is not None
        self.pair_btn.setEnabled(bool(can and f))
        self.unpin_btn.setEnabled(bool(can and f and self._pin_of(f) is not None))
        g = self.selected_group() if can else None
        has_split = getattr(self.svc.match, "split_subtracks", None) is not None
        self.split_btn.setEnabled(bool(has_split and g is not None and g.merged and g.reason != "pinned"))
        self.unsplit_btn.setEnabled(bool(has_split and g is not None
                                     and (g.reason == "split" or g.position in self.split_bases)))
        self.split_btn.setVisible(has_split); self.unsplit_btn.setVisible(has_split)

    def _view_album(self):
        return self.view.album_id if self.view is not None else None

    def _on_album(self, album_id: int):
        if self.view is not None and self.view.album_id == album_id:
            self.show_view()      # pins, fetched release, status: refetch the view

    def _on_removed(self, album_id: int):
        if any(r.album_id == album_id for r in self.rows.values()):
            if self.view is not None and self.view.album_id == album_id:
                self.view = None
            self.refresh_soon()

    def _on_job(self, job):
        kind = getattr(getattr(job, "kind", None), "name", "")
        if kind in ("CLUSTER", "SCAN"):
            self.refresh_soon()
        elif self.view is not None:
            self.show_view()

    def _on_cluster_changed(self, old: str, new: str):
        if self.current == old and new:
            self.current = new          # cluster ids hash their files: follow the cluster
        self.refresh_soon()

    # ---------------------------------------------------------------- pairing
    def selected_file(self) -> str | None:
        items = self.tracks.selectedItems()
        return items[0].data(0, FILE) if items else None

    def _pin_of(self, path):
        return next((p for p in getattr(self.view, "pins", []) if Path(p.path) == Path(path)), None) if self.view else None

    def _track_of(self, path) -> str | None:
        return next((pr.remote.position for pr in (self.view.pairs if self.view else [])
                     if pr.local is not None and pr.remote is not None and Path(pr.local.path) == Path(path)), None)

    def _file_on(self, position):
        return next((pr.local.path for pr in self.view.pairs
                     if pr.remote is not None and pr.remote.position == position and pr.local is not None), None)

    def pair(self, path, position: str) -> bool:
        """Pin `path` to the release track at `position`; swap if that track already has a file. Asks before
        overriding a disc hold. Returns True when the pair is in place."""
        v = self.view
        if v is None or v.album_id is None or not v.fetched:
            return False
        path = Path(path)
        if "+" in position:                              # a subtrack group row
            return self.pin_group(path, position.split("+"))
        if self._track_of(path) == position:
            return True
        other, back = self._file_on(position), self._track_of(path)
        aid = v.album_id
        if not self._pin_one(aid, path, position):
            return False
        if other is not None and Path(other) != path:
            if back is not None:             # swap: the other file takes the dragged file's old track
                self._pin_one(aid, Path(other), back)
            elif self._pin_of(other) is not None:
                self.svc.match.unpin(aid, Path(other))
        self.show_view()
        return True

    def pin_group(self, path, positions: list[str]) -> bool:
        """Pin ONE file to a whole subtrack group (match.pin(positions=...)); the group is then one track."""
        v = self.view
        if v is None or v.album_id is None or not v.fetched:
            return False
        path, aid = Path(path), v.album_id
        before = self._pin_of(path)
        if before is not None and list(getattr(before, "positions", [])) == list(positions):
            return True
        self.svc.match.pin(aid, path, positions=list(positions), release_id=v.release_id)
        self.view = self.svc.match.view(aid, v.release_id)
        conflict = self.conflict_for(path)
        if conflict is not None:
            self._restore(aid, path, before)
            self.inform("This pair can't be used", P.pin_conflict_text(conflict))
            self.show_view()
            return False
        self.show_view()
        return True

    def split_selected(self, base: str | None = None) -> bool:
        """Split a merged subtrack group (match.split_subtracks): its sub-tracks become separate tracks again."""
        g = self.group_of(base) if base else self.selected_group()
        if g is None or self.view is None or not g.merged or g.reason == "pinned":
            return False
        self.svc.match.split_subtracks(self.view.album_id, g.position, self.view.release_id)
        self.show_view()
        return True

    def unsplit_selected(self, base: str | None = None) -> bool:
        """Undo a split (match.unsplit_subtracks): the group may merge again."""
        g = self.group_of(base) if base else self.selected_group()
        if g is None or self.view is None or (g.reason != "split" and g.position not in self.split_bases):
            return False
        self.svc.match.unsplit_subtracks(self.view.album_id, g.position, self.view.release_id)
        self.show_view()
        return True

    def splits(self) -> list[str]:
        """The album's split groups (match.subtrack_splits), [] when the backend has none."""
        fn = getattr(self.svc.match, "subtrack_splits", None)
        if fn is None or self.view is None or self.view.album_id is None:
            return []
        return list(fn(self.view.album_id, self.view.release_id))

    def set_merge_subtracks(self, on: bool):
        """Settings.merge_subtracks; only how the release is read changes, nothing is written to the files."""
        try:
            self.svc.settings.set(merge_subtracks=bool(on))
        except self.svc.types.MMError as e:      # ValidationError: an older backend without the setting
            self.inform("Can't change this setting", str(e))
            return
        if self.view is not None:
            self.show_view()

    def _pin_one(self, aid, path: Path, position: str) -> bool:
        before = self._pin_of(path)
        self.svc.match.pin(aid, path, position, False, self.view.release_id)
        self.view = self.svc.match.view(aid, self.view.release_id)
        conflict = self.conflict_for(path)
        if conflict is None:
            return True
        if conflict.reason != P.OVERRIDE_REASON:
            self._restore(aid, path, before)
            self.inform("This pair can't be used", P.pin_conflict_text(conflict))
            return False
        if not self.confirm("Override the disc hold?",
                            f"{path.name} is held: the engine can't tell its disc, or its disc's file and track "
                            f"counts differ.\n\nPair it with “{position}” anyway? The pair is used as you set it "
                            "and the plan will say a disc hold was overridden."):
            self._restore(aid, path, before)
            return False
        self.svc.match.pin(aid, path, position, True, self.view.release_id)
        self.view = self.svc.match.view(aid, self.view.release_id)
        return True

    def conflict_for(self, path):
        """This file's PinConflict in the current view (Problem.pin_conflicts), else None."""
        return next((c for p in (self.view.problems if self.view else []) if P.kind_of(p) == P.PIN_CONFLICT
                     for c in P.pin_conflicts(p) if Path(c.path) == Path(path)), None)

    def _restore(self, aid, path, before):
        if before is None:
            self.svc.match.unpin(aid, path)
        elif len(getattr(before, "positions", None) or []) > 1:
            self.svc.match.pin(aid, path, positions=list(before.positions), override_hold=before.override_hold,
                               release_id=before.release_id)
        else:
            self.svc.match.pin(aid, path, before.position, before.override_hold, before.release_id)
        self.view = self.svc.match.view(aid, self.view.release_id)

    def pair_selected(self, position: str | None = None) -> bool:
        """Keyboard pairing: the selected file onto a release track chosen from a list."""
        f = self.selected_file()
        if not f or self.view is None or not self.view.fetched:
            return False
        if position is None:
            tracks = [pr.remote for pr in self.view.pairs if pr.remote is not None]
            names = [f"{t.position}  {t.title}" for t in tracks]
            pick, ok = QInputDialog.getItem(self, "Pair with track", f"Pair {Path(f).name} with:", names, 0, False)
            if not ok:
                return False
            position = tracks[names.index(pick)].position
        return self.pair(f, position)

    def unpin_selected(self):
        f = self.selected_file()
        if f and self.view is not None and self._pin_of(f) is not None:
            self.svc.match.unpin(self.view.album_id, Path(f))
            self.show_view()

    # ---------------------------------------------------------------- drops
    def drop_right(self, path: str, target):
        """target: a release-track position, or its tree item."""
        pos = target.data(0, TRACK) if isinstance(target, QTreeWidgetItem) else target
        if pos:
            self.pair(path, pos)

    def drop_left(self, path: str, target):
        """target: a left-pane key (("group", kind) / ("row", id) / ("file", id, path)), or its tree item."""
        key = target.data(0, KEY) if isinstance(target, QTreeWidgetItem) else target
        if not key:
            return
        target = None
        if key == ("group", "unclustered") or (key[0] in ("row", "file") and
                                               getattr(self.rows.get(key[1]), "kind", "") == "unclustered"):
            if self._pin_of(path) is not None:            # pinned: drop the pin, nothing else
                self.svc.match.unpin(self.view.album_id, Path(path)); self.show_view()
                return
        elif key[0] in ("row", "file") and getattr(self.rows.get(key[1]), "kind", "") == "tag_cluster":
            target = key[1]
        else:
            return
        try:
            self.svc.library.move_to_cluster(Path(path), target)
        except Exception as e:        # ValidationError: not a loose file / another folder
            self.inform("Can't move this file", str(e))
            return
        self.refresh()

    # ---------------------------------------------------------------- adopt / un-adopt / scan
    def adopt(self):
        r = self.rows.get(self.current) if self.current else None
        if r is None or r.kind != "tag_cluster" or r.album_id is not None:
            return
        row = self.svc.library.adopt_cluster(r.id)
        self.last_find_job = self.svc.match.find([row.id])
        self.refresh(); self.select(r.id)

    def unadopt(self):
        r = self.rows.get(self.current) if self.current else None
        if r is None or r.kind != "tag_cluster" or r.album_id is None:
            return
        try:
            c = self.svc.library.unadopt_cluster(r.album_id)
        except Exception as e:        # MMError: applied once (undo it in History)
            self.inform("Can't un-adopt", str(e))
            return
        self.current = c.id
        self.refresh(); self.select(c.id)

    def scan_clusters(self):
        self.last_cluster_job = self.svc.library.scan_clusters()
        return self.last_cluster_job
