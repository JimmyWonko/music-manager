"""service.plan: build a read-only plan for accepted albums, then apply it (the only disk write)."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..apply import AlbumApplyError, execute_layout
from ..fs import collect_albums, file_sha256
from ..journal import Journal
from ..playlists import playlist_path
from ..tags.write import preview_tags
from ._common import now_utc, set_status
from ._ops import journal_dir, record_operation
from ._planning import AlbumTarget, album_target, cross_album_collisions, resolve_template
from ._releases import _primary_image, provider_call
from ._state import State, events, state
from ._types import (
    AlbumPlan,
    FileChange,
    Job,
    JobKind,
    MatchStatus,
    MMError,
    NotFound,
    Plan,
    PlanStale,
    Problem,
    ProblemKind,
    TagChange,
)

COVER_NAME = "folder.jpg"
COVER_STEMS = ("cover", "folder", "front")


@dataclass
class _AlbumRecord:
    target: AlbumTarget
    plan: AlbumPlan
    fingerprint: dict[str, tuple]
    cover_url: str | None = None


@dataclass
class _PlanRecord:
    plan: Plan
    albums: list[_AlbumRecord]
    clear_other: bool
    consumed: bool = False
    steps: int = 0


# --- fingerprints: detect files that changed between build and apply ---------------

def _stat(path: Path) -> tuple:
    try:
        st = os.lstat(path)
    except OSError:
        return ("missing",)
    if os.path.isdir(path) and not os.path.islink(path):
        return ("dir", tuple(sorted(os.listdir(path))))
    return ("file", st.st_size, st.st_mtime_ns)


def _fingerprint(t: AlbumTarget) -> dict[str, tuple]:
    paths: list[Path] = [t.album.path, t.target]
    layout = t.layout
    if layout is not None:
        for item in [*layout.tracks, *layout.moves]:
            paths += [item.source, item.dest]
            if item.source.is_dir():
                for dirpath, _dirs, files in os.walk(item.source):
                    paths += [Path(dirpath) / f for f in files]
        paths += layout.old_playlists + layout.unmatched
    return {str(p): _stat(p) for p in paths}


def _changed(fp: dict[str, tuple]) -> list[Path]:
    return [Path(p) for p, old in fp.items() if _stat(Path(p)) != old]


# --- sizes for cross-device moves -----------------------------------------------------

def _device(path: Path) -> int | None:
    for candidate in (path, *path.parents):
        try:
            return os.stat(candidate).st_dev
        except OSError:
            continue
    return None


def _size(path: Path) -> int:
    if path.is_dir() and not path.is_symlink():
        total = 0
        for dirpath, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.lstat(Path(dirpath) / name).st_size
                except OSError:
                    pass
        return total
    try:
        return os.lstat(path).st_size
    except OSError:
        return 0


# --- build ---------------------------------------------------------------------------

def _file_changes(t: AlbumTarget, clear_other: bool, warnings: list[str]) -> list[FileChange]:
    layout = t.layout
    assert layout is not None
    changes: list[FileChange] = []
    by_source = {item.source: item for item in layout.tracks}
    for local, _dt, _method in layout.pairs:
        item = by_source.get(local)
        if item is None:
            changes.append(FileChange("track", local, local, [],
                                      skip_reason="no matching Discogs track: left in place, not tagged or moved"))
            continue
        try:
            tags = [TagChange(k, b, a) for k, b, a in preview_tags(item.source, item.meta, clear_other)]
        except Exception as exc:  # noqa: BLE001 - unreadable file: the apply will report it
            tags = []
            warnings.append(f"cannot read the tags of {item.source}: {exc}")
        changes.append(FileChange("track", item.source, item.dest, tags))
    for item in layout.moves:
        changes.append(FileChange("extra", item.source, item.dest, []))
    for old in layout.old_playlists:
        changes.append(FileChange("playlist", old, playlist_path(layout.target_dir), []))
    return changes


def build(album_ids: list[int], template: str | None = None,
          allow_track_mismatch: set[int] = frozenset()) -> Plan:
    """Everything plan.apply would do, file by file, with every problem. Read-only (never writes a status).

    Uses each album's chosen release, else its best candidate. A release that
    is not fetched yet is fetched now (index-only write; the only network
    call outside a job). allow_track_mismatch: album ids whose
    track_mismatch problem is overridden.
    """
    st = state()
    compiled = resolve_template(template)
    cfg = st.config()
    ids = [int(a) for a in album_ids]
    targets = [album_target(aid, None, compiled, st=st, fetch=True, need_release=True) for aid in ids]
    cross = cross_album_collisions(targets)
    allowed = {int(a) for a in allow_track_mismatch}
    warnings: list[str] = []
    records: list[_AlbumRecord] = []
    files_to_move = tags_to_write = bytes_to_copy = skipped = steps = 0
    covers = 0
    for t in targets:
        problems = [p for p in t.problems if not (p.kind is ProblemKind.TRACK_MISMATCH and t.album_id in allowed)]
        problems += cross.get(t.album_id, [])
        changes = _file_changes(t, cfg.clear_other_tags, warnings)
        dest_dev = _device(t.target)
        for change in changes:
            if change.skip_reason:
                skipped += 1
                continue
            tags_to_write += len(change.tags)
            if change.kind == "track":
                steps += 1  # tag write
            if change.kind != "playlist" and change.path_before != change.path_after:
                files_to_move += 1
                steps += 1
                if dest_dev is not None and _device(change.path_before) != dest_dev:
                    bytes_to_copy += _size(change.path_before)
        cover_url = None
        if cfg.save_cover_art:
            url, _thumb = _primary_image(t.release)
            has_cover = any(p.suffix.lower() in (".jpg", ".jpeg", ".png") and p.stem.casefold() in COVER_STEMS
                            and p.parent == t.album.path for p in t.album.sidecars)
            if url and not has_cover:
                cover_url = url
                covers += 1
        ap = AlbumPlan(t.album_id, t.release_id, t.album.path, t.target, changes, problems)
        records.append(_AlbumRecord(t, ap, _fingerprint(t), cover_url))
    refused = sum(1 for r in records if r.plan.problems)
    if refused:
        warnings.append(f"{refused} album(s) have problems and will be refused (held) by apply")
    if covers:
        warnings.append(f"{COVER_NAME} will be saved from Discogs for {covers} album(s)")
    plan = Plan(st.next_id(), [r.plan for r in records], warnings, files_to_move, tags_to_write, bytes_to_copy,
                skipped)
    with st.lock:
        st.plans[plan.id] = _PlanRecord(plan, records, cfg.clear_other_tags, steps=steps)
    return plan


# --- apply ---------------------------------------------------------------------------

def _save_cover(st: State, journal: Journal | None, target: Path, url: str, notes: list[str]) -> None:
    dest = target / COVER_NAME
    if journal is None or os.path.lexists(dest):
        return
    try:
        data = provider_call(st.client().image, url)
    except MMError as exc:
        notes.append(f"cover art not saved for {target.name}: {exc}")
        return
    journal.write(action="create-intent", path=str(dest))
    fd, tmp = tempfile.mkstemp(dir=target, prefix=".folder.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if os.path.lexists(dest):
            raise FileExistsError(dest)
        os.rename(tmp, dest)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        notes.append(f"cover art not saved for {target.name}: {exc}")
        return
    journal.write(action="create", path=str(dest), sha256=file_sha256(dest))


def _label(r: _AlbumRecord) -> str:
    from ..providers.discogs import release_artists

    return f"{release_artists(r.target.release)} \u2013 {r.target.release.get('title') or r.target.album.path.name}"


def _reindex(st: State, album_id: int, root: Path, folder: Path, status: MatchStatus, **columns) -> None:
    from .library import index_album

    found = next((a for a in collect_albums(folder) if a.path == folder), None) if folder.is_dir() else None
    if found is not None:
        index_album(st, root, found, status=status, album_id=album_id)
    set_status(album_id, status, **columns)


def apply(plan_id: int) -> Job:
    """Run a plan. Albums with problems, or whose files changed since the build, are refused and HELD.

    One journal and one Operation for the whole run. An album that fails
    while being applied keeps its status and is reported in the job message.
    """
    st = state()
    with st.lock:
        rec = st.plans.get(int(plan_id))
        if not isinstance(rec, _PlanRecord):
            raise NotFound(f"plan {plan_id}")
        if rec.consumed:
            raise PlanStale(f"plan {plan_id} was already applied: build a new plan")
        rec.consumed = True

    def work(ctx) -> str:
        started = now_utc()
        journal: Journal | None = None
        applied: list[_AlbumRecord] = []
        refused: list[_AlbumRecord] = []
        failed: list[tuple[_AlbumRecord, str]] = []
        notes: list[str] = []
        moved = 0
        done_steps = 0
        total = sum(1 for r in rec.albums if not r.plan.problems for c in r.plan.files
                    if not c.skip_reason for _ in range((c.kind == "track") + (c.path_before != c.path_after)))
        ctx.progress(0, total, "applying")

        def step(_path) -> None:
            nonlocal done_steps
            done_steps += 1
            ctx.progress(min(done_steps, total), total, check=False)

        try:
            for r in rec.albums:
                ctx.check()
                ap = r.plan
                if ap.problems:
                    set_status(ap.album_id, MatchStatus.HELD, held_for=list(ap.problems))
                    refused.append(r)
                    continue
                changed = _changed(r.fingerprint)
                if changed:
                    problem = Problem(ProblemKind.FILE_CHANGED,
                                      "Files changed on disk since the plan was built: rebuild the plan", changed)
                    set_status(ap.album_id, MatchStatus.HELD, held_for=[problem])
                    refused.append(r)
                    continue
                if journal is None:
                    journal = Journal("apply", journal_dir(st))
                t = r.target
                assert t.layout is not None
                try:
                    done = execute_layout(
                        t.layout, dry_run=False, clear_other=rec.clear_other, scan_root=t.root, journal=journal,
                        log=lambda _line: None, on_file=step,
                        after_moves=(lambda j, t=t, url=r.cover_url: _save_cover(st, j, t.target, url, notes))
                        if r.cover_url else None)
                except AlbumApplyError as exc:
                    failed.append((r, str(exc)))
                    events.album_changed.emit(ap.album_id)
                    continue
                moved += len(done)
                applied.append(r)
                _reindex(st, ap.album_id, t.root, t.target, MatchStatus.APPLIED, chosen_release=ap.release_id)
        finally:
            if journal is not None:
                journal.close()
                names = ", ".join(_label(r) for r in applied) or "nothing"
                summary = f"Tagged and moved: {names}"
                if failed:
                    summary += f"; failed: {', '.join(_label(r) for r, _ in failed)}"
                touched = applied + [r for r, _ in failed]
                record_operation(st, kind="apply", started=started, finished=now_utc(), albums=len(applied),
                                 files=moved, summary=summary, journal=journal.path,
                                 album_rows=[(r.plan.album_id, r.plan.folder_before, r.plan.folder_after,
                                              r.plan.release_id) for r in touched])
        msg = f"Applied {len(applied)} album(s)"
        if refused:
            msg += f"; refused {len(refused)} with problems (held; rebuild the plan)"
        if failed:
            msg += f"; {len(failed)} failed: " + " | ".join(f"{r.target.album.path}: {err}" for r, err in failed)
        if notes:
            msg += "; " + "; ".join(notes)
        if not applied:
            raise MMError(msg)
        return msg

    return st.runner.submit(JobKind.APPLY, work, total=rec.steps, message="queued")


__all__ = ["apply", "build"]
