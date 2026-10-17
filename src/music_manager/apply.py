"""Apply a chosen release to an album: validate the layout, tag every file, then move everything.
"""

from __future__ import annotations

import os
from pathlib import Path

from .fs import AlbumFolder, file_sha256
from .journal import Journal
from .match import match_tracks
from .moves import PlannedFile, _mkdirs_logged, _prune_empty, _trash_logged, describe_moves, move_planned, prune_limit
from .naming import (
    album_folder_name,
    format_artist,
    format_catalog,
    reissue_original_year,
    track_filename,
    track_prefix,
    year4,
)
from .playlists import (
    PLAYLIST_EXTS,
    album_playlist_tracks,
    is_generated_playlist,
    playlist_path,
    render_playlist,
    write_playlist_file,
)
from .providers.discogs import DiscogsRelease, DiscogsTrack, release_artists, release_catno, release_discs
from .tags.fields import TrackMeta
from .tags.snapshot import capture_tags
from .tags.write import write_tags

# ---------------------------------------------------------------------------
# Applying a release: tag everything, then move everything
# ---------------------------------------------------------------------------

class AlbumApplyError(RuntimeError):
    """An album could not be fully applied. The message says what happened."""


def validate_layout(album: AlbumFolder, dest_root: Path, target_dir: Path) -> None:
    """Refuse layouts that would make an apply write into or through its own source.

    Raises AlbumApplyError (before anything is written) when the destination
    library or the album's target folder lies inside the source album, when the
    target folder contains the source album (the album would be emptied into
    one of its parents), or when a symlink in the album points outside it
    (tagging would modify a file elsewhere). Re-tagging in place (target is
    the album folder itself) is allowed.
    """
    src = album.path.resolve()
    target = target_dir.resolve()
    root = dest_root.resolve()
    if target != src:
        if root == src or root.is_relative_to(src):
            raise AlbumApplyError(f"refusing: destination {dest_root} is inside the source album {album.path}\n  nothing was changed")
        if target.is_relative_to(src):
            raise AlbumApplyError(f"refusing: target folder {target_dir} is inside the source album {album.path}\n  nothing was changed")
        if src.is_relative_to(target):
            raise AlbumApplyError(
                f"refusing: target folder {target_dir} contains the source album {album.path}\n  nothing was changed")
    escaping: list[str] = []
    for dirpath, dirnames, filenames in os.walk(album.path, followlinks=False):
        for name in dirnames + filenames:
            entry = Path(dirpath) / name
            if not entry.is_symlink():
                continue
            real = entry.resolve()
            if real != src and not real.is_relative_to(src):
                escaping.append(f"    {entry}  ->  {real}")
    if escaping:
        raise AlbumApplyError(
            "refusing: symlinks in the album point outside it (their targets would be re-tagged or moved):\n"
            + "\n".join(escaping) + "\n  nothing was changed")


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
    album_artist = release_artists(release)
    folder_artist = format_artist(album_artist)
    title = release.get("title") or ""
    release_year = year4(release.get("released") or release.get("year"))
    catno = release_catno(release)
    label = ""
    if release.get("labels"):
        label = release["labels"][0].get("name") or ""
    genres = release.get("genres") or []
    styles = release.get("styles") or []
    genre = ", ".join(styles[:2] or genres[:2])
    discs = release_discs(release, dtracks)
    master_id = release.get("master_id") or 0
    # Original date only for reissues (master year earlier than this release).
    orig = reissue_original_year(original_year, release_year)

    folder = album_folder_name(title, original_year, release_year, catno, discs)
    target_dir = dest_root / folder_artist / folder

    print(f"  dest  {target_dir}")
    validate_layout(album, dest_root, target_dir)

    pairs, legacy = match_tracks(album, dtracks)
    if legacy:
        print("  note  these files use the old vinyl numbering (B1 = 21); they are renumbered 1..N")

    by_disc: dict[int, list[DiscogsTrack]] = {}
    for dt in dtracks:
        by_disc.setdefault(dt.disc, []).append(dt)

    plan: list[PlannedFile] = []
    for local, dt in pairs:
        if dt is None:
            print(f"  skip  extra file  {local.name}  (left in place)")
            continue
        disc_tracks = by_disc.get(dt.disc, dtracks)
        meta: TrackMeta = {
            "artist": dt.artist or album_artist,
            "albumartist": album_artist,
            "album": title,
            "title": dt.title,
            "track": dt.number,
            "tracktotal": len(disc_tracks),
            "disc": dt.disc,
            "disctotal": discs,
            "date": str(release.get("released") or release_year or ""),
            "originaldate": orig,
            "label": label,
            "catalognumber": format_catalog(catno) or "",
            "genre": genre,
            "release_id": release.get("id"),
            "master_id": master_id,
            "position": dt.position,
        }
        dest_file = target_dir / track_filename(track_prefix(dt), dt.title, local.suffix)
        where = f"{dt.disc}-{dt.number}"
        if dt.position and dt.position != str(dt.number):
            where += f", {dt.position}"
        print(f"  {local.name}")
        print(f"      -> {dest_file.name}   [{where}]")
        plan.append(PlannedFile(local, dest_file, meta))

    clash: dict[str, list[Path]] = {}
    for item in plan:
        clash.setdefault(str(item.dest).casefold(), []).append(item.source)
    clashes = {k: v for k, v in clash.items() if len(v) > 1}
    if clashes:
        detail = "; ".join(", ".join(p.name for p in v) for v in clashes.values())
        print(f"  warning: several files map to the same name and will get (2) suffixes: {detail}")

    # Generated album playlists list the old file names. They are not moved;
    # they are deleted and a fresh one is written once the move succeeds.
    old_playlists: list[Path] = []
    sidecars: list[Path] = []
    for sidecar in album.sidecars:
        if sidecar.suffix.lower() in PLAYLIST_EXTS and is_generated_playlist(sidecar):
            old_playlists.append(sidecar)
        else:
            sidecars.append(sidecar)
            if sidecar.suffix.lower() in PLAYLIST_EXTS:
                print(f"  note  hand-made playlist {sidecar.name} is moved as-is; it may list old file names")

    # Phase 1: tags. Nothing moves unless every file was tagged.
    # A real run journals every change before doing it, so the whole apply can be undone.
    journal: Journal | None = None
    if not dry_run:
        journal = Journal("apply", journal_dir)
        journal.write(action="start", command="apply", album=str(album.path), dest=str(target_dir))
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
                print(f"      {'would clear' if dry_run else 'cleared'}  {', '.join(removed)}  ({item.source.name})")
            tagged.append(item.source)

        moves: list[PlannedFile] = list(plan)
        for extra_dir in album.extra_dirs:
            print(f"  move  {extra_dir.name}/")
            moves.append(PlannedFile(extra_dir, target_dir / extra_dir.name))
        for sidecar in sidecars:
            rel = sidecar.relative_to(album.path)
            print(f"  move  {rel}")
            moves.append(PlannedFile(sidecar, target_dir / rel))
        for playlist in old_playlists:
            print(f"  drop  {playlist.relative_to(album.path)}  (generated playlist; regenerated after the move)")

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

    # Phase 3: playlists and cleanup.
        if old_playlists:
            regenerate_playlist(target_dir, old_playlists, journal)
        if album.path.resolve() != target_dir.resolve():
            limit = prune_limit(album.path, dest_root, scan_root)
            for gone in _prune_empty(album.path, limit, protect=[dest_root, target_dir], journal=journal):
                print(f"  removed empty folder  {gone}")
        return done
    finally:
        if journal is not None:
            journal.close()
            print(f"journal: {journal.path}")
            print(f"undo:    discogs-tag --undo-journal '{journal.path}'   (preview; add --apply to undo)")


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
