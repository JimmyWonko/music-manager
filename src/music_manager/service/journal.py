"""service.journal: the operation history, and undo as a previewed plan running backwards."""

from __future__ import annotations

import bisect
import builtins
import os
from dataclasses import dataclass
from pathlib import Path

from .. import journal as journal_mod
from ..fs import AUDIO_EXTS, trash_for
from ..playlists import PLAYLIST_EXTS
from ..tags.snapshot import LegacySnapshotError, preview_restore
from ._common import now_utc, set_status
from ._ops import (
    journal_dir,
    operation_row,
    record_operation,
    register_cli_journals,
    to_operation,
)
from ._state import State, events, state
from ._types import (
    AlbumPlan,
    FileChange,
    Job,
    JobKind,
    ManualPair,
    MatchStatus,
    MMError,
    NotFound,
    Operation,
    Page,
    Plan,
    PlanStale,
    Problem,
    ProblemKind,
    TagChange,
)

LEGACY_MESSAGE = ("These tag changes were recorded in an older journal format (pickled snapshots); restoring them "
                  "needs your explicit confirmation (trust_legacy=True).")


def list(offset: int = 0, limit: int = 200) -> Page[Operation]:  # noqa: A001 - contract name
    """Newest first. Journals written by the discogs-tag command are added on the way."""
    st = state()
    register_cli_journals(st)
    total = st.index.one("SELECT COUNT(*) AS n FROM operations")["n"]
    rows = st.index.query("SELECT * FROM operations ORDER BY started DESC, id DESC LIMIT ? OFFSET ?",
                          (max(0, int(limit)), max(0, int(offset))))
    return Page([to_operation(r) for r in rows], total, max(0, int(offset)))


# --- undo preview ---------------------------------------------------------------------

@dataclass
class _UndoRecord:
    operation_id: int
    plan: Plan
    trust_legacy: bool
    reasons: tuple[str, ...]  # every step's check result when previewed
    fingerprint: dict[str, tuple]
    groups: dict[int, tuple[int, Path, Path]]  # group -> (album id, folder now, folder it goes back to)


def _stat(path: Path) -> tuple:
    try:
        st = os.lstat(path)
    except OSError:
        return ("missing",)
    return ("dir",) if os.path.isdir(path) and not os.path.islink(path) else ("file", st.st_size, st.st_mtime_ns)


def _kind_of(path: Path, is_dir: bool = False) -> str:
    """By name only: during an undo the file is usually not at that path yet."""
    if is_dir:
        return "extra"
    suffix = path.suffix.lower()
    if suffix in PLAYLIST_EXTS:
        return "playlist"
    return "track" if suffix in AUDIO_EXTS else "extra"


def _steps(entries, trust_legacy: bool, where: list[Path | None] | None = None):
    """The undo steps and each one's check result, simulated in order.

    where, when given, gets for every step the real path that holds the
    step's file at the moment the step would run (earlier steps may have
    moved it, e.g. through a stage name in a swap).
    """
    steps = journal_mod.undo_plan(entries, trust_legacy=trust_legacy)
    view = journal_mod._UndoView(simulate=True)
    reasons = []
    for step in steps:
        if where is not None:
            real = None
            if step.current is not None:
                key = str(step.current)
                real = view.over.get(key, key) if key in view.over else key
            where.append(Path(real) if real not in (None, journal_mod._UndoView._DIR) else None)
        reason = step.check(view)
        if not reason:
            step.simulate(view)
        reasons.append(reason)
    return steps, reasons


@dataclass(frozen=True)
class _Origin:
    """What an undo preview knows about the album a journal block applied."""
    release_id: int = 0
    artist: str = ""
    title: str = ""


def _groups(st: State, op_id: int, entries
            ) -> tuple[list[int], dict[int, tuple[int, Path, Path]], dict[int, _Origin]]:
    """One group per "start" entry: (album id, folder now, folder it goes back to), and its release.

    release_id, artist and title come from the start entry (journals since
    2026-10-17); else the release_id the service recorded for the
    operation; else 0 and "" (e.g. older discogs-tag journals).
    """
    starts = [i for i, e in enumerate(entries) if e.get("action") == "start"]
    known = {r["folder_before"]: r for r in st.index.query("SELECT * FROM operation_albums WHERE op_id = ?", (op_id,))}
    groups: dict[int, tuple[int, Path, Path]] = {}
    origins: dict[int, _Origin] = {}
    for g, i in enumerate(starts):
        e = entries[i]
        original = Path(e.get("album") or e.get("root") or "/")
        now = Path(e.get("dest") or original)
        row = known.get(str(original))
        album_id = row["album_id"] if row else 0
        if not album_id:
            found = st.index.one("SELECT id FROM albums WHERE folder IN (?, ?)", (str(now), str(original)))
            album_id = found["id"] if found else 0
        groups[g] = (album_id, now, original)
        try:
            release_id = int(e.get("release_id") or 0)
        except (TypeError, ValueError):
            release_id = 0
        if not release_id and row is not None and row["release_id"]:
            release_id = int(row["release_id"])
        origins[g] = _Origin(release_id, str(e.get("artist") or ""), str(e.get("title") or ""))
    return starts, groups, origins


def _preview(st: State, op_id: int, trust_legacy: bool) -> tuple[Plan, _UndoRecord]:
    row = operation_row(op_id, st)
    op = to_operation(row)
    if not op.undoable:
        why = "already undone" if op.undone else ("undo operations are not undoable" if op.kind == "undo"
                                                   else "its journal is missing")
        raise MMError(f"operation {op_id} can't be undone ({why})")
    path = Path(row["journal"])
    entries = journal_mod.read_journal(path, log=lambda _line: None)
    legacy_op = op.legacy
    where: list[Path | None] = []
    steps, reasons = _steps(entries, trust_legacy, where)
    starts, groups, origins = _groups(st, op_id, entries)
    if not groups:
        groups[-1] = (0, Path("/"), Path("/"))

    def group_of(index: int) -> int:
        g = bisect.bisect_right(starts, index) - 1
        return g if g in groups else next(iter(groups))

    per_album: dict[int, dict[str, FileChange]] = {g: {} for g in groups}
    problems: dict[int, list[Problem]] = {g: [] for g in groups}
    fingerprint: dict[str, tuple] = {}
    legacy_paths: dict[int, list[Path]] = {}
    for step, reason, real in zip(steps, reasons, where, strict=True):
        g = group_of(step.index)
        files = per_album[g]
        for p in (step.current, step.original):
            if p is not None:
                fingerprint[str(p)] = _stat(p)
        is_legacy_block = step.action == "tags" and step.legacy and not trust_legacy
        skip = reason if reason and not is_legacy_block else None
        if step.action == "rename":
            is_dir = os.path.isdir(real) if real else False
            key = str(step.original)
            before = step.current
            earlier = files.pop(str(step.current), None) if str(step.current) != key else None
            if earlier is not None and earlier.path_after == step.current:
                before = earlier.path_before  # a chain (A -> stage -> B, as in a swap) shows as A -> B
            files[key] = FileChange(_kind_of(step.original, is_dir), before, step.original,
                                    files[key].tags if key in files else [], skip or (files[key].skip_reason
                                                                                      if key in files else None))
        elif step.action == "trashed":
            files[str(step.original)] = FileChange(_kind_of(step.original), step.current, step.original, [], skip)
        elif step.action == "create":
            files[str(step.current)] = FileChange(_kind_of(step.current), step.current,
                                                  trash_for(step.current, create=False).root / "files"
                                                  / step.current.name, [], skip)
        elif step.action == "tags":
            key = str(step.current)
            existing = files.get(key)
            where_now = real or step.current  # the real file that will be at step.current by then
            tags: list[TagChange] = []
            if is_legacy_block:
                legacy_paths.setdefault(g, []).append(step.current)
            elif not reason and os.path.lexists(where_now):
                try:
                    tags = [TagChange(k, b, a) for k, b, a in preview_restore(where_now, step.snapshot or {},
                                                                              trust_legacy)]
                except (LegacySnapshotError, ValueError, OSError) as exc:
                    skip = skip or f"cannot read the saved tags: {exc}"
            if existing:
                files[key] = FileChange("track", existing.path_before, existing.path_after,  # tag steps: audio only
                                        existing.tags + tags, existing.skip_reason or skip)
            else:
                files[key] = FileChange("track", step.current, step.current, tags, skip)
        if skip:
            target = step.current or step.original
            problems[g].append(Problem(ProblemKind.FILE_CHANGED, f"{target}: {skip}; left alone", [target]))
    for g, paths in legacy_paths.items():
        problems[g].append(Problem(ProblemKind.LEGACY_JOURNAL, LEGACY_MESSAGE, paths))
    if legacy_op and not trust_legacy and not legacy_paths:
        pass  # a legacy journal without tag steps undoes normally

    albums = []
    files_to_move = tags_to_write = bytes_to_copy = skipped = 0
    for g, (album_id, now_folder, original) in groups.items():
        changes = [*per_album[g].values()]
        for c in changes:
            if c.skip_reason:
                skipped += 1
                continue
            tags_to_write += len(c.tags)
            if c.path_before != c.path_after:
                files_to_move += 1
                try:
                    src_dev = os.lstat(c.path_before).st_dev
                    dst = next((p for p in (c.path_after.parent, *c.path_after.parents) if p.exists()), None)
                    if dst is not None and os.stat(dst).st_dev != src_dev:
                        bytes_to_copy += os.lstat(c.path_before).st_size
                except OSError:
                    pass
        origin = origins.get(g, _Origin())
        albums.append(AlbumPlan(album_id, origin.release_id, now_folder, original, changes, problems[g],
                                origin.artist, origin.title))
    plan = Plan(st.next_id(), albums, [], files_to_move, tags_to_write, bytes_to_copy, skipped)
    record = _UndoRecord(op_id, plan, trust_legacy, tuple(reasons), fingerprint, groups)
    return plan, record


def undo_plan(operation_id: int, trust_legacy: bool = False) -> Plan:
    """Read-only preview of undoing an operation: the apply's plan running backwards.

    Files edited, moved or deleted since are kept in files with skip_reason
    and reported as FILE_CHANGED problems; they are left alone. With an
    old-format journal the tag steps are LEGACY_JOURNAL problems unless
    trust_legacy. Only the latest preview of an operation can be run.
    """
    st = state()
    plan, record = _preview(st, int(operation_id), trust_legacy)
    with st.lock:
        st.plans[plan.id] = record
        st.undo_previews[int(operation_id)] = plan.id
    return plan


def undo(operation_id: int, plan_id: int) -> Job:
    """Run the latest undo preview of an operation, if the disk still matches it (else PlanStale)."""
    st = state()
    op_id = int(operation_id)
    row = operation_row(op_id, st)
    with st.lock:
        record = st.plans.get(int(plan_id))
        if not isinstance(record, _UndoRecord) or record.operation_id != op_id:
            raise NotFound(f"undo preview {plan_id} for operation {op_id}")
        if st.undo_previews.get(op_id) != int(plan_id):
            raise PlanStale(f"undo preview {plan_id} is not the latest preview for operation {op_id}: preview again")
    if any(p.kind is ProblemKind.LEGACY_JOURNAL for ap in record.plan.albums for p in ap.problems):
        raise MMError("Undo refused: this journal uses the old format; preview with trust_legacy=True after "
                      "confirming it is your own")
    path = Path(row["journal"])

    def work(ctx) -> str:
        with st.lock:
            if st.undo_previews.get(op_id) != int(plan_id):
                raise PlanStale(f"undo preview {plan_id} is no longer the latest: preview again")
            del st.undo_previews[op_id]
            st.plans.pop(int(plan_id), None)
        entries = journal_mod.read_journal(path, log=lambda _line: None)
        steps, reasons = _steps(entries, record.trust_legacy)
        now = {p: _stat(Path(p)) for p in record.fingerprint}
        if tuple(reasons) != record.reasons or now != record.fingerprint:
            raise PlanStale("files changed since the undo preview: preview again")
        started = now_utc()
        view = journal_mod._UndoView(simulate=False)
        journal = journal_mod.Journal("undo", journal_dir(st))
        journal.write(action="start", command="undo-journal", journal=str(path))
        done = 0
        skipped: list[str] = []
        total = len(steps)
        try:
            for i, step in enumerate(steps):
                ctx.progress(i, total, check=False)
                reason = step.check(view)
                if not reason:
                    try:
                        step.run(journal)
                        done += 1
                        continue
                    except (OSError, ValueError, TypeError) as exc:
                        reason = str(exc)
                skipped.append(f"{step.label}: {reason}")
        finally:
            journal.close()
        op = to_operation(row)
        summary = f"Undid: {op.summary}" + (f" ({len(skipped)} step(s) skipped)" if skipped else "")
        moved = sum(1 for s in steps if s.action in ("rename", "trashed"))
        new_id = record_operation(
            st, kind="undo", started=started, finished=now_utc(), albums=sum(1 for g in record.groups.values() if g[0]),
            files=moved, summary=summary, journal=journal.path, undo_of=op_id,
            album_rows=[(aid, now_folder, original, None)
                        for aid, now_folder, original in record.groups.values() if aid])
        with st.index.tx() as conn:
            conn.execute("UPDATE operations SET undone_by = ?, undone = ? WHERE id = ?",
                         (new_id, 0 if skipped else 1, op_id))
        for album_id, _now_folder, original in record.groups.values():
            if not album_id:
                continue
            try:
                _restore_album(st, album_id, original)
            except NotFound:
                continue
        msg = f"reversed {done} change(s)" + (f", {len(skipped)} skipped" if skipped else "")
        return msg

    return st.runner.submit(JobKind.UNDO, work, total=None, message="queued")


def _restore_album(st: State, album_id: int, original: Path) -> None:
    from ._common import root_for
    from .plan import _reindex

    root_row = st.index.one("SELECT root FROM albums WHERE id = ?", (album_id,))
    if root_row is None:
        raise NotFound(f"album {album_id}")
    root = root_for(original, st) or Path(root_row["root"])
    if original.is_dir():
        _reindex(st, album_id, root, original, MatchStatus.NEEDS_REVIEW)
    else:
        set_status(album_id, MatchStatus.NEEDS_REVIEW)
    if st.index.one("SELECT id FROM albums WHERE id = ?", (album_id,)) is not None:  # an adopted cluster
        events.album_changed.emit(album_id)  # undone into its folder is removed (album_removed)


def manual_pairs(operation_id: int) -> builtins.list[ManualPair]:
    """The manual pairs (match.pin) an apply used, from its journal, in journal order. [] for other operations."""
    st = state()
    row = operation_row(int(operation_id), st)
    if not row["journal"] or not Path(row["journal"]).is_file():
        return []
    entries = journal_mod.read_journal(Path(row["journal"]), log=lambda _line: None)
    starts, groups, _origins = _groups(st, int(operation_id), entries)
    out = []
    for g, i in enumerate(starts):
        for m in entries[i].get("manual") or []:
            out.append(ManualPair(groups[g][0], Path(m["path"]), str(m["position"]), bool(m.get("override_hold"))))
    return out


__all__ = ["list", "manual_pairs", "undo", "undo_plan"]
