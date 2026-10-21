"""service.plan: build a read-only plan for accepted albums, then apply it (the only disk write)."""

from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from .. import coverart as cover_engine
from .. import moves as moves_mod
from ..apply import AlbumApplyError, execute_layout, release_info
from ..fs import collect_albums, file_sha256, is_junk_file, junk_files
from ..journal import Journal
from ..playlists import playlist_path
from ..tags.fields import tag_values
from ..tags.id3 import latin1_losses
from ..tags.write import preview_tag_changes
from . import coverart
from ._common import album_record, cluster_guess, is_loose_tracks, now_utc, set_status
from ._ops import journal_dir, record_operation
from ._planning import (
    AlbumTarget,
    album_target,
    chosen_release_id,
    cross_album_collisions,
    resolve_template,
)
from ._releases import _primary_image, fetch_release, provider_call, release_is_fresh
from ._state import State, events, state
from ._types import (
    AlbumPlan,
    CoverDefaults,
    EncodingLoss,
    FileChange,
    Job,
    JobKind,
    JobState,
    MatchStatus,
    MMError,
    NotFound,
    Plan,
    PlanStale,
    Problem,
    ProblemKind,
    TagChange,
    UnwrittenField,
)

V23_SUFFIXES = (".wav", ".aif", ".aiff")  # always ID3v2.3 (tags.write); MP3 only with Settings.id3v23_mp3
COVER_NAME = "folder.jpg"
COVER_STEMS = ("cover", "folder", "front")


@dataclass
class _AlbumRecord:
    target: AlbumTarget
    plan: AlbumPlan
    fingerprint: dict[str, tuple]
    cover_url: str | None = None
    embed: cover_engine.EmbedCover | None = None  # front cover embedded with the tags


@dataclass
class _PlanRecord:
    plan: Plan
    albums: list[_AlbumRecord]
    clear_other: bool
    consumed: bool = False
    steps: int = 0
    not_fetched: list[AlbumPlan] = field(default_factory=list)  # left out: release still missing


# --- fingerprints: detect files that changed between build and apply ---------------

def _stat(path: Path) -> tuple:
    try:
        st = os.lstat(path)
    except OSError:
        return ("missing",)
    if os.path.isdir(path) and not os.path.islink(path):
        return ("dir", tuple(sorted(os.listdir(path))))
    return ("file", st.st_size, st.st_mtime_ns)


def _fingerprint(t: AlbumTarget, extra: list[Path] = ()) -> dict[str, tuple]:
    paths: list[Path] = [t.album.path, t.target, *extra]
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

def _junk_left_behind(album: Path, root: Path | None, target: Path, moving: set[Path]) -> list[Path]:
    """The junk files (fs.junk_files) apply's cleanup would trash: in the album folder (those not moving with
    it) and in each parent that holds nothing else, up to (not including) the library root, never in the
    album's new folder or its parents."""
    out = [p for p in junk_files(album) if p not in moving]
    keep = {target, *target.parents}
    current = album
    while root is not None and root in current.parent.parents and current.parent not in keep:
        parent = current.parent
        try:
            if any(p != current and not is_junk_file(p) for p in parent.iterdir()):
                break
        except OSError:
            break
        out += junk_files(parent)
        current = parent
    return out


def _file_changes(t: AlbumTarget, clear_other: bool, warnings: list[str], migrate: bool = False,
                  unwritten: list[UnwrittenField] | None = None, styles: tuple[bool, bool] = (False, False),
                  id3: tuple[bool, bool] = (False, False),
                  losses: list[EncodingLoss] | None = None) -> list[FileChange]:
    layout = t.layout
    assert layout is not None
    changes: list[FileChange] = []
    for item in layout.tracks:  # the apply writes item.meta: the same choice as the preview
        item.meta["migrate_legacy_keys"] = migrate
        item.meta["write_styles"], item.meta["styles_in_genre"] = styles
        item.meta["id3_v23"], item.meta["id3_latin1"] = id3
        v23 = item.source.suffix.lower() in V23_SUFFIXES or (item.source.suffix.lower() == ".mp3" and id3[0])
        if id3[1] and v23 and losses is not None:
            losses += [EncodingLoss(item.source, f, v, lost) for f, v, lost in latin1_losses(tag_values(item.meta))]
    held = release_info(t.release, t.dtracks, t.original_year).tags
    for note in held.notes:
        warnings.append(f"{t.album.path.name}: {note}")
    if unwritten is not None:
        unwritten += [UnwrittenField(u.field, u.reason, u.detail, t.album.path) for u in held.unwritten]
    by_source = {item.source: item for item in layout.tracks}
    for local, _dt, method in layout.pairs:
        item = by_source.get(local)
        if item is None:
            changes.append(FileChange("track", local, local, [],
                                      skip_reason="no matching Discogs track: left in place, not tagged or moved"))
            continue
        try:
            tags = [TagChange(c.key, c.before, c.after, c.key_before, c.key_after, c.migration)
                    for c in preview_tag_changes(item.source, item.meta, clear_other)]
        except Exception as exc:  # noqa: BLE001 - unreadable file: the apply will report it
            tags = []
            warnings.append(f"cannot read the tags of {item.source}: {exc}")
        changes.append(FileChange("track", item.source, item.dest, tags, pinned=method == "manual",
                                  merged_subtracks=list(_dt.subtracks) if _dt is not None else []))
    for item in layout.moves:
        changes.append(FileChange("extra", item.source, item.dest, []))
    for old in layout.old_playlists:
        changes.append(FileChange("playlist", old, playlist_path(layout.target_dir), []))
    return changes


def _with_cover(changes: list[FileChange], image, opts: CoverDefaults, warnings: list[str]) -> list[FileChange]:
    """Add a "cover_art" TagChange to every track that plan.apply would give the front cover."""
    out = []
    for change in changes:
        if change.kind == "track" and not change.skip_reason:
            try:
                diff = cover_engine.preview_embed(change.path_before, image, opts.replace_cover_art)
            except Exception as exc:  # noqa: BLE001 - unreadable file: the apply will report it
                diff = None
                warnings.append(f"cannot read the cover art of {change.path_before}: {exc}")
            if diff is not None:
                change = replace(change, tags=[*change.tags, TagChange("cover_art", *diff)])
        out.append(change)
    return out


def _cover_for(st: State, t: AlbumTarget, opts: CoverDefaults, warnings: list[str]) -> coverart.Choice:
    folder_ok = not cluster_guess(album_record(t.album_id, st)) and not is_loose_tracks(t.album)
    choice = coverart.choose(st, t.album.path, t.release, opts, folder_ok=folder_ok)
    warnings += choice.warnings
    return choice


FETCH_RETRY_AFTER_S = 120.0  # a release that failed to download is not retried by plan.build for this long


def _fetch_job(st: State, wanted: list[tuple[int, int]]) -> int | None:
    """Queue (or reuse) the background job that downloads these releases; None if none can start now.

    Jobs run one at a time, so the newest job covering the wanted releases
    finishes after all the others: that is the one returned.
    """
    now = time.time()
    with st.lock:
        covering: list[int] = []
        todo: list[tuple[int, int]] = []
        for pair in wanted:
            job_id = st.fetching.get(pair)
            if job_id is not None:
                try:
                    live = st.runner.get(job_id).state not in (JobState.DONE, JobState.FAILED, JobState.CANCELLED)
                except NotFound:
                    live = False
                if live:
                    covering.append(job_id)
                    continue
                st.fetching.pop(pair, None)
            failed = st.fetch_failures.get(pair)
            if failed is not None and now - failed[0] < FETCH_RETRY_AFTER_S:
                continue
            todo.append(pair)
        if not todo:
            return max(covering) if covering else None

        def work(ctx) -> str:
            fetched, errors = 0, []
            try:
                for i, (album_id, release_id) in enumerate(todo):
                    ctx.progress(i, len(todo), f"fetching release {release_id} from Discogs")
                    try:
                        fetch_release(album_id, release_id, st)
                    except MMError as exc:
                        with st.lock:
                            st.fetch_failures[(album_id, release_id)] = (time.time(), str(exc))
                        errors.append(f"release {release_id}: {exc}")
                    else:
                        with st.lock:
                            st.fetch_failures.pop((album_id, release_id), None)
                        fetched += 1
                    events.album_changed.emit(album_id)
                ctx.progress(len(todo), len(todo), "fetched")
            finally:
                with st.lock:
                    for pair in todo:
                        if st.fetching.get(pair) == ctx.job_id:
                            st.fetching.pop(pair, None)
            msg = f"Fetched {fetched} release(s): rebuild the plan"
            if errors:
                msg += f"; {len(errors)} failed: " + " | ".join(errors)
            if not fetched:
                raise MMError(msg)
            return msg

        job = st.runner.submit(JobKind.FETCH_CANDIDATES, work, total=len(todo), message="queued")
        for pair in todo:
            st.fetching[pair] = job.id
        return job.id


def _names(t: AlbumTarget) -> tuple[str, str]:
    from ..providers.discogs import release_artists

    if is_loose_tracks(t.album):  # never tagged as that release: it is held, not overridable
        return t.album.path.name, ""
    return release_artists(t.release), str(t.release.get("title") or "")


def build(album_ids: list[int], template: str | None = None,
          allow_track_mismatch: set[int] = frozenset()) -> Plan:
    """Everything plan.apply would do, file by file, with every problem. Read-only (never writes a status).

    Uses each album's chosen release, else its best candidate. Never calls
    Discogs: an album whose full release is not stored (or is older than the
    6-hour Discogs caching limit) gets a RELEASE_NOT_FETCHED problem and no
    files, and the release is downloaded by a background job, Plan.fetch_job.
    Build the plan again when that job finishes. allow_track_mismatch: album
    ids whose track_mismatch problem is overridden.
    """
    st = state()
    compiled = resolve_template(template)
    cfg = st.config()
    ids = [int(a) for a in album_ids]
    wanted: list[tuple[int, int]] = []
    pending: dict[int, AlbumTarget] = {}
    targets: list[AlbumTarget] = []
    for aid in ids:
        rid = chosen_release_id(aid, st)
        if rid is not None and not release_is_fresh(aid, rid, st):
            wanted.append((aid, rid))
            pending[aid] = album_target(aid, rid, compiled, st=st, fetch=False)
            continue
        targets.append(album_target(aid, rid, compiled, st=st, fetch=False, need_release=True))
    fetch_job = _fetch_job(st, wanted) if wanted else None
    cross = cross_album_collisions(targets + list(pending.values()))
    allowed = {int(a) for a in allow_track_mismatch}
    warnings: list[str] = []
    records: list[_AlbumRecord] = []
    plans: dict[int, AlbumPlan] = {}
    files_to_move = tags_to_write = bytes_to_copy = skipped = steps = 0
    covers = 0
    opts = coverart.options(st)
    cover_wanted: list[tuple[int, str]] = []
    for t in pending.values():
        failed = st.fetch_failures.get((t.album_id, t.release_id))
        if failed is not None and fetch_job is None:
            message = f"Release {t.release_id} could not be fetched from Discogs: {failed[1]}"
        else:
            message = f"Release {t.release_id} is being fetched from Discogs: build the plan again when it arrives"
        problem = Problem(ProblemKind.RELEASE_NOT_FETCHED, message, [t.album.path])
        plans[t.album_id] = AlbumPlan(t.album_id, t.release_id, t.album.path, t.target, [],
                                      [problem, *cross.get(t.album_id, [])], *_names(t))
    for t in targets:
        problems = [p for p in t.problems if not (p.kind is ProblemKind.TRACK_MISMATCH and t.album_id in allowed)]
        problems += cross.get(t.album_id, [])
        unwritten: list[UnwrittenField] = []
        losses: list[EncodingLoss] = []
        changes = _file_changes(t, cfg.clear_other_tags, warnings, cfg.migrate_tag_keys, unwritten,
                               (cfg.write_styles, cfg.styles_in_genre), (cfg.id3v23_mp3, cfg.id3_latin1), losses)
        if losses:
            warnings.append(f"{t.album.path.name}: {len(losses)} tag value(s) lose characters as Latin-1 "
                            "(Settings.id3_latin1): see AlbumPlan.encoding_losses")
        junk = _junk_left_behind(t.album.path, t.root, t.target, {c.path_before for c in changes})
        choice = _cover_for(st, t, opts, warnings)
        if choice.pending_url and not problems:  # held albums download nothing
            cover_wanted.append((t.album_id, choice.pending_url))
        embed = None
        if choice.image is not None:
            changes = _with_cover(changes, choice.image, opts, warnings)
            if any(tc.key == "cover_art" for c in changes for tc in c.tags):
                embed = cover_engine.EmbedCover(choice.image, opts.replace_cover_art)
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
        if t.layout is not None and t.layout.pin_overrides:
            warnings.append(f"{len(t.layout.pin_overrides)} manual pair(s) of {t.album.path.name} override a disc hold "
                            "(match.pin override_hold=True)")
        cover_url = None
        if cfg.save_cover_art:
            url, _thumb = _primary_image(t.release)
            has_cover = any(p.suffix.lower() in (".jpg", ".jpeg", ".png") and p.stem.casefold() in COVER_STEMS
                            and p.parent == t.album.path for p in t.album.sidecars)
            if url and not has_cover:
                cover_url = url
                covers += 1
        ap = AlbumPlan(t.album_id, t.release_id, t.album.path, t.target, changes, problems, *_names(t),
                       cover=choice.art if embed is not None else None, unwritten=unwritten, junk=junk,
                       encoding_losses=losses)
        plans[t.album_id] = ap
        art_path = [choice.art.path] if embed is not None and choice.art.path else []
        records.append(_AlbumRecord(t, ap, _fingerprint(t, art_path), cover_url, embed))
    if pending:
        if fetch_job is not None:
            warnings.append(f"{len(pending)} release(s) are being fetched from Discogs (job {fetch_job}): "
                            "build the plan again when it finishes")
        else:
            warnings.append(f"{len(pending)} release(s) could not be fetched from Discogs")
    refused = sum(1 for r in records if r.plan.problems)
    if refused:
        warnings.append(f"{refused} album(s) have problems and will be refused (held) by apply")
    if covers:
        warnings.append(f"{COVER_NAME} will be saved from Discogs for {covers} album(s)")
    cover_job = coverart.fetch_job(st, cover_wanted) if cover_wanted else None
    if cover_wanted:
        warnings.append(f"the Discogs cover of {len(cover_wanted)} album(s) is not downloaded yet"
                        + (f" (job {cover_job}): build the plan again to embed it" if cover_job else ""))
    plan = Plan(st.next_id(), [plans[aid] for aid in ids if aid in plans], warnings, files_to_move, tags_to_write,
                bytes_to_copy, skipped, fetch_job, cover_job)
    with st.lock:
        st.plans[plan.id] = _PlanRecord(plan, records, cfg.clear_other_tags, steps=steps,
                                        not_fetched=[plans[aid] for aid in pending])
    return plan


# --- apply ---------------------------------------------------------------------------

def _save_cover(st: State, journal: Journal | None, target: Path, url: str, notes: list[str]) -> None:
    dest = target / COVER_NAME
    if journal is None or os.path.lexists(dest):
        return
    try:
        data = coverart.cached_image(st, url) or provider_call(st.client().image, url)
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
    other = st.index.one("SELECT id FROM albums WHERE folder = ? AND id != ?", (str(folder), album_id))
    if other is not None:  # an adopted cluster went back to its folder (undo): the folder's own row takes its files
        with st.index.tx() as conn:
            conn.execute("DELETE FROM albums WHERE id = ?", (album_id,))
            st.index.delete_fts(conn, album_id)
        if found is not None:
            index_album(st, root, found, album_id=other["id"])
        events.album_removed.emit(album_id)
        events.album_changed.emit(other["id"])
        return
    if found is not None:
        index_album(st, root, found, status=status, album_id=album_id)
    set_status(album_id, status, **columns)


def apply(plan_id: int) -> Job:
    """Run a plan. Albums with problems, or whose files changed since the build, are refused and HELD.

    Raises MMError at once, before any job, when no album of the plan is
    applicable now (every one has problems or waits for its release); the
    albums with problems are held. If albums that looked fine are all
    refused during the job (files changed on disk), the job ends FAILED.

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
        applicable = [r for r in rec.albums if not r.plan.problems]
        if applicable:
            rec.consumed = True
    if not applicable:
        # Nothing could run: say so now, before any job. Albums with problems are held as usual.
        for r in rec.albums:
            set_status(r.plan.album_id, MatchStatus.HELD, held_for=list(r.plan.problems))
        parts = []
        if rec.albums:
            parts.append(f"{len(rec.albums)} album(s) have problems (held; rebuild the plan)")
        if rec.not_fetched:
            parts.append(f"{len(rec.not_fetched)} album(s) wait for their release from Discogs (rebuild the plan)")
        raise MMError("Nothing to apply: " + ("; ".join(parts) or "the plan is empty"))

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
                moves_mod.VERIFY_HASH = st.config().verify_hash  # cross-drive copies (Settings.verify_hash)
                try:
                    done = execute_layout(
                        t.layout, dry_run=False, clear_other=rec.clear_other, scan_root=t.root, journal=journal,
                        log=lambda _line: None, on_file=step,
                        after_moves=(lambda j, t=t, url=r.cover_url: _save_cover(st, j, t.target, url, notes))
                        if r.cover_url else None,
                        cover=r.embed)
                except AlbumApplyError as exc:
                    failed.append((r, str(exc)))
                    events.album_changed.emit(ap.album_id)
                    continue
                moved += len(done)
                applied.append(r)
                _reindex(st, ap.album_id, t.root, t.target, MatchStatus.APPLIED, chosen_release=ap.release_id)
                with st.index.tx() as conn:  # the pins are used up: the journal keeps them (journal.manual_pairs)
                    conn.execute("DELETE FROM pins WHERE album_id = ?", (ap.album_id,))
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
        if rec.not_fetched:
            msg += f"; left out {len(rec.not_fetched)} whose release is not fetched yet (rebuild the plan)"
        if failed:
            msg += f"; {len(failed)} failed: " + " | ".join(f"{r.target.album.path}: {err}" for r, err in failed)
        if notes:
            msg += "; " + "; ".join(notes)
        if not applied:
            raise MMError(msg)
        return msg

    return st.runner.submit(JobKind.APPLY, work, total=rec.steps, message="queued")


__all__ = ["apply", "build"]
