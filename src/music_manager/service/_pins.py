"""Manual pairs (match.pin): stored per album and release, checked against the disc rules."""

from __future__ import annotations

from pathlib import Path

from ..fs import AlbumFolder
from ..match import (
    SubtrackDecision,
    _disc_from_folder,
    align_tracks_detailed,
    collapse_subtracks,
)
from ..providers.discogs import (
    DiscogsRelease,
    DiscogsTrack,
    release_artists,
    subtrack_groups,
)
from ._state import State
from ._types import Pin, PinConflict, Problem, ProblemKind

HELD_METHODS = ("ambiguous_disc", "track_count")  # what the engine refuses; a pin overrides it only when asked


def _stat(path: Path) -> tuple[int | None, int | None]:
    try:
        s = path.stat()
        return s.st_size, s.st_mtime_ns
    except OSError:
        return None, None


def pin_rows(st: State, album_id: int, release_id: int | None = None):
    sql, args = "SELECT * FROM pins WHERE album_id = ?", [album_id]
    if release_id is not None:
        sql, args = sql + " AND release_id = ?", [album_id, int(release_id)]
    return st.index.query(sql + " ORDER BY path", args)


def to_pin(row) -> Pin:
    path = Path(row["path"])
    return Pin(path, row["position"], int(row["release_id"]), bool(row["override_hold"]),
               _stat(path) != (row["size"], row["mtime_ns"]), group_positions(row["position"]))


GROUP_SEP = "+"  # a group pin's positions as stored: "2-9a+2-9b"


def group_positions(position: str) -> list[str]:
    return [p.strip() for p in str(position).split(GROUP_SEP)]


def track_index(position: str, dtracks: list[DiscogsTrack]) -> int | None:
    """The release track a position names: as Discogs lists it, or "<disc>-<number>" (or "<number>" on one disc)."""
    want = position.strip().casefold()
    if GROUP_SEP in want:  # a group pin: the merged track of exactly these subtracks
        parts = tuple(p.strip() for p in want.split(GROUP_SEP))
        return next((i for i, dt in enumerate(dtracks)
                     if dt.subtracks and tuple(p.casefold() for p in dt.subtracks) == parts), None)
    one_disc = len({dt.disc for dt in dtracks}) <= 1
    for i, dt in enumerate(dtracks):
        names = {(dt.position or "").casefold(), f"{dt.disc}-{dt.number}"}
        if one_disc:
            names.add(str(dt.number))
        if want in names:
            return i
    return None


def resolve(st: State, album_id: int, release_id: int, album: AlbumFolder, dtracks: list[DiscogsTrack],
            title_match_min: float) -> tuple[dict[Path, int], list[Problem], set[Path]]:
    """(usable pins: file -> track index, PIN_CONFLICT problems, files whose pin overrides a hold).

    A pin is not used (and is a problem) when its file changed or left the album, its position is
    not on the release, two pins name one track, or it would override a disc hold (the file's CD
    folder is another disc, or the engine refuses the file) without override_hold. Nothing else moves.
    """
    pins = [to_pin(r) for r in pin_rows(st, album_id, release_id)]
    if not pins:
        return {}, [], set()
    problems: list[Problem] = []
    multi = len({dt.disc for dt in dtracks}) > 1
    files = set(album.tracks)
    baseline = {p: how for p, _dt, how in align_tracks_detailed(album, dtracks, title_match_min)[0]}
    wanted: dict[int, list[Pin]] = {}

    def refuse(pin: Pin, reason: str, why: str) -> None:
        problems.append(Problem(ProblemKind.PIN_CONFLICT, f"Manual pair {pin.path.name} -> {pin.position}: {why}",
                                [pin.path], pin_conflicts=[PinConflict(pin.path, pin.position, reason)]))

    for pin in pins:
        i = track_index(pin.position, dtracks)
        if pin.stale or pin.path not in files:
            refuse(pin, "stale", "the file changed since it was paired; pair it again")
        elif i is None and GROUP_SEP in pin.position:
            refuse(pin, "not_subtrack_group", f"{', '.join(pin.positions)} are not one subtrack group of release "
                   f"{release_id}")
        elif i is None:
            refuse(pin, "unknown_position", f"release {release_id} has no track {pin.position}")
        else:
            wanted.setdefault(i, []).append(pin)
    usable: dict[Path, int] = {}
    overrides: set[Path] = set()
    for i, group in wanted.items():
        if len(group) > 1:
            for pin in group:
                refuse(pin, "duplicate_position", f"{len(group)} files are paired with track {pin.position}")
            continue
        pin = group[0]
        folder_disc = _disc_from_folder(pin.path, album.path) if multi else None
        wrong_folder = folder_disc is not None and folder_disc != dtracks[i].disc
        held = baseline.get(pin.path) in HELD_METHODS or wrong_folder
        if held and not pin.override_hold:
            if wrong_folder:
                refuse(pin, "disc_folder_mismatch", f"the file is in the disc {folder_disc} folder, the track is on "
                       f"disc {dtracks[i].disc}; pair it again with override_hold=True")
            else:
                refuse(pin, "override_hold_required", "this overrides a disc hold (the file's disc can't be told "
                       "or its disc's counts differ); pair it again with override_hold=True")
            continue
        usable[pin.path] = i
        if held:
            overrides.add(pin.path)
    return usable, problems, overrides


def release_tracks(st: State, album_id: int, release_id: int, album: AlbumFolder, release: DiscogsRelease,
                   title_match_min: float
                   ) -> tuple[list[DiscogsTrack], list[SubtrackDecision], dict[Path, int], list[Problem], set[Path]]:
    """The release's tracks for this album (subtracks merged, see match.collapse_subtracks; a group pin forces
    its group), then the album's pins against them: (tracks, subtrack decisions, usable pins, PIN_CONFLICT
    problems, files whose pin overrides a hold)."""
    rows = list(pin_rows(st, album_id, release_id))
    keys = {tuple(p.casefold() for p in g.positions) for g in subtrack_groups(release)} if rows else set()
    files = set(album.tracks)
    forced = []
    for row in rows:
        pin = to_pin(row)
        key = tuple(p.casefold() for p in pin.positions)
        if len(key) > 1 and key in keys and not pin.stale and pin.path in files:
            forced.append(tuple(pin.positions))
    dtracks, decisions = collapse_subtracks(release, release_artists(release), album, title_match_min, forced)
    usable, problems, overrides = resolve(st, album_id, release_id, album, dtracks, title_match_min)
    return dtracks, decisions, usable, problems, overrides
