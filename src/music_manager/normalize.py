"""Library cleanup: --normalize-folders.

Renames album folders made by older versions ("[1984]  My War [1984]
[SST023]") to the current rule ("[1984]  My War [SST023]") without
calling Discogs and without touching any file's tags. Preview by default;
--apply renames and records every rename in a JSON-lines journal that
--undo-journal can reverse.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .fs import AlbumFolder, collect_albums
from .journal import Journal
from .naming import YEAR_RE, sanitize_filename, year4
from .playlists import is_generated_playlist, playlists_in
from .tags.read import read_tag_years

# Exactly the shape this tool writes: "[Y1]  Title[ NCD] [Y2][ [CAT]]",
# two spaces after the leading bracket and single spaces elsewhere.
OLD_FOLDER = re.compile(
    r"^\[(?P<y1>\d{4}|)\]  (?P<title>[^\[\]]+?)(?P<discs> \d+CD)?"
    r"(?P<y2seg> \[(?P<y2>\d{4}|)\])(?P<cat> \[[^\[\]]+\])?$"
)


# Already in the current shape: "[Y]  Title[ NCD][ [CAT]]".
NEW_FOLDER = re.compile(r"^\[\d{4}\]  [^\[\]]+?(?: \d+CD)?(?: \[[^\[\]]+\])?$")


@dataclass
class FolderPlan:
    """Result of plan_folder_rename: action is "rename", "ok" or "skip"."""
    action: str
    new_name: str = ""
    reason: str = ""


def plan_folder_rename(name: str, tag_years: tuple[str, str] | None = None) -> FolderPlan:
    """Decide the new name of one album folder from its name alone.

    tag_years is (DATE year, ORIGINALDATE/ORIGINALYEAR year) read from the
    album's files, or None when not read. Pure function: no file access.
    """
    match = OLD_FOLDER.match(name)
    if not match:
        if NEW_FOLDER.match(name):
            return FolderPlan("ok")
        return FolderPlan("skip", reason="not in the '[YEAR]  Album [YEAR] [CATNO]' shape")
    y1, y2 = match.group("y1"), match.group("y2")
    cat = match.group("cat") or ""
    if YEAR_RE.fullmatch(cat.strip(" []")):
        return FolderPlan("skip", reason="catalog number looks like a year (ambiguous)")
    if y1 and y2 and y1 < y2:
        return FolderPlan("ok")  # a reissue: both years stay
    if y1 and y2 and y1 > y2:
        return FolderPlan("skip", reason=f"leading year {y1} is later than release year {y2}")
    if bool(y1) != bool(y2):
        return FolderPlan("skip", reason="only one of the two years is filled in")
    if y1 and tag_years:
        date, original = (year4(v) for v in tag_years)
        if date and original and original < date:
            return FolderPlan(
                "skip",
                reason=f"tags say reissue (original {original}, release {date}) but folder years are equal",
            )
    if y1:
        new = name[: match.start("y2seg")] + name[match.end("y2seg"):]
    else:
        # "[]  Album []" from an old version with no year at all.
        new = match.group("title") + (match.group("discs") or "") + cat
    if not new.strip() or new != sanitize_filename(new):
        return FolderPlan("skip", reason="new name would not be a clean folder name")
    return FolderPlan("rename", new_name=new)


def album_tag_years(album: AlbumFolder) -> tuple[str, str] | None:
    """Years from the album's first track that has any, or None."""
    for track in album.tracks:
        date, original = read_tag_years(track)
        if date or original:
            return date, original
    return None


@dataclass
class FolderRename:
    old: Path
    new: Path
    playlists: list[tuple[Path, Path]] = field(default_factory=list)


def plan_library_normalization(root: Path) -> tuple[list[FolderRename], list[tuple[Path, str]], int]:
    """(renames, skipped with reasons, number already fine) for a library."""
    renames: list[FolderRename] = []
    skipped: list[tuple[Path, str]] = []
    fine = 0
    for album in collect_albums(root):
        name = album.path.name
        if album.layout_problem:
            skipped.append((album.path, album.layout_problem))
            continue
        plan = plan_folder_rename(name)
        if plan.action == "rename" and OLD_FOLDER.match(name).group("y1"):
            plan = plan_folder_rename(name, album_tag_years(album))
        if plan.action == "ok":
            fine += 1
            continue
        if plan.action == "skip":
            skipped.append((album.path, plan.reason))
            continue
        new_dir = album.path.with_name(plan.new_name)
        playlists = []
        for playlist in playlists_in(album.path):
            if playlist.stem == name and is_generated_playlist(playlist):
                playlists.append((new_dir / playlist.name, new_dir / f"{plan.new_name}{playlist.suffix}"))
        renames.append(FolderRename(album.path, new_dir, playlists))

    # Never merge or overwrite: an existing target, or two folders that
    # would get the same name, are conflicts and are left alone. (The
    # existence check follows the drive's own case rules.)
    targets: dict[str, int] = {}
    for item in renames:
        targets[str(item.new)] = targets.get(str(item.new), 0) + 1
    ok: list[FolderRename] = []
    for item in renames:
        if os.path.lexists(item.new):
            skipped.append((item.old, f"conflict: {item.new.name!r} already exists"))
        elif targets[str(item.new)] > 1:
            skipped.append((item.old, f"conflict: another folder would also become {item.new.name!r}"))
        else:
            ok.append(item)
    return ok, skipped, fine


def _rename_logged(journal: Journal, kind: str, old: Path, new: Path) -> None:
    if os.path.lexists(new):
        raise FileExistsError(f"{new} already exists")
    os.rename(old, new)
    st = os.lstat(new)
    journal.write(action="rename", kind=kind, old=str(old), new=str(new), dev=st.st_dev, ino=st.st_ino)


def normalize_folders(root: Path, apply: bool, journal_dir: Path | None = None) -> int:
    root = root.resolve()
    if not root.is_dir():
        print(f"not a folder: {root}", file=sys.stderr)
        return 2
    renames, skipped, fine = plan_library_normalization(root)
    mode = "APPLY" if apply else "PREVIEW (nothing is renamed; add --apply to rename)"
    print(f"normalize album folders under {root}\n{mode}\n")
    for item in renames:
        print(f"  {item.old}\n    -> {item.new.name}")
        for old_pl, new_pl in item.playlists:
            print(f"       playlist {old_pl.name} -> {new_pl.name}")
    if skipped:
        print("\nskipped (left as they are):")
        for path, reason in skipped:
            print(f"  {path}\n    {reason}")
    conflicts = sum(1 for _, reason in skipped if reason.startswith("conflict"))
    print(
        f"\n{len(renames)} to rename, {fine} already fine, "
        f"{len(skipped) - conflicts} skipped, {conflicts} conflicts"
    )
    if not apply or not renames:
        return 0

    journal = Journal("normalize-folders", journal_dir)
    journal.write(action="start", command="normalize-folders", root=str(root))
    done = failed = 0
    try:
        for item in renames:
            try:
                _rename_logged(journal, "folder", item.old, item.new)
            except OSError as exc:
                failed += 1
                print(f"  FAILED {item.old}: {exc}")
                continue
            done += 1
            for old_pl, new_pl in item.playlists:
                try:
                    _rename_logged(journal, "playlist", old_pl, new_pl)
                except OSError as exc:
                    print(f"  warning: playlist not renamed: {old_pl}: {exc}")
    finally:
        journal.close()
    print(f"\nrenamed {done} folders, {failed} failed")
    print(f"journal: {journal.path}")
    print(f"undo:    discogs-tag --undo-journal '{journal.path}' --apply")
    return 1 if failed else 0
