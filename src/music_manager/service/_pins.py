"""Manual pairs (match.pin): stored per album and release, checked against the disc rules."""

from __future__ import annotations

from pathlib import Path

from ..fs import AlbumFolder
from ..match import _disc_from_folder, align_tracks_detailed
from ..providers.discogs import DiscogsTrack
from ._state import State
from ._types import Pin, Problem, ProblemKind

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
               _stat(path) != (row["size"], row["mtime_ns"]))


def track_index(position: str, dtracks: list[DiscogsTrack]) -> int | None:
    """The release track a position names: as Discogs lists it, or "<disc>-<number>" (or "<number>" on one disc)."""
    want = position.strip().casefold()
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

    def refuse(pin: Pin, why: str) -> None:
        problems.append(Problem(ProblemKind.PIN_CONFLICT, f"Manual pair {pin.path.name} -> {pin.position}: {why}",
                                [pin.path]))

    for pin in pins:
        i = track_index(pin.position, dtracks)
        if pin.stale or pin.path not in files:
            refuse(pin, "the file changed since it was paired; pair it again")
        elif i is None:
            refuse(pin, f"release {release_id} has no track {pin.position}")
        else:
            wanted.setdefault(i, []).append(pin)
    usable: dict[Path, int] = {}
    overrides: set[Path] = set()
    for i, group in wanted.items():
        if len(group) > 1:
            for pin in group:
                refuse(pin, f"{len(group)} files are paired with track {pin.position}")
            continue
        pin = group[0]
        folder_disc = _disc_from_folder(pin.path, album.path) if multi else None
        held = baseline.get(pin.path) in HELD_METHODS or (folder_disc is not None and folder_disc != dtracks[i].disc)
        if held and not pin.override_hold:
            refuse(pin, "this overrides a disc hold (the file's disc can't be told, its disc's counts differ, "
                        "or it sits in another disc's folder); pair it again with override_hold=True")
            continue
        usable[pin.path] = i
        if held:
            overrides.add(pin.path)
    return usable, problems, overrides
