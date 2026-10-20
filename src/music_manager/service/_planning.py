"""Where albums would go: template resolution, per-album layouts and their problems. Read-only."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from ..apply import (
    AlbumLayout,
    check_layout,
    disc_folder_message,
    disc_folder_mismatches,
    disc_warning,
    layout_album,
    release_info,
    template_paths,
    template_sets_disc_path,
    track_count_message,
)
from ..fs import AlbumFolder, is_audio
from ..naming.templates import (
    PRESETS,
    CompiledTemplate,
    TemplateError,
    TemplateStructureError,
    compile_template,
)
from ..providers.discogs import DiscogsRelease, DiscogsTrack
from ._common import (
    album_folder,
    album_record,
    cluster_guess,
    root_for,
    title_match_min,
)
from ._releases import (
    album_tracks,
    candidate_row,
    candidate_rows,
    release_for,
    release_from_search,
)
from ._state import State, state
from ._types import (
    ConfigError,
    MMError,
    NotFound,
    Problem,
    ProblemKind,
    Template,
    ValidationError,
)

ARCHIVIST_TEXT = PRESETS[0][2]
LAYOUT_KIND = {
    "dest_inside_source": ProblemKind.DEST_INSIDE_SOURCE,
    "source_inside_target": ProblemKind.SOURCE_INSIDE_TARGET,
    "symlink_outside": ProblemKind.SYMLINK_OUTSIDE,
}


# --- templates ------------------------------------------------------------------

def builtin_templates() -> list[Template]:
    return [Template(pid, name, text, True) for pid, name, text in PRESETS]


def all_templates(st: State | None = None) -> list[Template]:
    st = st or state()
    users = [Template(tid, name, text, False) for tid, (name, text) in st.config().templates.items()]
    return builtin_templates() + users


def template_text(template_id: str | None, st: State | None = None) -> str:
    """Text of a Template.id; None = Settings.active_template. Unknown id: NotFound."""
    st = st or state()
    tid = st.config().active_template if template_id is None else template_id
    for t in all_templates(st):
        if t.id == tid:
            return t.text
    raise NotFound(f"template {tid!r}")


def compile_text(text: str, structure: bool = True) -> CompiledTemplate | None:
    """Compile template text; the archivist preset's text is today's layout exactly (None).

    Raises ValidationError with the offending field.
    """
    try:
        compiled = compile_template(text)
    except TemplateStructureError as exc:
        if structure:
            raise ValidationError(str(exc), field=exc.field) from exc
        raise
    except TemplateError as exc:
        raise ValidationError(str(exc), field=exc.field) from exc
    if text == ARCHIVIST_TEXT:
        return None
    return compiled


def resolve_template(template_id: str | None, st: State | None = None) -> CompiledTemplate | None:
    return compile_text(template_text(template_id, st))


# --- layouts --------------------------------------------------------------------

@dataclass
class AlbumTarget:
    album_id: int
    release_id: int
    root: Path
    album: AlbumFolder
    target: Path
    release: DiscogsRelease
    fetched: bool
    dtracks: list[DiscogsTrack]
    original_year: str
    layout: AlbumLayout | None = None  # None until the full release is known
    problems: list[Problem] = field(default_factory=list)
    label_missing: bool = False

    @property
    def files(self) -> list[tuple[Path, Path]]:
        if self.layout is None:
            return []
        return [(item.source, item.dest) for item in self.layout.tracks]


def library_root(folder: Path, st: State) -> Path:
    root = root_for(folder, st)
    if root is not None:
        return root
    roots = st.config().library_roots
    if not roots:
        raise ConfigError("No library root configured: settings.set(library_roots=[...]) first")
    return roots[0]


def chosen_release_id(album_id: int, st: State) -> int | None:
    row = album_record(album_id, st)
    if row["chosen_release"]:
        try:
            candidate_row(album_id, row["chosen_release"], st)
            return int(row["chosen_release"])
        except NotFound:
            pass
    rows = candidate_rows(album_id, st)
    if not rows:
        return None
    from ._releases import build_candidates

    cands = build_candidates(album_id, st)
    return cands[0].release_id if cands else int(rows[0]["release_id"])


def album_target(album_id: int, release_id: int | None, template: CompiledTemplate | None, *,
                 st: State | None = None, fetch: bool = False, need_release: bool = False) -> AlbumTarget:
    """Layout of one album for one release (the chosen one, else the best candidate). Read-only on disk.

    With fetch, a release that is not fetched yet (or older than 6 hours) is
    fetched now; otherwise only the folder is known (from the search result).
    """
    st = st or state()
    row = album_record(album_id, st)
    if release_id is None:
        release_id = chosen_release_id(album_id, st)
        if release_id is None:
            raise MMError(f"album {album_id} has no chosen release and no candidates: run match.find first")
    release, original_year, search = release_for(album_id, release_id, st, fetch=fetch)
    fetched = release is not None
    if release is None:
        if need_release:
            raise MMError(f"release {release_id} for album {album_id} is not fetched yet")
        release = release_from_search(search)
    folder = Path(row["folder"])
    root = library_root(folder, st)
    album = album_folder(album_id, st)
    dtracks = album_tracks(release) if fetched else []
    side_names = not st.config().sequential_sides
    target, namer = template_paths(template, root, release, dtracks, original_year, side_names)
    info = release_info(release, dtracks, original_year)
    result = AlbumTarget(album_id, int(release_id), root, album, target, release, fetched, dtracks, original_year,
                         label_missing=not info.label.strip())
    if album.layout_problem:
        lc = album.layout_conflict
        result.problems.append(Problem(ProblemKind.LAYOUT_AMBIGUOUS, album.layout_problem, [album.path],
                                       layout_reason=lc.reason if lc else "",
                                       blocking_subdirs=list(lc.subfolders) if lc else [],
                                       loose_files=list(lc.loose_files) if lc else []))
    for kind, message, paths in check_layout(album, root, target):
        if kind == "dest_inside_source" and paths == [target] and cluster_guess(row):
            continue  # a cluster moves only its own files, so its target may be a subfolder of theirs
        result.problems.append(Problem(LAYOUT_KIND[kind], message, list(paths)))
    if fetched:
        layout = layout_album(album, root, release, dtracks, original_year, target_dir=target, file_name=namer,
                              check=False, log=lambda _line: None, disc_folders=not template_sets_disc_path(template),
                              title_match_min=title_match_min())
        result.layout = layout
        result.problems += _album_problems(result, row["track_count"])
    result.problems += _occupied(result, st)
    return result


def _album_problems(t: AlbumTarget, track_count: int) -> list[Problem]:
    problems = []
    layout = t.layout
    assert layout is not None
    if len(t.dtracks) != track_count:
        problems.append(Problem(ProblemKind.TRACK_MISMATCH,
                                f"Discogs lists {len(t.dtracks)} tracks, the folder has {track_count}", [t.album.path]))
    if layout.ambiguous:
        problems.append(Problem(ProblemKind.DISC_AMBIGUOUS, disc_warning(t.album, layout.ambiguous, layout.disc_conflicts),
                                list(layout.ambiguous), disc_conflicts=list(layout.disc_conflicts)))
    if layout.count_holds:
        problems.append(Problem(ProblemKind.DISC_TRACK_COUNT, track_count_message(t.album, layout.count_holds),
                                [p for hold in layout.count_holds for p in hold.paths]))
    wrong = disc_folder_mismatches(layout)
    if wrong:
        problems.append(Problem(ProblemKind.DISC_FOLDER_MISMATCH,
                                disc_folder_message(t.album, wrong, "the destination puts tracks into"),
                                [m.path for m in wrong], folder_mismatches=wrong))
    seen: dict[str, list[Path]] = {}
    for item in [*layout.tracks, *layout.moves]:
        seen.setdefault(str(item.dest).casefold(), []).append(item.source)
    for sources in seen.values():
        if len(sources) > 1:
            dest = next(i.dest for i in [*layout.tracks, *layout.moves] if i.source == sources[0])
            problems.append(Problem(ProblemKind.COLLISION,
                                    f"{len(sources)} files would get the same name: {dest.name}", [dest, *sources]))
    sources = {item.source for item in [*layout.tracks, *layout.moves]}
    for item in [*layout.tracks, *layout.moves]:
        if item.dest == item.source or item.dest in sources:
            continue
        if os.path.lexists(item.dest):
            problems.append(Problem(ProblemKind.COLLISION,
                                    f"{item.dest} already exists and would not be overwritten", [item.dest]))
    return problems


def _occupied(t: AlbumTarget, st: State) -> list[Problem]:
    """Another album already lives in (or holds audio inside) the target folder."""
    if t.target.resolve() == t.album.path.resolve():
        return []
    problems = []
    others = st.index.query("SELECT id, folder, artist, title FROM albums WHERE folder = ? AND id != ?",
                            (str(t.target), t.album_id))
    for other in others:
        problems.append(Problem(ProblemKind.COLLISION,
                                f"Collision with another album: {other['artist']} – {other['title']} ({t.target})",
                                [t.target]))
    if not others and t.target.is_dir():
        mine = {p.resolve() for p in t.album.tracks}
        try:
            foreign = [p for p in t.target.iterdir() if p.is_file() and is_audio(p) and p.resolve() not in mine]
        except OSError:
            foreign = []
        if foreign:
            problems.append(Problem(ProblemKind.COLLISION,
                                    f"{t.target} already holds {len(foreign)} other audio file(s)", [t.target]))
    return problems


def cross_album_collisions(targets: list[AlbumTarget]) -> dict[int, list[Problem]]:
    """Two albums of one plan or preview that would land in the same folder."""
    by_folder: dict[str, list[AlbumTarget]] = {}
    for t in targets:
        by_folder.setdefault(str(t.target).casefold(), []).append(t)
    out: dict[int, list[Problem]] = {}
    for group in by_folder.values():
        if len({t.album_id for t in group}) < 2:
            continue
        for t in group:
            others = ", ".join(str(o.album.path) for o in group if o.album_id != t.album_id)
            out.setdefault(t.album_id, []).append(Problem(
                ProblemKind.COLLISION, f"{t.target} is also the target of {others}", [t.target]))
    return out
