"""Plain-English labels for contract Problems (AlbumPlan.problems / Destination.problems).

Works with either backend: kinds are compared by their string value (ProblemKind is a StrEnum).
Decisions (21:44 PT):
- Apply: any problem holds back that album only ("Needs a look"); clean albums still apply.
  track_mismatch can be overridden one album at a time ("Apply anyway…" + confirm); nothing else can.
- Undo: journal format is per operation, so legacy_journal (until trust_legacy=True) blocks the whole
  undo. Skipped steps stay in AlbumPlan.files with FileChange.skip_reason; the rest undoes.
"""
from __future__ import annotations

import re as _re
from pathlib import Path

LABELS = {
    "dest_inside_source": "New folder is inside the album",
    "source_inside_target": "Album is inside its new folder",
    "symlink_outside": "Link points outside the album",
    "collision": "Two things would get the same name",
    "track_mismatch": "Track count doesn't match the release",
    "file_changed": "Files changed since this was planned",
    "legacy_journal": "Older journal format: tag restore needs confirmation",
    "release_not_fetched": "Fetching from Discogs…",   # not an error, never holds an album
    "disc_ambiguous": "Can't work out the disc layout",   # multi-disc: paths = the files; never overridable
    "layout_ambiguous": "Loose tracks next to album folders",   # paths = [album folder]; never overridable
    "disc_folder_mismatch": "Tracks would go into another disc's folder",   # template issue; never overridable
}
NOT_FETCHED = "release_not_fetched"   # not held: apply leaves the album out and its status stays as it was
OVERRIDABLE = "track_mismatch"   # per album only, via plan.build(allow_track_mismatch={id}) after a confirm
LEGACY = "legacy_journal"
LAYOUT_AMBIGUOUS = "layout_ambiguous"    # 68cf4d7: loose audio next to disc/album folders; held, never overridden
DISC_FOLDER_MISMATCH = "disc_folder_mismatch"  # 2b82859: the naming template files a track into another disc's CD folder
DISC_AMBIGUOUS = "disc_ambiguous"    # holds the album; allow_track_mismatch / "Apply anyway…" don't touch it


def kind_of(problem) -> str:
    k = problem.kind
    return str(getattr(k, "value", k))


def label(problem) -> str:
    k = kind_of(problem)
    return LABELS.get(k, k.replace("_", " ").capitalize())


def waiting(ap) -> bool:
    """Apply: the album's release isn't stored yet (Plan.fetch_job downloads it); rebuild afterwards."""
    return any(kind_of(p) == NOT_FETCHED for p in ap.problems)


def held(ap) -> bool:
    """Apply: an album with any other problem is held back."""
    return bool(ap.problems) and not waiting(ap)


def overridable(ap) -> bool:
    """Apply: held only by track_mismatch, so "Apply anyway…" is offered."""
    return bool(ap.problems) and all(kind_of(p) == OVERRIDABLE for p in ap.problems)


def all_problems(plan):
    return [p for ap in plan.albums for p in ap.problems]


def one_line(problem) -> str:
    return f"{label(problem)}: {problem.message}"


# ---------------------------------------------------------------- FileChange.kind
# Undo plans at 0666aad report moved track files as kind "extra" (the kind is read from the old path, which
# no longer exists); Bassline's fix makes them "track". The UI accepts both and shows audio files as tracks.
AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".wma", ".ape", ".wv", ".aiff", ".aif",
              ".alac"}   # mirrors music_manager.fs.AUDIO_EXTS


def display_kind(fc) -> str:
    """'track' for kind "track", and for kind "extra" on an audio file; anything else as given."""
    kind = str(fc.kind)
    if kind == "extra":
        name = getattr(fc, "path_after", None) or getattr(fc, "path_before", None)
        if name is not None and getattr(name, "suffix", "").lower() in AUDIO_EXTS:
            return "track"
    return kind


# ---------------------------------------------------------------- Plan.warnings in the apply dialog
COVER_STEMS = ("cover", "folder", "front")      # mirrors music_manager.service.plan.COVER_STEMS
COVER_EXTS = (".jpg", ".jpeg", ".png")
_COVER_RE = _re.compile(r"^(?P<head>.+ will be saved from Discogs for )(?P<n>\d+)(?P<tail> album\(s\).*)$")
_REFUSED_RE = _re.compile(r"^\d+ album\(s\) have problems and will be refused")
_FETCH_RE = _re.compile(r"^\d+ release\(s\) (are being fetched|could not be fetched) from Discogs")


def has_cover(folder) -> bool:
    """A cover image already in the album folder (the backend then saves no folder.jpg). Read-only."""
    try:
        return any(p.suffix.lower() in COVER_EXTS and p.stem.casefold() in COVER_STEMS for p in folder.iterdir())
    except (OSError, AttributeError, TypeError):
        return False


def apply_warnings(plan, clean, held_aps, waiting_aps, cover_check=has_cover) -> list[str]:
    """Plan.warnings as the apply dialog shows them.

    - "N album(s) have problems and will be refused (held)" is dropped when the held-back box lists them.
    - "N release(s) are being fetched / could not be fetched" is dropped when the fetching box lists them.
    - per-album counts ("folder.jpg will be saved … for N album(s)") count only albums that apply now: the
      real service counts held albums too. Albums without a cover yet are the backend's candidates; when
      that matches N, the clean ones among them are counted, otherwise N is capped at the clean count.
      A count that drops to 0 drops the line.
    """
    out = []
    clean_ids = {ap.album_id for ap in clean}
    for w in plan.warnings:
        if held_aps and _REFUSED_RE.match(w):
            continue
        if waiting_aps and _FETCH_RE.match(w):
            continue
        m = _COVER_RE.match(w)
        if m and (held_aps or waiting_aps):
            n = int(m["n"])
            eligible = [ap for ap in plan.albums if not waiting(ap) and not cover_check(ap.folder_before)]
            if len(eligible) == n:
                n = sum(1 for ap in eligible if ap.album_id in clean_ids)
            else:
                n = min(n, len(clean))
            if not n:
                continue
            w = f"{m['head']}{n}{m['tail']}"
        out.append(w)
    return out


# ---------------------------------------------------------------- apply results
def nothing_applied(message: str | None) -> bool:
    """Job.message of an apply that applied nothing: "[MMError: ]Applied 0 album(s); refused N …"."""
    return bool(message) and _re.search(r"\bApplied 0 album", message) is not None


def refused_count(message: str | None) -> int:
    m = _re.search(r"refused (\d+)", message or "")
    return int(m.group(1)) if m else 0


# ---------------------------------------------------------------- album names in plans (Bassline e1eca5e)
NOT_IN_INDEX = "Album not in the library index"
_GENERIC_NAME = _re.compile(r"^Album \d+$")


def album_label(ap, fallback=None, undo: bool = False, row=None) -> str:
    """Name for an AlbumPlan in the apply dialog / undo preview.

    1. AlbumPlan.artist / title (e1eca5e: the release's, from plan.build or the apply's journal)
    2. the library index, via fallback(album_id), when album_id is set and found
    3. the album folder's name (+ "Discogs release N" when release_id is known)
    A layout_ambiguous album is named from the library row (row(album_id), see display_names) or its folder.
    Older services have no artist/title fields; older journals give album_id/release_id 0 and "" names."""
    lp = None if undo else layout_problem(getattr(ap, "problems", None))
    if lp is not None:      # layout_ambiguous: the release's names would mislead; name the folder (+ hint)
        r = None
        if row is not None and getattr(ap, "album_id", 0):
            try:
                r = row(ap.album_id)
            except Exception:
                r = None
        if r is not None:
            a, t = display_names(r, lp)
            return f"{a} – {t}" if a else t
        fname = getattr(getattr(ap, "folder_before", None), "name", "") or "Album"
        return f"{fname}/ ({layout_hint(lp)})"
    artist = str(getattr(ap, "artist", "") or "").strip()
    title = str(getattr(ap, "title", "") or "").strip()
    if artist and title:
        return f"{artist} – {title}"
    if artist or title:
        return artist or title
    aid = getattr(ap, "album_id", 0) or 0
    if aid and fallback is not None:
        try:
            name = fallback(aid)
        except Exception:
            name = ""
        if name and name != NOT_IN_INDEX and not _GENERIC_NAME.match(name):
            return name
    folder = getattr(ap, "folder_after" if undo else "folder_before", None)
    fname = getattr(folder, "name", "") or ""
    rid = getattr(ap, "release_id", 0) or 0
    if fname and fname != "/":
        return f"{fname} (Discogs release {rid})" if rid else fname
    return f"Discogs release {rid}" if rid else NOT_IN_INDEX


def release_note(ap) -> str:
    rid = getattr(ap, "release_id", 0) or 0
    return f"Discogs release {rid}" if rid else "Discogs release unknown (older journal)"


def blocking_reasons(ap) -> list[str]:
    """Labels of the problems that "Apply anyway…" can't override (everything but track_mismatch)."""
    return [label(p) for p in ap.problems if kind_of(p) != OVERRIDABLE]


def shown_paths(problem, folder) -> list[str]:
    """Problem.paths for display: relative to the album folder when inside it (disc_ambiguous lists files)."""
    out = []
    for x in problem.paths:
        try:
            out.append(str(x.relative_to(folder)) if folder is not None and x != folder else str(x))
        except (ValueError, AttributeError, TypeError):
            out.append(str(x))
    return out



# ---------------------------------------------------------------- layout_ambiguous (68cf4d7, contract "Album
# folders with loose audio and subfolders"). Problem.paths is [album folder]; since ccfa4a8 the detail is
# structured: layout_reason ("disc_folders" | "album_folders"), blocking_subdirs and loose_files. The message is
# shown as text only; nothing is read out of it. An older service leaves the fields empty: then the shape and
# the loose files come from the album's own files, and the subfolders are only named by the message itself.
LOOSE_TITLE = "Loose tracks, not an album"


def _plist(value) -> list[Path]:
    return [Path(str(x)) for x in (value or [])]


def _folder_of(problem):
    return Path(str(problem.paths[0])) if getattr(problem, "paths", None) else None


def layout_problem(problems):
    """The LAYOUT_AMBIGUOUS problem in a list of problems (AlbumPlan.problems, held_for, ...), or None."""
    return next((p for p in problems or [] if kind_of(p) == LAYOUT_AMBIGUOUS), None)


def layout_shape(problem, files=None) -> str:
    """"discs": loose audio next to CD1/CD2 folders (one album); "folders": next to other album folders.
    From Problem.layout_reason; without it (older service), "discs" when some of the album's audio is in a
    subfolder (the disc-folder album holds every file; the other kind only its loose files)."""
    reason = str(getattr(problem, "layout_reason", "") or "")
    if reason in ("disc_folders", "album_folders"):
        return "discs" if reason == "disc_folders" else "folders"
    folder = _folder_of(problem)
    if folder is not None and any(Path(str(f)).parent != folder for f in files or []):
        return "discs"
    return "folders"


def layout_hint(problem, files=None) -> str:
    return ("loose tracks next to disc folders" if layout_shape(problem, files) == "discs"
            else "loose tracks next to album folders")


def layout_dirs(problem) -> list[str]:
    """Problem.blocking_subdirs as names relative to the album folder ("CD1", "[1984]  My War [SST023]");
    [] when the service doesn't send them (the message still names them)."""
    folder = _folder_of(problem)
    out = []
    for d in _plist(getattr(problem, "blocking_subdirs", None)):
        try:
            out.append(d.relative_to(folder).as_posix() if folder is not None else d.name)
        except ValueError:
            out.append(str(d))
    return out


def loose_files(problem, files=None) -> list[Path]:
    """Problem.loose_files; without it, the album's files that sit directly in the album folder."""
    given = _plist(getattr(problem, "loose_files", None))
    if given:
        return given
    folder = _folder_of(problem)
    return [Path(str(f)) for f in files or [] if folder is None or Path(str(f)).parent == folder]


def is_loose_container(row, problem) -> bool:
    """The scan made a not-an-album folder into an "album" of its loose files (an artist folder with a stray
    track): folder-shaped layout problem, and the scan's names are just the folder's and its parent's."""
    if problem is None or layout_shape(problem) != "folders":
        return False
    folder = Path(str(getattr(row, "folder", "") or ""))
    title = str(getattr(row, "title", "") or "").strip()
    artist = str(getattr(row, "artist", "") or "").strip()
    return title in ("", folder.name) and artist in ("", folder.parent.name)


def display_names(row, problem=None, parent=None) -> tuple[str, str]:
    """(artist, album) for the library and review lists.
    - held loose-files "album" in an artist folder: (folder name, "Loose tracks, not an album")
    - other layout_ambiguous albums: their names + " · loose tracks next to … folders"
    - a folder with no names of its own (e.g. Bonus/ listed as its own album): "<parent album> / Bonus" with
      the parent's artist when the parent is known (parent = (row, problem) of a held folder above it)."""
    folder = Path(str(getattr(row, "folder", "") or ""))
    artist = str(getattr(row, "artist", "") or "").strip()
    title = str(getattr(row, "title", "") or "").strip()
    if problem is not None:
        if is_loose_container(row, problem):
            return folder.name, LOOSE_TITLE
        return artist, f"{title} · {layout_hint(problem)}"
    if not artist and title in ("", folder.name):
        if parent is not None and not is_loose_container(*parent):
            prow = parent[0]
            return (str(getattr(prow, "artist", "") or ""),
                    f"{str(getattr(prow, 'title', '') or folder.parent.name)} / {folder.name}")
        return "", f"{folder.parent.name}/{folder.name}"
    return artist, title


def layout_note(row, parent) -> str:
    """Tooltip line for an album listed on its own because the folder above it has loose tracks."""
    if parent is None:
        return ""
    return (f"Listed as its own album: the folder above it, {Path(str(parent[0].folder)).name}/, has loose tracks "
            "next to album folders and is held (move the files on disk and rescan)")


def layout_files(problem, folder, files) -> list[str]:
    """Lines naming the files involved: the loose ones (relative to the album folder), the album's files in
    its disc folders (disc_folders), then the subfolders that caused the hold. files: the album's audio
    (AlbumPlan.files' path_before, or Album.tracks' paths), used for the disc folders' files and as the
    fallback when the problem has no loose_files."""
    folder = Path(str(folder)) if folder is not None else _folder_of(problem)

    def rel(f):
        f = Path(str(f))
        try:
            return f.relative_to(folder).as_posix() if folder is not None else f.name
        except ValueError:
            return str(f)
    loose = [rel(f) for f in loose_files(problem, files)]
    shape = layout_shape(problem, files)
    inside = [rel(f) for f in files or [] if "/" in rel(f)] if shape == "discs" else []
    dirs = layout_dirs(problem)
    lines = [f"Loose files ({len(loose)}):"] + [f"  {r}" for r in loose]
    if inside:          # disc_folders: the disc folders' files are part of this album too
        lines += [f"In the subfolders ({len(inside)}):"] + [f"  {r}" for r in inside]
    if dirs:
        lines += [f"Next to {'disc folders' if shape == 'discs' else 'album folders'} ({len(dirs)}):"] + [
            f"  {d}/" for d in dirs]
    return lines


# ---------------------------------------------------------------- disc_ambiguous detail (ccfa4a8, contract
# "Disc conflicts"): Problem.disc_conflicts has one DiscConflict per refused file (same files as paths).
DISC_REASONS = {
    "numbering": "the folder's track numbers don't restart on each disc the way the release's do "
                 "(a file is missing or extra), so they can't say which disc it is",
    "no_title_match": "its title doesn't match any of the release's tracks with this number",
    "several_title_matches": "its title matches more than one of the release's tracks with this number",
    "same_track": "another file with the same number matches the same track",
    "held_with_group": "its title fits one track, but another file with the same number can't be placed, "
                       "so the files with this number wait together",
    "sources_disagree": "its disc folder, disc tag and cue/m3u say different discs",
}


def disc_reason(conflict) -> str:
    r = str(getattr(conflict, "reason", "") or "")
    return DISC_REASONS.get(r, r.replace("_", " "))


def choice_text(c) -> str:
    """'2-1 “Forbidden Jimmy” (21% title match)'."""
    return f"{c.position} “{c.title}” ({c.title_similarity:.0%} title match)"


def _disc(value) -> str:
    return "nothing" if value is None else f"disc {value}"


def sources_text(c) -> str:
    """What each disc source says (sources_disagree)."""
    if c.sidecar is not None:
        side = f"{Path(str(c.sidecar)).name} says {_disc(c.sidecar_disc)}" if c.sidecar_disc is not None else \
            f"the cue/m3u files listing it ({Path(str(c.sidecar)).name}, …) disagree"
    else:
        side = "no cue/m3u lists it"
    return f"folder says {_disc(c.folder_disc)}; disc tag says {_disc(c.tag_disc)}; {side}"


def disc_conflict_detail(c) -> list[str]:
    """The choices for one file, after its reason line."""
    out = []
    if c.reason == "sources_disagree":
        out.append(sources_text(c))
    if c.same_number:
        out.append(f"Track {c.number} on each disc: " + "; ".join(choice_text(x) for x in c.same_number))
    if c.title_matches:
        out.append("Its title matches: " + "; ".join(choice_text(x) for x in c.title_matches))
    elif c.reason != "sources_disagree":
        out.append("Its title matches no track on the release")
    return out


def disc_conflict_lines(problem, folder) -> list[str]:
    """Lines for the files involved in a disc_ambiguous problem: per file the reason in plain words and the
    disc/track choices with their title similarity. Without disc_conflicts (older service), just the files."""
    conflicts = list(getattr(problem, "disc_conflicts", None) or [])
    if not conflicts:
        files = shown_paths(problem, folder)
        return [f"Files ({len(files)}):"] + [f"  {x}" for x in files]
    names = dict(zip((Path(str(x)) for x in problem.paths), shown_paths(problem, folder)))
    lines = [f"Files ({len(conflicts)}):"]
    for c in conflicts:
        path = Path(str(c.path))
        name = names.get(path) or shown_paths(type("P", (), {"paths": [path]})(), folder)[0]
        lines.append(f"  {name} — {disc_reason(c)}")
        lines += [f"      {x}" for x in disc_conflict_detail(c)]
    return lines


def disc_conflict_for(problem, path):
    """The DiscConflict for one file, or None."""
    return next((c for c in getattr(problem, "disc_conflicts", None) or [] if Path(str(c.path)) == Path(str(path))),
                None)



# ---------------------------------------------------------------- disc_folder_mismatch (2b82859, contract "Behaviour
# changes of 2026-10-18" item 3): Problem.folder_mismatches has one DiscFolderMismatch per file (same files as
# paths). The fix is the naming template, so the UI offers the naming settings.
NAMING_HINT = "Change the naming template so each disc's tracks go into that disc's folder."


def has_folder_mismatch(problems) -> bool:
    return any(kind_of(p) == DISC_FOLDER_MISMATCH for p in problems or [])


def folder_mismatch_text(m, folder=None) -> str:
    """'13 Mason Jar.flac → CD2/ (disc tag 1)' (the file relative to the album folder when inside it)."""
    path = Path(str(m.path))
    name = path.name
    if folder is not None:
        try:
            name = path.relative_to(Path(str(folder))).as_posix()
        except ValueError:
            pass
    return f"{name} → {Path(str(m.folder)).name}/ (disc tag {m.tag_disc})"


def folder_mismatch_lines(problem, folder) -> list[str]:
    """Files involved in a disc_folder_mismatch, one line each, then what to do. Without folder_mismatches
    (older service), just the files."""
    ms = list(getattr(problem, "folder_mismatches", None) or [])
    if not ms:
        files = shown_paths(problem, folder)
        return [f"Files ({len(files)}):"] + [f"  {x}" for x in files] + [NAMING_HINT]
    return [f"Files ({len(ms)}):"] + [f"  {folder_mismatch_text(m, folder)}" for m in ms] + [NAMING_HINT]
