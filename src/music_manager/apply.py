"""Apply a chosen release to an album: validate the layout, tag every file, then move everything.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .conflicts import (
    DiscConflict,
    DiscFolderMismatch,
    describe_disc_conflict,
    describe_folder_mismatch,
)
from .fs import DISC_DIR, AlbumFolder, file_sha256
from .journal import Journal
from .match import TITLE_MATCH_MIN, align_tracks_explained, ambiguous_disc_files
from .moves import (
    PlannedFile,
    _mkdirs_logged,
    _prune_empty,
    _trash_logged,
    describe_moves,
    move_planned,
    prune_limit,
)
from .naming import (
    album_folder_name,
    folder_lead_year,
    format_artist,
    format_catalog,
    reissue_original_year,
    track_filename,
    track_prefix,
    year4,
)
from .naming.templates import CompiledTemplate, album_fields, track_fields
from .playlists import (
    PLAYLIST_EXTS,
    album_playlist_tracks,
    is_generated_playlist,
    playlist_path,
    render_playlist,
    write_playlist_file,
)
from .providers.discogs import (
    DiscogsRelease,
    DiscogsTrack,
    release_artists,
    release_catno,
    release_discs,
)
from .tags.fields import TrackMeta
from .tags.snapshot import capture_tags
from .tags.write import write_tags

# ---------------------------------------------------------------------------
# Applying a release: tag everything, then move everything
# ---------------------------------------------------------------------------

class AlbumApplyError(RuntimeError):
    """An album could not be fully applied. The message says what happened."""


# (kind, message, paths): kinds are "dest_inside_source", "source_inside_target", "symlink_outside"
LayoutProblem = tuple[str, str, list[Path]]


def check_layout(album: AlbumFolder, dest_root: Path, target_dir: Path) -> list[LayoutProblem]:
    """Every reason validate_layout would refuse this layout, without raising (see there)."""
    problems: list[LayoutProblem] = []
    src = album.path.resolve()
    target = target_dir.resolve()
    root = dest_root.resolve()
    if target != src:
        if root == src or root.is_relative_to(src):
            problems.append(("dest_inside_source",
                             f"refusing: destination {dest_root} is inside the source album {album.path}\n"
                             "  nothing was changed", [dest_root]))
        elif target.is_relative_to(src):
            problems.append(("dest_inside_source",
                             f"refusing: target folder {target_dir} is inside the source album {album.path}\n"
                             "  nothing was changed", [target_dir]))
        elif src.is_relative_to(target):
            problems.append(("source_inside_target",
                             f"refusing: target folder {target_dir} contains the source album {album.path}\n"
                             "  nothing was changed", [target_dir]))
    escaping: list[str] = []
    links: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(album.path, followlinks=False):
        for name in dirnames + filenames:
            entry = Path(dirpath) / name
            if not entry.is_symlink():
                continue
            real = entry.resolve()
            if real != src and not real.is_relative_to(src):
                escaping.append(f"    {entry}  ->  {real}")
                links.append(entry)
    if escaping:
        problems.append(("symlink_outside",
                         "refusing: symlinks in the album point outside it (their targets would be re-tagged or moved):\n"
                         + "\n".join(escaping) + "\n  nothing was changed", links))
    return problems


def validate_layout(album: AlbumFolder, dest_root: Path, target_dir: Path) -> None:
    """Refuse layouts that would make an apply write into or through its own source.

    Raises AlbumApplyError (before anything is written) when the destination
    library or the album's target folder lies inside the source album, when the
    target folder contains the source album (the album would be emptied into
    one of its parents), or when a symlink in the album points outside it
    (tagging would modify a file elsewhere). Re-tagging in place (target is
    the album folder itself) is allowed.
    """
    problems = check_layout(album, dest_root, target_dir)
    if problems:
        raise AlbumApplyError(problems[0][1])


@dataclass
class ReleaseInfo:
    """Album-level values of a release, shared by every track's tags and the folder name."""
    album_artist: str
    folder_artist: str
    title: str
    release_year: str
    original_year: str  # the master's year as given ("" when unknown)
    reissue_original: str  # original date for tags: only for reissues
    catno: str
    label: str
    genre: str
    discs: int
    master_id: int
    by_disc: dict[int, list[DiscogsTrack]]


def release_info(release: DiscogsRelease, dtracks: list[DiscogsTrack], original_year: str) -> ReleaseInfo:
    album_artist = release_artists(release)
    release_year = year4(release.get("released") or release.get("year"))
    label = ""
    if release.get("labels"):
        label = release["labels"][0].get("name") or ""
    genres = release.get("genres") or []
    styles = release.get("styles") or []
    by_disc: dict[int, list[DiscogsTrack]] = {}
    for dt in dtracks:
        by_disc.setdefault(dt.disc, []).append(dt)
    return ReleaseInfo(
        album_artist=album_artist,
        folder_artist=format_artist(album_artist),
        title=release.get("title") or "",
        release_year=release_year,
        original_year=original_year,
        # Original date only for reissues (master year earlier than this release).
        reissue_original=reissue_original_year(original_year, release_year),
        catno=release_catno(release),
        label=label,
        genre=", ".join(styles[:2] or genres[:2]),
        discs=release_discs(release, dtracks),
        master_id=release.get("master_id") or 0,
        by_disc=by_disc,
    )


def track_meta(release: DiscogsRelease, info: ReleaseInfo, dt: DiscogsTrack, dtracks: list[DiscogsTrack]) -> TrackMeta:
    """The tags write_tags writes for one track of the release."""
    disc_tracks = info.by_disc.get(dt.disc, dtracks)
    return {
        "artist": dt.artist or info.album_artist,
        "albumartist": info.album_artist,
        "album": info.title,
        "title": dt.title,
        "track": dt.number,
        "tracktotal": len(disc_tracks),
        "disc": dt.disc,
        "disctotal": info.discs,
        "date": str(release.get("released") or info.release_year or ""),
        "originaldate": info.reissue_original,
        "label": info.label,
        "catalognumber": format_catalog(info.catno) or "",
        "genre": info.genre,
        "release_id": release.get("id"),
        "master_id": info.master_id,
        "position": dt.position,
    }


def default_target_dir(dest_root: Path, info: ReleaseInfo) -> Path:
    """Today's layout: <dest>/<Artist>/[orig]  Title [year] [CATNO] [2CD]."""
    folder = album_folder_name(info.title, info.original_year, info.release_year, info.catno, info.discs)
    return dest_root / info.folder_artist / folder


def default_file_name(dt: DiscogsTrack, local: Path) -> str:
    return track_filename(track_prefix(dt), dt.title, local.suffix)


def template_album_fields(release: DiscogsRelease, info: ReleaseInfo) -> dict[str, str]:
    """The album-level template fields (%albumartist%, %leadyear%, ...) of a release."""
    return album_fields(
        album_artist=info.album_artist,
        title=info.title,
        lead_year=folder_lead_year(info.original_year, info.release_year),
        original_year=folder_lead_year(info.original_year, info.release_year),
        reissue_year=info.release_year if reissue_original_year(info.original_year, info.release_year) else "",
        catno=info.catno,
        label=info.label,
        formats=list(release.get("formats") or []),
        discs=info.discs,
    )


def template_track_fields(dt: DiscogsTrack, side_names: bool | None = None) -> dict[str, str]:
    return track_fields(title=dt.title, artist=dt.artist, number=dt.number,
                        position=track_prefix(dt, side_names), disc=dt.disc)


def template_paths(
    template: CompiledTemplate | None,
    dest_root: Path,
    release: DiscogsRelease,
    dtracks: list[DiscogsTrack],
    original_year: str,
    side_names: bool | None = None,
) -> tuple[Path, Callable[[DiscogsTrack, Path], str]]:
    """(target folder, file namer) for a template; None is the built-in archivist layout.

    The built-in archivist preset is today's layout exactly (default_target_dir
    and track_filename), including the "2CD" a multi-disc folder gets, which
    the preset's text has no field for.
    """
    info = release_info(release, dtracks, original_year)
    if template is None:
        return default_target_dir(dest_root, info), (
            lambda dt, local: track_filename(track_prefix(dt, side_names), dt.title, local.suffix))
    fields = template_album_fields(release, info)
    target = dest_root.joinpath(*template.folder_parts(fields))
    return target, lambda dt, local: template.file_name(fields, template_track_fields(dt, side_names), local.suffix)


@dataclass
class AlbumLayout:
    """Everything an apply of one album would do, computed without touching disk."""
    album: AlbumFolder
    dest_root: Path
    target_dir: Path
    pairs: list[tuple[Path, DiscogsTrack | None, str]]  # local, track, match method (see align_tracks)
    tracks: list[PlannedFile]  # tagged and moved
    unmatched: list[Path]  # left in place, untagged
    moves: list[PlannedFile] = field(default_factory=list)  # extra folders and sidecars
    old_playlists: list[Path] = field(default_factory=list)  # generated playlists: dropped, regenerated
    legacy: bool = False
    clashes: list[list[Path]] = field(default_factory=list)  # sources that map to one destination
    release_id: int = 0  # the release being applied (recorded in the journal's "start" entry)
    artist: str = ""
    title: str = ""
    ambiguous: list[Path] = field(default_factory=list)  # refused: disc unknown (also in unmatched)
    disc_conflicts: list[DiscConflict] = field(default_factory=list)  # why each of ambiguous was refused


def layout_album(
    album: AlbumFolder,
    dest_root: Path,
    release: DiscogsRelease,
    dtracks: list[DiscogsTrack],
    original_year: str,
    *,
    target_dir: Path | None = None,
    file_name: Callable[[DiscogsTrack, Path], str] | None = None,
    check: bool = True,
    log: Callable[[str], None] = print,
    title_match_min: float = TITLE_MATCH_MIN,
) -> AlbumLayout:
    """Plan one album's apply: where it goes, every file's tags and destination. Read-only.

    target_dir and file_name override today's layout (default_target_dir /
    default_file_name). With check, validate_layout runs and raises, and so does a track
    bound for another disc's CD folder (see disc_folder_mismatches).
    title_match_min: the title-pass pairing threshold (see align_tracks).
    """
    info = release_info(release, dtracks, original_year)
    if target_dir is None:
        target_dir = default_target_dir(dest_root, info)
    name_of = file_name or default_file_name

    log(f"  dest  {target_dir}")
    if check:
        validate_layout(album, dest_root, target_dir)

    pairs, legacy, conflicts = align_tracks_explained(album, dtracks, title_match_min)
    if legacy:
        log("  note  these files use the old vinyl numbering (B1 = 21); they are renumbered 1..N")
    ambiguous = ambiguous_disc_files(pairs)
    if ambiguous:
        log(f"  warning: {disc_warning(album, ambiguous, conflicts)}")
        for conflict in conflicts:
            log(f"    {describe_disc_conflict(conflict, album.path)}")

    plan: list[PlannedFile] = []
    unmatched: list[Path] = []
    for local, dt, method in pairs:
        if dt is None and method == "ambiguous_disc":
            unmatched.append(local)
            continue
        if dt is None:
            log(f"  skip  extra file  {local.name}  (left in place)")
            unmatched.append(local)
            continue
        meta = track_meta(release, info, dt, dtracks)
        dest_file = target_dir / name_of(dt, local)
        where = f"{dt.disc}-{dt.number}"
        if dt.position and dt.position != str(dt.number):
            where += f", {dt.position}"
        log(f"  {local.name}")
        shown = dest_file.relative_to(target_dir) if target_dir in dest_file.parents else dest_file.name
        log(f"      -> {shown}   [{where}]")
        plan.append(PlannedFile(local, dest_file, meta))

    clash: dict[str, list[Path]] = {}
    for item in plan:
        clash.setdefault(str(item.dest).casefold(), []).append(item.source)
    clashes = [v for v in clash.values() if len(v) > 1]
    if clashes:
        detail = "; ".join(", ".join(p.name for p in v) for v in clashes)
        log(f"  warning: several files map to the same name and will get (2) suffixes: {detail}")

    # Generated album playlists list the old file names. They are not moved;
    # they are deleted and a fresh one is written once the move succeeds.
    old_playlists: list[Path] = []
    moves: list[PlannedFile] = [PlannedFile(extra_dir, target_dir / extra_dir.name) for extra_dir in album.extra_dirs]
    for sidecar in album.sidecars:
        if sidecar.suffix.lower() in PLAYLIST_EXTS and is_generated_playlist(sidecar):
            old_playlists.append(sidecar)
        else:
            moves.append(PlannedFile(sidecar, target_dir / sidecar.relative_to(album.path)))
            if sidecar.suffix.lower() in PLAYLIST_EXTS:
                log(f"  note  hand-made playlist {sidecar.name} is moved as-is; it may list old file names")
    layout = AlbumLayout(album, dest_root, target_dir, pairs, plan, unmatched, moves, old_playlists, legacy, clashes,
                         release_id=int(release.get("id") or 0), artist=info.album_artist, title=info.title,
                         ambiguous=ambiguous, disc_conflicts=conflicts)
    if check:
        wrong = disc_folder_mismatches(layout)
        if wrong:
            raise AlbumApplyError(disc_folder_message(album, wrong, "would put tracks into") +
                                  "\n  nothing was written")
    return layout


def disc_warning(album: AlbumFolder, files: list[Path], conflicts: list[DiscConflict] | None = None) -> str:
    """The message for files whose disc cannot be told (see match.align_tracks_explained)."""
    def names(paths):
        return ", ".join(str(p.relative_to(album.path)) if album.path in p.parents else p.name for p in paths)

    mixed = {c.path for c in conflicts or [] if c.reason == "sources_disagree"}
    parts = []
    rest = [p for p in files if p not in mixed]
    if rest:
        parts.append(f"cannot tell which disc {len(rest)} file(s) belong to: {names(rest)}. Their track numbers "
                     "repeat on several discs and no disc tag, CD1/CD2 folder, 1-01 style name or title says which. "
                     "Nothing is paired by guesswork; put each disc in its own CD1/CD2 folder or name the files "
                     "1-01, 2-01, ...")
    if mixed:
        sources = [p for p in files if p in mixed]
        parts.append(f"the disc folder, disc tag and cue/m3u of {len(sources)} file(s) don't agree: "
                     f"{names(sources)}. Nothing is paired; move each file into its disc's folder (or fix its "
                     "disc tag) so all three agree, then try again")
    return " Also: ".join(parts)


def disc_folder_mismatches(layout: AlbumLayout) -> list[DiscFolderMismatch]:
    """Tracks the layout would put in a CD1/Disc 2 folder whose number is not their disc tag.

    Every folder between the destination library and the file counts. plan.build reports
    these (DISC_FOLDER_MISMATCH), layout_album(check=True) refuses them, and execute_layout
    refuses them again before writing anything (the backstop).
    """
    out = []
    for item in layout.tracks:
        disc = int(item.meta.get("disc") or 0) if item.meta else 0
        if not disc:
            continue
        try:
            parts = item.dest.parent.relative_to(layout.dest_root).parts
            base = layout.dest_root
        except ValueError:
            parts, base = (item.dest.parent.name,), item.dest.parent.parent
        for i, part in enumerate(parts):
            m = re.search(r"\d+", part) if DISC_DIR.match(part) else None
            if m and int(m.group(0)) != disc:
                out.append(DiscFolderMismatch(item.source, item.dest, base.joinpath(*parts[:i + 1]),
                                              int(m.group(0)), disc))
                break
    return out


def disc_folder_message(album: AlbumFolder, mismatches: list[DiscFolderMismatch], lead: str) -> str:
    detail = "; ".join(describe_folder_mismatch(m) for m in mismatches)
    return (f"{album.path}: {lead} another disc's folder ({len(mismatches)} file(s)): {detail}. "
            "Change the naming template so each disc's tracks go into that disc's folder")


def execute_layout(
    layout: AlbumLayout,
    dry_run: bool,
    clear_other: bool = False,
    scan_root: Path | None = None,
    journal_dir: Path | None = None,
    *,
    journal: Journal | None = None,
    log: Callable[[str], None] = print,
    on_file: Callable[[Path], None] | None = None,
    after_moves: Callable[[Journal | None], None] | None = None,
) -> list[tuple[Path, Path]]:
    """Carry out a layout: tag every file in place, then move everything, then playlists and cleanup.

    Without a journal, a real run opens (and closes) its own. A shared
    journal gets this album's "start" entry and is left open. on_file is
    called after each file is tagged and after each move; after_moves runs
    once everything moved (before playlists and pruning).
    """
    album, dest_root, target_dir = layout.album, layout.dest_root, layout.target_dir
    plan = layout.tracks
    wrong = disc_folder_mismatches(layout)
    if wrong:  # the backstop: never file a track into another disc's folder, whatever planned it
        raise AlbumApplyError(disc_folder_message(album, wrong, "refusing to put tracks into") +
                              "\n  nothing was written")

    # Phase 1: tags. Nothing moves unless every file was tagged.
    # A real run journals every change before doing it, so the whole apply can be undone.
    own_journal = journal is None
    if not dry_run:
        if journal is None:
            journal = Journal("apply", journal_dir)
        journal.write(action="start", command="apply", album=str(album.path), dest=str(target_dir),
                      release_id=layout.release_id, artist=layout.artist, title=layout.title)
    else:
        journal = None
    tagged: list[Path] = []
    try:
        for item in plan:
            try:
                if journal is not None:
                    journal.write(action="tags", phase="before", path=str(item.source), snapshot=capture_tags(item.source))
                removed = write_tags(item.source, item.meta, clear_other=clear_other, dry_run=dry_run)
            except Exception as exc:  # noqa: BLE001 - any failure must stop this album only
                msg = f"tagging failed for {item.source}: {type(exc).__name__}: {exc}\n  nothing was moved"
                if tagged and not dry_run:
                    msg += f"; {len(tagged)} file(s) were already re-tagged in place:\n"
                    msg += "\n".join(f"    {p}" for p in tagged)
                raise AlbumApplyError(msg) from exc
            if journal is not None:
                journal.write(action="tags", phase="after", path=str(item.source), sha256=file_sha256(item.source))
            if removed:
                log(f"      {'would clear' if dry_run else 'cleared'}  {', '.join(removed)}  ({item.source.name})")
            tagged.append(item.source)
            if on_file is not None and not dry_run:
                on_file(item.source)

        moves: list[PlannedFile] = list(plan)
        for item in layout.moves:
            if item.source in album.extra_dirs:
                log(f"  move  {item.source.name}/")
            else:
                log(f"  move  {item.source.relative_to(album.path)}")
            moves.append(item)
        for playlist in layout.old_playlists:
            log(f"  drop  {playlist.relative_to(album.path)}  (generated playlist; regenerated after the move)")

        if dry_run:
            return []

        # Phase 2: moves.
        done: list[tuple[Path, Path]] = []
        try:
            _mkdirs_logged(journal, target_dir)
            move_planned(moves, done, journal)
        except Exception as exc:  # noqa: BLE001
            moved_sources = {src for src, _ in done}
            pending = [m for m in moves if m.source not in moved_sources]
            raise AlbumApplyError(
                f"move failed: {type(exc).__name__}: {exc}\n"
                f"  all files were tagged; the album is split between\n"
                f"    {album.path}\n    {target_dir}\n" + describe_moves(done, pending)
            ) from exc
        if on_file is not None:
            for src, _dest in done:
                on_file(src)
        if after_moves is not None:
            after_moves(journal)

    # Phase 3: playlists and cleanup.
        if layout.old_playlists:
            regenerate_playlist(target_dir, layout.old_playlists, journal)
        if album.path.resolve() != target_dir.resolve():
            limit = prune_limit(album.path, dest_root, scan_root)
            for gone in _prune_empty(album.path, limit, protect=[dest_root, target_dir], journal=journal):
                log(f"  removed empty folder  {gone}")
        return done
    finally:
        if journal is not None and own_journal:
            journal.close()
            log(f"journal: {journal.path}")
            log(f"undo:    discogs-tag --undo-journal '{journal.path}'   (preview; add --apply to undo)")


def apply_album(
    album: AlbumFolder,
    dest_root: Path,
    release: DiscogsRelease,
    dtracks: list[DiscogsTrack],
    original_year: str,
    dry_run: bool,
    clear_other: bool = False,
    scan_root: Path | None = None,
    journal_dir: Path | None = None,
) -> list[tuple[Path, Path]]:
    """Tag the album's files, then move them into place.

    Order: plan everything, write tags to every file in place (nothing is
    moved if any file fails), then move tracks, extra folders and sidecars.
    original_year is the master's year ("" when unknown); it is only used
    for a reissue. Afterwards the old folder, and parents that became empty,
    are removed (see prune_limit). Returns the (source, destination)
    list of moves made.
    """
    if album.layout_problem:
        raise AlbumApplyError(f"{album.path}: {album.layout_problem}\n  nothing was written")
    layout = layout_album(album, dest_root, release, dtracks, original_year)
    if layout.ambiguous:
        raise AlbumApplyError(f"{disc_warning(album, layout.ambiguous, layout.disc_conflicts)}\n  nothing was written")
    return execute_layout(layout, dry_run, clear_other, scan_root, journal_dir)


def regenerate_playlist(target_dir: Path, old_playlists: list[Path], journal: Journal | None = None) -> None:
    """Generated playlists go to the trash (never unlink), then a fresh one is written."""
    for playlist in old_playlists:
        try:
            if playlist.exists():
                _trash_logged(journal, playlist)
        except OSError as exc:
            print(f"  warning: could not trash old playlist {playlist}: {exc}")
    dest = playlist_path(target_dir)
    if dest.exists() and not is_generated_playlist(dest):
        print(f"  warning: not replacing hand-made playlist {dest}")
        return
    tracks = album_playlist_tracks(target_dir)
    if not tracks:
        return
    try:
        if journal is not None:
            journal.write(action="create-intent", path=str(dest))
        write_playlist_file(dest, render_playlist(target_dir, tracks))
        if journal is not None:
            journal.write(action="create", path=str(dest), sha256=file_sha256(dest))
        print(f"  playlist  {dest.name}  ({len(tracks)} tracks)")
    except OSError as exc:
        print(f"  warning: could not write playlist {dest}: {exc}")
