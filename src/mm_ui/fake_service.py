"""In-memory stand-in for music_manager.service: contract TYPES + mock library/matching data.

Shaped to /workspace/mm-fix/API-CONTRACT.md v0 (rev. 2026-10-08 18:49 PT). The contract-shaped
namespaces (library, match, plan, journal, templates, settings, ...) and the template engine live in
fake_contract.py. All data here is MOCK DATA for UI work. Nothing touches disk.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field, replace
from enum import Enum, StrEnum
from pathlib import Path
from datetime import datetime, timedelta, timezone

from PySide6.QtCore import QObject, QTimer, Signal


class MatchStatus(Enum):   # values are strings, as in the real service (API-DEVIATIONS #13)
    UNMATCHED = "unmatched"; QUEUED = "queued"; NEEDS_REVIEW = "needs_review"; MATCHED = "matched"
    ACCEPTED = "accepted"; SKIPPED = "skipped"; NO_MATCH = "no_match"; APPLIED = "applied"
    HELD = "held"       # sorts right after NEEDS_REVIEW; refused by plan.apply, saved in the library DB


class JobKind(Enum):
    SCAN = "scan"; MATCH = "match"; FETCH_CANDIDATES = "fetch_candidates"; APPLY = "apply"; UNDO = "undo"
    CLUSTER = "cluster"     # c2fcf25: library.scan_clusters


class JobState(Enum):
    QUEUED = "queued"; RUNNING = "running"; PAUSED = "paused"; DONE = "done"; FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Job:
    id: int; kind: JobKind; state: JobState; done: int; total: int | None; message: str


@dataclass(frozen=True)
class ProviderCredit:
    name: str; text: str; url: str


DISCOGS = ProviderCredit("Discogs", "Data provided by Discogs", "https://www.discogs.com/")


@dataclass(frozen=True)
class LocalTrack:
    id: int; path: Path; disc: int; number: int | None; title: str; duration_s: float | None


@dataclass(frozen=True)
class RemoteTrack:
    position: str; disc: int; number: int; title: str; artist: str; duration_s: float | None
    subtracks: list[str] = field(default_factory=list)   # 2026-10-21: merged subtrack positions ["2-9a", "2-9b"]


@dataclass(frozen=True)
class TrackPair:
    method: str                         # Literal["number", "side", "title", "position", "unmatched"]
    local: LocalTrack | None; remote: RemoteTrack | None
    title_similarity: float; duration_delta_s: float | None; ok: bool
    positions: list[str] = field(default_factory=list)   # 2026-10-21: method "subtracks": ["2-9a", "2-9b"]


SEARCH_SHARE, TRACK_COUNT_WEIGHT, DURATIONS_WEIGHT = 0.75, 0.20, 0.05   # contract "Scoring" (4dc5e69)
DURATION_TOLERANCE_S = 5.0


@dataclass(frozen=True)
class ScorePart:
    field: str; weight: float; value: float; local: str; remote: str; ok: bool


@dataclass(frozen=True)
class Candidate:
    release_id: int; master_id: int | None
    artist: str; title: str; year: str; country: str; label: str; catno: str; formats: list[str]
    score: float
    margin: float | None                        # score minus runner-up's; None if only candidate
    breakdown: list[ScorePart]; alignment: list[TrackPair] | None
    artwork_url: str | None; thumb_url: str | None; artwork: Path | None
    fetched: bool; source: ProviderCredit
    auto_score: float | None = None             # the --auto score; auto_accept ranks by it (4dc5e69)


@dataclass(frozen=True)
class ClusterFile:          # c334bbf, contract "Clustering and album match view (2026-10-20)"
    path: Path
    disc: int               # DISCNUMBER tag, else CD folder / "2-01" name, else 1
    number: int | None
    title: str
    duration_s: float | None
    album: str              # the file's own album tag ("" if none); what it was clustered by
    artist: str             # its albumartist tag, else artist tag


@dataclass(frozen=True)
class Cluster:
    id: str                 # "cl-" + 12 hex chars, a hash of its file paths: same files, same id
    artist: str
    title: str
    folder: Path            # the files' common parent folder
    files: list[ClusterFile]
    disc_count: int
    album_id: int | None = None
    kind: str = "tag_cluster"   # c2fcf25: or "unclustered" (files moved out of every cluster; artist/title "")


@dataclass(frozen=True)
class ClusterRow:           # c2fcf25: one line of match.clusters
    id: str                 # "album-<id>", "cl-…" (tag cluster) or "un-…" (unclustered)
    kind: str               # "folder" | "tag_cluster" | "unclustered"
    artist: str
    title: str
    file_count: int
    status: MatchStatus | None
    album_id: int | None
    loose_tracks: bool
    folder: Path


@dataclass(frozen=True)
class Pin:                  # c2fcf25: a manual pair
    path: Path
    position: str
    release_id: int
    override_hold: bool
    stale: bool
    positions: list[str] = field(default_factory=list)   # 2026-10-21: [position], or a subtrack group


@dataclass(frozen=True)
class SubtrackGroup:        # 2026-10-21: release tracks sharing one base position (2-9a + 2-9b)
    disc: int
    position: str           # the base: "2-9"
    positions: list[str]    # ["2-9a", "2-9b"]
    titles: list[str]
    merged: bool
    reason: str             # pinned | durations_fit | not_needed | ambiguous | durations_unknown | durations_differ
    #                         | still_held | split | merge_off
    path: Path | None       # the file paired with the merged track; None when not merged
    duration_s: float | None  # summed Discogs length; None unless every subtrack lists one


@dataclass(frozen=True)
class ManualPair:           # c2fcf25: journal.manual_pairs
    album_id: int
    path: Path
    position: str
    override_hold: bool


@dataclass(frozen=True)
class MatchView:
    album_id: int | None    # None: a cluster not adopted yet
    cluster_id: str | None
    release_id: int | None
    fetched: bool           # else pairs / unmatched_tracks are empty and every file is unmatched
    candidates: list[Candidate]
    pairs: list[TrackPair]
    unmatched_files: list[LocalTrack]
    unmatched_tracks: list[RemoteTrack]
    problems: list[Problem]
    pins: list[Pin] = field(default_factory=list)   # c2fcf25: the album's pins for this release
    subtrack_groups: list[SubtrackGroup] = field(default_factory=list)   # 2026-10-21


@dataclass(frozen=True)
class AutoRules:
    min_score: float = 0.90             # AUTO_MIN_SCORE
    min_margin: float = 0.10            # AUTO_MIN_MARGIN, lead over the runner-up
    require_track_count: bool = True    # False = --allow-track-mismatch
    title_match_min: float = 0.80       # TITLE_MATCH_MIN for title-pass pairing


@dataclass(frozen=True)
class AlbumRow:
    id: int; folder: Path; artist: str; title: str; year: str; catno: str
    track_count: int; status: MatchStatus; confidence: float | None; artwork: Path | None
    artist_sort: str        # ALBUMARTISTSORT tag if present, else artist; what ARTIST sorts by
    added_at: datetime      # when a scan first saw this folder (timezone-aware UTC); ADDED sorts by it
    held_for: list          # list[Problem]; empty unless status is HELD
    loose_tracks: bool = False   # d6ac2f3: loose audio next to subfolders with audio (artist = folder name, title "")
    cluster_id: str | None = None  # c334bbf: set when this album is an adopted cluster (library.adopt_cluster)


@dataclass(frozen=True)
class Album(AlbumRow):
    tracks: list[LocalTrack] = field(default_factory=list)
    extras: list[Path] = field(default_factory=list)


@dataclass(frozen=True)
class AlbumFilter:
    status: set[MatchStatus] | None = None
    max_confidence: float | None = None
    text: str = ""


@dataclass(frozen=True)
class Page:
    items: list; total: int; offset: int


class StatusCounts(dict):
    """library.status_counts(): MatchStatus -> albums, plus a library-wide id3v23 track count (2026-10-21)."""

    def __init__(self, counts=(), id3v23: int = 0):
        super().__init__(counts)
        self.id3v23 = id3v23


@dataclass(frozen=True)
class TagChange:
    key: str; before: str | None; after: str | None
    key_before: str | None = None; key_after: str | None = None  # 2026-10-21: the file's own key (renames)
    migration: bool = False                     # 2026-10-21: caused by Settings.migrate_tag_keys only


@dataclass(frozen=True)
class FileChange:
    kind: str; path_before: Path; path_after: Path; tags: list[TagChange]
    skip_reason: str | None = None     # None means the step runs; skipped steps count toward nothing
    pinned: bool = False               # c2fcf25: this track was paired by hand (match.pin)
    merged_subtracks: list[str] = field(default_factory=list)   # 2026-10-21: this file is these subtracks


class ProblemKind(StrEnum):
    DEST_INSIDE_SOURCE = "dest_inside_source"   # library or target folder is inside the album
    SOURCE_INSIDE_TARGET = "source_inside_target"
    SYMLINK_OUTSIDE = "symlink_outside"         # paths lists every offending link
    COLLISION = "collision"
    TRACK_MISMATCH = "track_mismatch"           # release track count differs from local files
    FILE_CHANGED = "file_changed"               # edited/moved since plan or apply: rebuild / skipped on undo
    LEGACY_JOURNAL = "legacy_journal"           # undo of an old-format step needs trust_legacy=True
    RELEASE_NOT_FETCHED = "release_not_fetched" # plan.build: release still downloading (see Plan.fetch_job)
    DISC_AMBIGUOUS = "disc_ambiguous"           # multi-disc: files whose disc can't be told (paths); not overridable
    LAYOUT_AMBIGUOUS = "layout_ambiguous"       # loose audio next to disc folders / other albums; never overridden
    DISC_FOLDER_MISMATCH = "disc_folder_mismatch"   # destination puts tracks in another disc's CD folder; never overridden
    DISC_TRACK_COUNT = "disc_track_count"   # 1566b36: a disc's file/track counts differ, titles don't settle it; never overridden
    PIN_CONFLICT = "pin_conflict"           # c2fcf25: a manual pin that can't be used (paths: its file)
    ENCODING_DROPPED = "encoding_dropped"   # tag batch: Latin-1 can't hold some values (plan.build allow_encoding_drop)
# Cross-device moves are not a problem: they are allowed, and every file appears in `files`.


@dataclass(frozen=True)
class PinConflict:          # 1f7b340: why one pin isn't used
    path: Path
    position: str
    reason: str             # stale | unknown_position | duplicate_position | override_hold_required | disc_folder_mismatch
    #                         | not_subtrack_group (2026-10-21)


@dataclass(frozen=True)
class Problem:
    kind: ProblemKind; message: str; paths: list[Path]   # message is plain text for the dialog
    # ccfa4a8 (all default empty, so older code and stored problems still work):
    disc_conflicts: list[DiscConflict] = field(default_factory=list)   # DISC_AMBIGUOUS: one per refused file
    layout_reason: str = ""                     # LAYOUT_AMBIGUOUS: "disc_folders" | "album_folders"
    blocking_subdirs: list[Path] = field(default_factory=list)          # LAYOUT_AMBIGUOUS: subfolders that caused it
    loose_files: list[Path] = field(default_factory=list)               # LAYOUT_AMBIGUOUS: audio directly in the folder
    folder_mismatches: list[DiscFolderMismatch] = field(default_factory=list)  # DISC_FOLDER_MISMATCH: one per file
    pin_conflicts: list[PinConflict] = field(default_factory=list)   # 1f7b340: PIN_CONFLICT, one per refused pin


@dataclass(frozen=True)
class DiscFolderMismatch:
    """A track the destination would put into a CD1 / Disc 2 folder of another disc (2b82859)."""
    path: Path                                  # the track file now
    destination: Path                           # where the plan would put it
    folder: Path                                # the CD1/Disc 2 folder in that destination
    folder_disc: int                            # the disc that folder's name says
    tag_disc: int                               # the disc tag the file gets (its release disc)


@dataclass(frozen=True)
class DiscConflict:
    """One file the multi-disc pairing refused (contract "Disc conflicts")."""
    path: Path
    number: int | None                          # track number in its name (None: none)
    title: str                                  # title part of its name
    reason: str                                 # numbering | no_title_match | several_title_matches | same_track
                                                # | held_with_group | sources_disagree
    same_number: list[DiscChoice] = field(default_factory=list)     # release tracks with that number
    title_matches: list[DiscChoice] = field(default_factory=list)   # tracks its title matches, best first
    folder_disc: int | None = None              # sources_disagree only
    tag_disc: int | None = None
    sidecar_disc: int | None = None
    sidecar: Path | None = None


@dataclass(frozen=True)
class DiscChoice:
    """A release track a file could be."""
    disc: int; position: str; number: int; title: str
    title_similarity: float                     # the file's title vs this track's, 0..1


@dataclass(frozen=True)
class EncodingLoss:
    path: Path; field: str; value: str; lost: str  # the track, canonical tag, value, characters lost
    severity: str = "dropped"; proposed_value: str | None = None  # "transliterated" (amber) / "dropped" (red)


@dataclass(frozen=True)
class UnwrittenField:
    field: str; reason: str; detail: str  # canonical tag name, stable reason code, human-readable text
    folder: Path | None = None             # the album folder (AlbumPlan.folder_before)


@dataclass(frozen=True)
class AlbumPlan:
    album_id: int; release_id: int; folder_before: Path; folder_after: Path
    files: list[FileChange]; problems: list[Problem]   # any problem blocks apply for this album
    artist: str = ""; title: str = ""                  # the release's ("" when unknown, e.g. an old journal)
    unwritten: list[UnwrittenField] = field(default_factory=list)  # 2026-10-21: tags held back, and why
    junk: list[Path] = field(default_factory=list)                 # tag batch: junk files cleanup would trash
    encoding_losses: list[EncodingLoss] = field(default_factory=list)  # tag batch: Latin-1 losses
    junk_moving: list[Path] = field(default_factory=list)          # tag batch: junk files moving with the album
    id3v23_files: int = 0                                          # tag batch: tracks written as ID3v2.3
    encoding_transliterated: int = 0; encoding_dropped: int = 0    # tag batch: EncodingLoss totals


@dataclass(frozen=True)
class Plan:
    id: int; albums: list[AlbumPlan]; warnings: list[str]
    files_to_move: int; tags_to_write: int; bytes_to_copy: int
    skipped: int = 0                   # number of skipped steps (FileChange.skip_reason set)
    fetch_job: int | None = None       # job fetching releases this plan lacks; rebuild when it finishes


@dataclass(frozen=True)
class Operation:
    id: int; started: datetime; finished: datetime | None; albums: int; files: int
    kind: str                          # "apply", "normalize", "undo"
    undoable: bool                     # False once fully undone, or if its journal is missing
    undone: bool                       # a later undo reversed it (partly undone still counts False)
    undone_by: int | None              # id of that undo operation
    undo_of: int | None                # for kind "undo": the operation it reversed
    summary: str
    journal_format: int = 2            # 1 = old pickle snapshots, 2 = JSON
    legacy: bool = False               # journal_format < 2: undo_plan reports LEGACY_JOURNAL problems


class AlbumSort(StrEnum):    # every sort ends with folder path as the tiebreak, so pages are stable
    ARTIST = "artist"         # albumartist sort name, then lead year, then title
    TITLE = "title"
    YEAR = "year"             # lead year, then artist
    ADDED = "added"           # newest scan first
    CONFIDENCE = "confidence" # lowest first, unmatched (None) last
    STATUS = "status"         # review order: NEEDS_REVIEW, UNMATCHED, QUEUED, ... APPLIED
    FOLDER = "folder"
# Prefix "-" for descending, e.g. "-year".


@dataclass(frozen=True)
class Destination:
    library_root: Path                           # the configured root the album lands under
    folder: Path                                 # absolute; always inside library_root
    files: list[tuple[Path, Path]]               # before, after, both absolute (empty until fetched)
    problems: list[Problem]                      # same kinds as AlbumPlan; any of them blocks apply


@dataclass(frozen=True)
class Template:
    id: str                 # "archivist", "simple", "edition-detail", "label-archive", or "user:<slug>"
    name: str               # shown in the UI
    text: str
    builtin: bool           # built-ins can't be edited or deleted; "Duplicate" makes a user copy


@dataclass(frozen=True)
class Collision:
    target: Path; album_ids: list[int]        # two or more albums, one folder or file


@dataclass(frozen=True)
class TemplatePreview:
    moves: list[tuple[Path, Path]]            # before, after
    collisions: list[Collision]               # plan.apply refuses any album listed here
    warnings: list[str]                       # e.g. "3 albums have no label: Unknown label/"


@dataclass(frozen=True)
class Settings:
    library_roots: list[Path]               # empty until the user picks one; nothing defaults
    active_template: str                    # Template.id, default "archivist"
    sequential_sides: bool = False          # True: 1..n instead of A1/B1 for vinyl and cassette
    save_cover_art: bool = True             # folder.jpg from the chosen release
    clear_other_tags: bool = False
    merge_subtracks: bool = True            # 2026-10-21: merge subtracks ripped as one file automatically
    migrate_tag_keys: bool = False          # 2026-10-21: also drop tag keys earlier versions wrote (Vorbis YEAR)
    write_styles: bool = False              # 2026-10-21: Discogs styles as STYLE (off: genres only)
    styles_in_genre: bool = False           # 2026-10-21: also append the styles to GENRE
    id3v23_mp3: bool = False                # tag batch: MP3s as ID3v2.3 too (WAV/AIFF always are)
    id3_latin1: bool = False                # tag batch: ID3v2.3 text as Latin-1 (see AlbumPlan.encoding_losses)
    verify_hash: str = "sha256"             # tag batch: "sha256" or "xxhash"


# ---------------------------------------------------------------- errors
class MMError(Exception):
    """Base error."""


class NotFound(MMError):
    pass


class PlanStale(MMError):
    pass


class ValidationError(MMError):
    """Bad template etc. `field` names the offending field (attribute name not fixed by the contract)."""
    def __init__(self, message: str, field: str | None = None):
        super().__init__(message); self.field = field


class ProviderError(MMError):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message); self.retry_after = retry_after


class ConfigError(MMError):
    pass


class Events(QObject):
    job_progress = Signal(object)
    job_finished = Signal(object)
    album_changed = Signal(int)
    album_removed = Signal(int)             # 1f7b340: album deleted from the index (un-adopt, emptied cluster, …)
    candidate_ready = Signal(int, int)
    clusters_changed = Signal(object)       # c2fcf25: list[str] of current cluster ids
    cluster_changed = Signal(str, str)      # c2fcf25: old id, new id ("" = gone)
    match_view_changed = Signal(int)        # c2fcf25: album_id (pins)
    rate_limited = Signal(float)


# ---------------------------------------------------------------- mock data
# Absolute (the contract requires absolute paths) but deliberately fake; the mock never touches disk.
LIBRARY_ROOT = Path("/mock/grok-bot-approved")
ROOT = LIBRARY_ROOT / "Black Flag"
SCANNED_AT = datetime(2026, 9, 14, 20, 0, tzinfo=timezone.utc)   # mock: first scan of the real-looking albums

_MOCK = [
    # artist, title, year, catno, [(title, secs)], candidates [(label, catno, year, country, fmt, score)]
    ("Black Flag", "Damaged", "1981", "", [
        ("Rise Above", 146), ("Spray Paint", 35), ("Six Pack", 140), ("What I See", 118),
        ("TV Party", 215), ("Thirsty and Miserable", 140), ("Police Story", 93),
        ("Gimme Gimme Gimme", 107), ("Depression", 162), ("Room 13", 132),
        ("Damaged II", 143), ("No More", 128), ("Padded Cell", 103), ("Life of Pain", 139),
        ("Damaged I", 231)],
     [("SST", "SST 007", "1981", "US", "Vinyl, LP", 0.72),
      ("Unicorn Records", "9502", "1981", "US", "Vinyl, LP", 0.66),
      ("SST", "SST 007", "1990", "US", "CD, Reissue", 0.61),
      ("SST", "SST CD 007", "2003", "US", "CD, Reissue", 0.58)]),
    ("Black Flag", "My War", "1984", "SST 023", [
        ("My War", 225), ("Can't Decide", 325), ("Beat My Head Against the Wall", 149),
        ("I Love You", 205), ("Forever Time", 81), ("The Swinging Man", 177),
        ("Nothing Left Inside", 404), ("Three Nights", 395), ("Scream", 410)],
     [("SST", "SST 023", "1984", "US", "Vinyl, LP", 0.95),
      ("SST", "SST 023", "1987", "US", "CD, Reissue", 0.81)]),
    ("Black Flag", "Slip It In", "1984", "", [
        ("Slip It In", 395), ("Black Coffee", 284), ("Wound Up", 254), ("Rat's Eyes", 225),
        ("Obliteration", 290), ("The Bars", 233), ("My Ghetto", 138), ("You're Not Evil", 333)],
     [("SST", "SST 029", "1984", "US", "Vinyl, LP", 0.84),
      ("SST", "SST 029", "1990", "US", "CD, Reissue", 0.79)]),
    ("Black Flag", "Live '84", "1984", "", [("The Process of Weeding Out", 330), ("Nervous Breakdown", 140)],
     []),
    ("Black Flag", "Everything Went Black", "1982", "SST 015", [
        ("Gimmie Gimmie Gimmie", 112), ("I Don't Care", 59), ("White Minority", 66),
        ("No Values", 103), ("Revenge", 63)],
     [("SST", "SST 015", "1982", "US", "2xVinyl, LP, Comp", 0.93)]),
]


def _sim(a: str, b: str) -> float:
    a, b = a.lower(), b.lower()
    if a == b:
        return 1.0
    sa, sb = set(a.split()), set(b.split())
    return len(sa & sb) / max(1, len(sa | sb))


# ---------------------------------------------------------------- multi-disc pairing (contract fc27fcf)
import re as _re
from collections import Counter as _Counter
from difflib import SequenceMatcher as _SM

_DISC_DIR = _re.compile(r"^(?:cd|disc|disk)\s*(\d+)$", _re.I)
_NUM = _re.compile(r"^(?:(\d+)-)?(\d+)\b")
TITLE_MATCH_MIN = 0.80


def _title_of(stem: str) -> str:
    return _re.sub(r"^(?:\d+-)?\d+[\s.\-_]*", "", stem).strip()


def _tsim(a: str, b: str) -> float:
    """Title similarity for the multi-disc rules (the engine uses difflib too)."""
    if not a or not b:
        return 0.0
    return round(_SM(None, a.casefold(), b.casefold()).ratio(), 3)


def _choice(rt, number: int, title: str) -> "DiscChoice":
    return DiscChoice(rt.disc, rt.position or f"{rt.disc}-{rt.number}", number, rt.title, _tsim(title, rt.title))


def pair_tracks(local: list, remote: list, title_match_min: float = TITLE_MATCH_MIN, sidecars=None):
    """(pairs, ambiguous files); see pair_tracks_explained."""
    out, amb, _conflicts = pair_tracks_explained(local, remote, title_match_min, sidecars)
    return out, amb


def pair_tracks_explained(local: list, remote: list, title_match_min: float = TITLE_MATCH_MIN, sidecars=None):
    """MOCK of the engine's align_tracks_explained on a multi-disc release (API-CONTRACT "Multi-disc pairing",
    "Disc conflicts"): every file is paired on (disc, track).
    - A file whose disc sources don't all agree (its CD1/CD2 folder, its disc tag LocalTrack.disc, and the
      cue/m3u that lists it: sidecars = {file path: (sidecar path, disc)}) is refused: sources_disagree.
    - Otherwise the disc comes from the disc tags (used only when every numbered file has one, no two claim
      the same (disc, track) and the release has all of them), then a CD1/CD2 folder, then a "1-01" name.
    - Files without a disc that share a number are placed only when the folder's numbers restart exactly
      like the release's discs (else: numbering) and each title picks one same-number track (else:
      no_title_match / several_title_matches / same_track, the rest of the group held_with_group).

    Returns (pairs, ambiguous, conflicts): pairs[i] is the RemoteTrack for local[i] or None, ambiguous the
    refused files and conflicts one DiscConflict per refused file, both in file order. A single-disc
    release pairs by number (then by order), as the mock always did; no tags are read."""
    sidecars = sidecars or {}
    discs = {rt.disc for rt in remote}
    keyed = {(rt.disc, rt.number): rt for rt in remote}
    info = []
    for lt in local:
        m = _NUM.match(lt.path.stem)
        number = int(m.group(2)) if m else lt.number
        folder = _DISC_DIR.match(lt.path.parent.name)
        hint = int(folder.group(1)) if folder else (int(m.group(1)) if m and m.group(1) else None)
        info.append([lt, hint, number])
    if len(discs) <= 1:
        out = [next((rt for rt in remote if rt.number == n), None) for _lt, _h, n in info]
        if None in out and len(local) == len(remote):
            out = list(remote)
        return out, [], []
    out: list = [None] * len(info)
    refused: set[int] = set()
    conflicts: dict = {}
    for k, (lt, _h, n) in enumerate(info):            # 1. disc sources must agree (ccfa4a8)
        folder = _DISC_DIR.match(lt.path.parent.name)
        fdisc = int(folder.group(1)) if folder else None
        side, sdisc = sidecars.get(lt.path, (None, None))
        said = {d for d in (fdisc, lt.disc, sdisc) if d is not None}
        if len(said) > 1:
            refused.add(k)
            title = _title_of(lt.path.stem)
            conflicts[k] = DiscConflict(lt.path, n, title, "sources_disagree",
                                        [_choice(rt, rt.number, title) for rt in remote
                                         if n is not None and rt.number == n],
                                        [], fdisc, lt.disc, sdisc, side)
    live = [k for k in range(len(info)) if k not in refused]
    tag_keys = [(info[k][0].disc, info[k][2]) for k in live if info[k][2] is not None]
    if (all(info[k][0].disc for k in live if info[k][2] is not None) and tag_keys
            and len(set(tag_keys)) == len(tag_keys) and all(key in keyed for key in tag_keys)):
        for k in live:
            info[k][1] = info[k][0].disc               # tags win when they fit the whole album
    used: set[int] = set()
    loose = [k for k in live if info[k][1] is None and info[k][2] is not None]
    counts = _Counter(info[k][2] for k in loose)
    repeated = sorted(n for n, c in counts.items() if c > 1)
    if repeated:
        hinted = {info[k][1] for k in live if info[k][1] is not None}
        open_tracks = [rt for rt in remote if rt.disc not in hinted]
        restarts = _Counter(rt.number for rt in open_tracks) == counts
        for n in repeated:
            group = [k for k in loose if info[k][2] == n]
            same = [rt for rt in open_tracks if rt.number == n]
            titles = {k: _title_of(info[k][0].path.stem) for k in group}
            reasons = dict.fromkeys(group, "numbering")
            picks = None
            if restarts:
                good = {k: [rt for rt in same if titles[k] and _tsim(titles[k], rt.title) >= title_match_min]
                        for k in group}
                single = [id(g[0]) for g in good.values() if len(g) == 1]
                bad = {}
                for k, g in good.items():
                    if not g:
                        bad[k] = "no_title_match"
                    elif len(g) > 1:
                        bad[k] = "several_title_matches"
                    elif single.count(id(g[0])) > 1:
                        bad[k] = "same_track"
                if bad:
                    reasons = {k: bad.get(k, "held_with_group") for k in group}
                else:
                    picks = {k: g[0] for k, g in good.items()}
            if picks is None:
                for k in group:
                    refused.add(k)
                    t = titles[k]
                    matches = sorted((_choice(rt, rt.number, t) for rt in remote
                                      if t and _tsim(t, rt.title) >= title_match_min),
                                     key=lambda c: (-c.title_similarity, c.disc, c.number))
                    conflicts[k] = DiscConflict(info[k][0].path, n, t, reasons[k],
                                                [_choice(rt, n, t) for rt in same], matches)
                continue
            for k, rt in picks.items():
                out[k] = rt; used.add(id(rt))
    for k, (_lt, h, n) in enumerate(info):
        if out[k] is not None or k in refused or n is None:
            continue
        if h is not None:
            rt = keyed.get((h, n))
        else:
            cands = [rt for rt in remote if rt.number == n and id(rt) not in used]
            rt = cands[0] if len(cands) == 1 else None
        if rt is not None and id(rt) not in used:
            out[k] = rt; used.add(id(rt))
    order = sorted(refused)
    return out, [info[k][0].path for k in order], [conflicts[k] for k in order]


# 68cf4d7 fs.py wording (contract "Album folders with loose audio and subfolders")
MIXED_DISCS = ("loose audio files sit next to disc folders with audio ({discs}); it is not clear which files "
               "belong to which disc, so nothing is applied. Move the loose files into a disc folder (or the "
               "disc folders' files up) and scan again")
STRAY_AUDIO = ("loose audio files sit next to subfolders that also hold audio ({folders}); it is not clear "
               "whether this is an artist folder with stray files or an album with extra folders, so nothing "
               "in it is applied or moved. Move the loose files into a subfolder (or the subfolders' files "
               "up) and scan again")       # 0a3fad1: neutral wording


def disc_warning(folder: Path, files: list[Path], conflicts: list | None = None) -> str:
    """Same wording as music_manager.apply.disc_warning (ccfa4a8)."""
    def names(paths):
        return ", ".join(str(p.relative_to(folder)) if folder in p.parents else p.name for p in paths)

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


_DISC_DIR_PREFIX = _re.compile(r"^(?:cd|disc|disk)\s*[-_. ]?\s*\d+", _re.I)   # fs.DISC_DIR


def folder_mismatches(root: Path, placed: list[tuple[Path, Path, int]]) -> list:
    """MOCK of apply.disc_folder_mismatches: placed = (file now, destination, disc tag it gets). Every folder
    between the library root and the file counts; the first CDn/Disc n folder with another number wins."""
    out = []
    for src, dest, disc in placed:
        if not disc:
            continue
        try:
            parts, base = dest.parent.relative_to(root).parts, root
        except ValueError:
            parts, base = (dest.parent.name,), dest.parent.parent
        for i, part in enumerate(parts):
            m = _re.search(r"\d+", part) if _DISC_DIR_PREFIX.match(part) else None
            if m and int(m.group(0)) != disc:
                out.append(DiscFolderMismatch(src, dest, base.joinpath(*parts[:i + 1]), int(m.group(0)), disc))
                break
    return out


def disc_count_message(folder: Path, disc: int, files: list[Path], n_tracks: int) -> str:
    """Wording of the engine's DISC_TRACK_COUNT message (API-CONTRACT 2026-10-19 item 2), shown as text only."""
    sub = {Path(str(f)).parent for f in files}
    where = f" ({next(iter(sub)).name}/)" if len(sub) == 1 and next(iter(sub)) != folder else ""
    return (f"disc {disc}{where} has {len(files)} files but the release lists {n_tracks} tracks, and the titles "
            "don't pair every file with exactly one track. None of this disc's files is paired: pairing by number "
            "would shift every title after the difference. Choose a release that lists the discs as the files have "
            "them, or name the files after their tracks so their titles pair them")


def disc_folder_message(folder: Path, mismatches: list, lead: str = "the destination puts tracks into") -> str:
    """Same wording as music_manager.apply.disc_folder_message (2b82859)."""
    detail = "; ".join(f"{m.path.name} -> {m.folder.name}/ (disc tag {m.tag_disc})" for m in mismatches)
    return (f"{folder}: {lead} another disc's folder ({len(mismatches)} file(s)): {detail}. "
            "Change the naming template so each disc's tracks go into that disc's folder")


class FakeService(QObject):
    """Mirrors the contract's module functions as methods."""

    def __init__(self, fetch_delay_ms: int = 900):
        super().__init__()
        self.events = Events()
        self._ids = itertools.count(1)
        self._albums: dict[int, Album] = {}
        self._cands: dict[int, list[Candidate]] = {}
        self._fetch_queue: list[tuple[int, int]] = []
        self._chosen: dict[int, int] = {}
        tid = itertools.count(1000)
        rid = itertools.count(1_500_000, 7919)
        for artist, title, year, catno, tracks, cands in _MOCK:
            aid = next(self._ids)
            folder = ROOT / f"{year} - {title}"
            lt = [LocalTrack(next(tid), folder / f"{i:02d} {t}.flac", 1, i, t, float(s))
                  for i, (t, s) in enumerate(tracks, 1)]
            clist = []
            for label, cno, cyear, country, fmt, score in cands:
                # album/artist similarity chosen so that score == sum(weight * value) (contract "Scoring")
                cat_v = 1.0 if catno and catno == cno else 0.0
                yr_v = 1.0 if year == cyear else 0.0
                sim = max(0.0, min(1.0, (score - .18 * cat_v - .07 * yr_v) / .75))
                if sim == 1.0 and not cat_v:      # hand-set score above what the parts allow: partial catno match
                    cat_v = round(max(0.0, min(1.0, (score - .75 - .07 * yr_v) / .18)), 4)
                parts = [
                    ScorePart("album", .40, round(sim, 4), title, title, sim >= 0.8),
                    ScorePart("artist", .35, round(sim, 4), artist, artist, sim >= 0.8),
                    ScorePart("catno", .18, cat_v, catno or "—", cno, cat_v >= 1.0),
                    ScorePart("year", .07, yr_v, year, cyear, year == cyear),
                ]
                clist.append(Candidate(next(rid), None, artist, title, cyear, country, label, cno,
                                       [fmt], score, None, parts, None, None, None, None, False, DISCOGS,
                                       auto_score=score))   # unfetched: score == the --auto score
            clist = [replace(c, margin=round(c.score - clist[i + 1].score, 4) if i + 1 < len(clist) else None)
                     for i, c in enumerate(clist)]
            top = clist[0].score if clist else None
            if not clist:
                status = MatchStatus.NO_MATCH
            elif top >= 0.9:
                status = MatchStatus.MATCHED
            else:
                status = MatchStatus.NEEDS_REVIEW
            self._albums[aid] = Album(aid, folder, artist, title, year, catno, len(lt), status, top,
                                      None, artist, SCANNED_AT + timedelta(minutes=aid), [],
                                      tracks=lt, extras=[folder / "cover.jpg"])
            self._cands[aid] = clist
            self._fetch_queue += [(aid, c.release_id) for c in clist[:4]]
        self._layout_problems: dict[int, tuple] = {}       # MOCK: (layout_problem, reason, subfolders) (68cf4d7)
        self._sidecars: dict[int, dict] = {}               # MOCK: album -> {file: (cue/m3u, its disc)}
        self._tracklists: dict[int, list[RemoteTrack]] = {}   # MOCK: releases with an explicit (multi-disc) tracklist
        self._subtrack_releases: dict[int, list] = {}   # MOCK 2026-10-21: release -> [(position, title, secs)]
        self._file_tags: dict[Path, tuple[str, str]] = {}   # MOCK: file -> (artist, album) tags, for clustering
        self._rid = rid
        self._tid = tid
        self._timer = QTimer(self)
        self._timer.setInterval(fetch_delay_ms)
        self._timer.timeout.connect(self._fetch_one)
        self._timer.start()

    def add_multi_disc_album(self, artist: str, title: str, year: str, files: list[tuple[str, int | None, float]],
                             tracks: list[tuple[int, int, str, float]], score: float = 0.95,
                             catno: str = "MOCK-2CD", sidecars: list[tuple[str, int, list[str]]] = ()) -> int:
        """MOCK: add an album whose (one) candidate is a multi-disc release.
        files: (path relative to the album folder, disc number tag or None, seconds)
        tracks: (disc, number, title, seconds) in release order.
        sidecars: (cue/m3u path relative to the folder, its disc, [the files it lists, as in files])."""
        aid = next(self._ids)
        folder = LIBRARY_ROOT / artist / f"{year} - {title}"
        lt = []
        for rel, disc, secs in files:
            path = folder / f"{rel}.flac"
            m = _NUM.match(path.stem)
            lt.append(LocalTrack(next(self._tid), path, disc, int(m.group(2)) if m else None,
                                 _title_of(path.stem) or path.stem, float(secs)))
        rid = next(self._rid)
        self._tracklists[rid] = [RemoteTrack(f"{d}-{n}", d, n, t, artist, float(sec)) for d, n, t, sec in tracks]
        parts = [ScorePart("album", .40, 1.0, title, title, True), ScorePart("artist", .35, 1.0, artist, artist, True),
                 ScorePart("catno", .18, 1.0, catno, catno, True), ScorePart("year", .07, 1.0, year, year, True)]
        cand = Candidate(rid, None, artist, title, year, "US", "Mock Label", catno, [f"{len({d for d, *_ in tracks})}xCD"],
                         1.0, None, parts, None, None, None, None, False, DISCOGS, auto_score=1.0)
        self._albums[aid] = Album(aid, folder, artist, title, year, catno, len(lt), MatchStatus.MATCHED, 1.0,
                                  None, artist, SCANNED_AT + timedelta(minutes=aid), [], tracks=lt, extras=[])
        self._cands[aid] = [cand]
        self._sidecars[aid] = {folder / f"{rel}.flac": (folder / side, disc)
                               for side, disc, rels in sidecars for rel in rels}
        return aid

    def add_subtrack_album(self, artist: str, title: str, year: str, catno: str,
                           tracks: list[tuple[str, str, float | None]], files: list[tuple[str, float]],
                           score: float = 0.97) -> int:
        """MOCK (2026-10-21 "Subtracks"): an album whose one, fetched candidate lists subtracks.
        tracks: (Discogs position "2-9a", title, seconds or None) in release order; files: (path relative to the
        album folder, seconds). FakeContract pairs it on (disc, number) and merges groups like collapse_subtracks."""
        aid = next(self._ids)
        folder = LIBRARY_ROOT / artist / f"{year} - {title}"
        lt = []
        for rel, secs in files:
            path = folder / f"{rel}.flac"
            m = _NUM.match(path.stem)
            fm = _DISC_DIR.match(path.parent.name)
            lt.append(LocalTrack(next(self._tid), path, int(fm.group(1)) if fm else 1, int(m.group(2)) if m else None,
                                 _title_of(path.stem) or path.stem, float(secs)))
        rid = next(self._rid)
        self._subtrack_releases[rid] = list(tracks)
        discs = len({p.split("-")[0] for p, _t, _s in tracks})
        parts = [ScorePart("album", .40, 1.0, title, title, True), ScorePart("artist", .35, 1.0, artist, artist, True),
                 ScorePart("catno", .18, 1.0, catno, catno, True), ScorePart("year", .07, 1.0, year, year, True)]
        cand = Candidate(rid, None, artist, title, year, "US", "Mock Label", catno, [f"{discs}xCD"], score, None,
                         parts, None, None, None, None, True, DISCOGS, auto_score=score)
        self._albums[aid] = Album(aid, folder, artist, title, year, catno, len(lt), MatchStatus.NEEDS_REVIEW, score,
                                  None, artist, SCANNED_AT + timedelta(minutes=aid), [], tracks=lt, extras=[])
        self._cands[aid] = [cand]
        return aid

    def add_prine_subtracks(self, lengths: bool = False) -> int:
        """MOCK modeled on Discogs 4054414, John Prine "The Singing Mailman Delivers" (2CD): disc 2 lists
        2-9a "Hey Good Lookin'" and 2-9b "Jambalaya (On The Bayou)", ripped as ONE file, so 24 files for
        25 tracks. lengths=False (as on Discogs): no durations, the group can't be merged automatically and
        disc 2 stays held until a file is pinned to the group. lengths=True: Discogs-style lengths that sum
        to the file's, so the group merges automatically (reason durations_fit)."""
        d1 = ["Sour Grapes", "Illegal Smile", "Paradise", "Hello In There", "Souvenirs", "Quiet Man",
              "Sam Stone", "Great Society Conflict Veteran's Blues", "Frying Pan", "Blue Umbrella", "A Good Time"]
        d2 = [("1", "Spanish Pipedream"), ("2", "Twistin' Mabel"), ("3", "Pretty Good"), ("4", "Donald And Lydia"),
              ("5", "Flashback Blues"), ("6", "Angel From Montgomery"), ("7", "Your Flag Decal Won't Get You Into Heaven Anymore"),
              ("8", "Uwanta B. Lindasa"), ("9a", "Hey Good Lookin'"), ("9b", "Jambalaya (On The Bayou)"),
              ("10", "Billy The Bum"), ("11", "The Great Compromise"), ("12", "Everybody"), ("13", "Late John Garfield Blues")]
        def secs(i: int) -> float:
            return 150.0 + 7 * i
        tracks = [(f"1-{i}", t, secs(i) if lengths else None) for i, t in enumerate(d1, 1)]
        tracks += [(f"2-{p}", t, (100.0 if p == "9a" else 95.0 if p == "9b" else secs(20 + k)) if lengths else None)
                   for k, (p, t) in enumerate(d2)]
        files = [(f"CD1/{i:02d} {t}", secs(i)) for i, t in enumerate(d1, 1)]
        k = 0
        for p, t in d2:
            if p == "9b":
                k += 1
                continue
            name = "Hey Good Lookin' - Jambalaya" if p == "9a" else t
            files.append((f"CD2/{int(p.rstrip('ab')):02d} {name}", 196.0 if p == "9a" else secs(20 + k)))
            k += 1
        title = "The Singing Mailman Delivers" + ("" if not lengths else " (lengths listed)")
        return self.add_subtrack_album("John Prine", title, "2011", "OBR-012", tracks, files)

    def add_layout_album(self, folder: Path, artist: str, title: str, year: str, catno: str, files: list[str],
                         layout_problem: str = "", candidate: bool = True, score: float = 0.93,
                         layout_reason: str = "", blocking_subdirs: list[str] = ()) -> int:
        """MOCK of one scanned album folder. files: audio paths relative to folder (no extension). A non-empty
        layout_problem is AlbumFolder.layout_problem (68cf4d7): plan.build / match.destination then give the
        album a LAYOUT_AMBIGUOUS problem and apply holds it. layout_reason / blocking_subdirs (names relative
        to folder) are AlbumFolder.layout_conflict (ccfa4a8); loose_files are the files directly in folder."""
        aid = next(self._ids)
        lt = []
        for i, rel in enumerate(files, 1):
            path = folder / f"{rel}.mp3"
            m = _NUM.match(path.stem)
            lt.append(LocalTrack(next(self._tid), path, None, int(m.group(2)) if m else i,
                                 _title_of(path.stem) or path.stem, 150.0 + 10 * i))
        clist = []
        if candidate:
            parts = [ScorePart("album", .40, 1.0, title, title, True), ScorePart("artist", .35, 1.0, artist, artist, True),
                     ScorePart("catno", .18, 1.0, catno or "—", catno or "X-1", True),
                     ScorePart("year", .07, 1.0, year, year, True)]
            clist = [Candidate(next(self._rid), None, artist, title, year, "US", "Mock Label", catno or "X-1", ["CD"],
                               score, None, parts, None, None, None, None, False, DISCOGS, auto_score=score)]
        status = MatchStatus.NEEDS_REVIEW if clist else MatchStatus.UNMATCHED
        loose = layout_reason == "album_folders"       # d6ac2f3: loose tracks, named after its own folder
        if loose:
            artist, title, year, catno = folder.name, "", "", ""
        self._albums[aid] = Album(aid, folder, artist, title, year, catno, len(lt), status, score if clist else None,
                                  None, artist, SCANNED_AT + timedelta(minutes=aid), [], loose_tracks=loose,
                                  tracks=lt, extras=[])
        self._cands[aid] = clist
        if layout_problem:
            self._layout_problems[aid] = (layout_problem, layout_reason, [folder / d for d in blocking_subdirs])
        return aid

    def add_loose_layouts(self) -> dict[str, int]:
        """MOCK of the three layouts in the contract section, as the 68cf4d7 scan sees them:
        - "stray": Minutemen/ with a loose demo next to two album folders -> loose tracks (d6ac2f3): just the
          loose file, artist "Minutemen" (the folder's name), title "", loose_tracks=True, held when applied;
          "buzz" and "tie" are listed as albums of their own;
        - "mixed": an album with loose audio next to CD1/ and CD2/ -> one album with every file, held;
        - "bonus": an album with an audio Bonus/ folder -> also loose tracks (layout_reason "album_folders", named
          after its folder), held; "bonus_dir" is its own (untagged) album."""
        ids = {}
        art = LIBRARY_ROOT / "Minutemen"
        buzz, tie = "[1983]  Buzz or Howl Under the Influence of Heat [SST016]", "[1985]  3-Way Tie (For Last) [SST058]"
        ids["stray"] = self.add_layout_album(art, LIBRARY_ROOT.name, art.name, "", "", ["demo take"],
                                             STRAY_AUDIO.format(folders=f"{buzz}/, {tie}/"),
                                             layout_reason="album_folders", blocking_subdirs=[buzz, tie])
        ids["buzz"] = self.add_layout_album(art / buzz, "Minutemen", "Buzz or Howl Under the Influence of Heat",
                                            "1983", "SST 016", ["01 Self-Referenced", "02 Cut", "03 Dreams Are Free"])
        ids["tie"] = self.add_layout_album(art / tie, "Minutemen", "3-Way Tie (For Last)", "1985", "SST 058",
                                           ["01 The Price of Paradise", "02 Lost"])
        mixed = LIBRARY_ROOT / "Meat Puppets" / "[1984]  Meat Puppets II [SST019]"
        ids["mixed"] = self.add_layout_album(mixed, "Meat Puppets", "Meat Puppets II", "1984", "SST 019",
                                             ["01 Split Myself in Two", "CD1/02 Magic Toy Missing", "CD2/01 Lost"],
                                             MIXED_DISCS.format(discs="CD1/, CD2/"),
                                             layout_reason="disc_folders", blocking_subdirs=["CD1", "CD2"])
        zen = LIBRARY_ROOT / "Hüsker Dü" / "[1984]  Zen Arcade [SST027]"
        ids["bonus"] = self.add_layout_album(zen, "Hüsker Dü", "Zen Arcade", "1984", "SST 027",
                                             ["01 Something I Learned Today", "02 Broken Home, Broken Heart"],
                                             STRAY_AUDIO.format(folders="Bonus/"),
                                             layout_reason="album_folders", blocking_subdirs=["Bonus"])
        ids["bonus_dir"] = self.add_layout_album(zen / "Bonus", "", "Bonus", "", "", ["01 Outtake"], candidate=False)
        return ids

    def add_loose_cluster_demo(self) -> dict[str, int]:
        """MOCK of the contract's clustering example: Henry Rollins/ holds loose files tagged for two albums next
        to an album folder, so the folder is loose tracks (held) and library.clusters() splits it in two."""
        art = LIBRARY_ROOT / "Henry Rollins"
        sub = "[1987]  Hot Animal Machine [TXH 0002]"
        files = ["01 Black And White", "02 Followed Around", "03 Lost And Found", "05 There's A Man Outside"]
        aid = self.add_layout_album(art, "Henry Rollins", "", "", "", files[:3] + ["01 Gone Fishin'"],
                                    STRAY_AUDIO.format(folders=f"{sub}/"), candidate=False,
                                    layout_reason="album_folders", blocking_subdirs=[sub])
        for t in self._albums[aid].tracks:
            self._file_tags[t.path] = (("Rollins Band", "Life Time") if "Gone" in t.title
                                       else ("Henry Rollins", "Hot Animal Machine"))
        real = self.add_layout_album(art / sub, "Henry Rollins", "Hot Animal Machine (2CD)", "1987", "TXH 0002",
                                     ["01 Black And White"])
        return {"loose": aid, "album": real}

    def layout_problem(self, album_id: int) -> str:
        """AlbumFolder.layout_problem of a mock album ("" when the layout is fine)."""
        return self._layout_problems.get(album_id, ("",))[0]

    def layout_conflict(self, album_id: int):
        """(reason, blocking subfolders, loose files) of a held mock layout, None when the layout is fine."""
        if album_id not in self._layout_problems:
            return None
        _msg, reason, subdirs = self._layout_problems[album_id]
        album = self._albums[album_id]
        return reason, list(subdirs), [t.path for t in album.tracks if t.path.parent == album.folder]

    def disc_pairing(self, album_id: int, release_id: int):
        """(pairs, ambiguous files) for a multi-disc mock release, None for ordinary mock releases."""
        x = self.disc_pairing_explained(album_id, release_id)
        return None if x is None else x[:2]

    def _raw_pairing(self, album_id: int, release_id: int):
        tl = self._tracklists.get(release_id)
        return None if tl is None else pair_tracks_explained(self._albums[album_id].tracks, tl,
                                                             sidecars=self._sidecars.get(album_id))

    def disc_pairing_explained(self, album_id: int, release_id: int):
        """(pairs, ambiguous files, DiscConflicts) for a multi-disc mock release, None otherwise. Files on a disc
        held for disc_track_count (1566b36) are left unpaired (None), never shifted."""
        raw = self._raw_pairing(album_id, release_id)
        if raw is None:
            return None
        held = {pth for _d, _n, _m, paths in self.disc_count_holds(album_id, release_id) for pth in paths}
        pairs, amb, conflicts = raw
        return [None if t.path in held else rt for t, rt in zip(self._albums[album_id].tracks, pairs)], amb, conflicts

    def disc_count_holds(self, album_id: int, release_id: int) -> list[tuple[int, int, int, list[Path]]]:
        """MOCK of 1566b36: (disc, files, tracks, that disc's files) for each disc whose number of paired files
        differs from the release's track count for it and whose titles don't pair every file with exactly one
        of its tracks (a missing file with all titles matching still stands; a medley file holds the disc)."""
        raw = self._raw_pairing(album_id, release_id)
        if raw is None or raw[1]:          # files whose disc can't be told: disc_ambiguous already holds it
            return []
        tl, tracks = self._tracklists[release_id], self._albums[album_id].tracks
        by_disc: dict[int, list] = {}
        for t, rt in zip(tracks, raw[0]):
            if rt is not None:
                by_disc.setdefault(rt.disc, []).append(t)
        out = []
        for disc, files in sorted(by_disc.items()):
            rel = [rt for rt in tl if rt.disc == disc]
            if len(files) == len(rel):
                continue
            matches = [[rt for rt in rel if _tsim(t.title, rt.title) >= TITLE_MATCH_MIN] for t in files]
            one_each = all(len(m) == 1 for m in matches) and len({id(m[0]) for m in matches}) == len(files)
            if not one_each:
                out.append((disc, len(files), len(rel), [t.path for t in files]))
        return out

    # -------- background "Discogs" fetch, paced like the real rate limit
    def _fetch_one(self, pace: bool = True):
        if not self._fetch_queue:
            self._timer.stop(); return
        aid, rel = self._fetch_queue.pop(0)
        album = self._albums[aid]
        out = []
        for c in self._cands[aid]:
            if c.release_id != rel:
                out.append(c); continue
            pairs = []
            multi = self.disc_pairing(aid, rel)
            if multi is not None:                   # (disc, track) pairing; refused files are "unmatched"
                remotes, _amb = multi
                for lt, rt in zip(album.tracks, remotes):
                    if rt is None:
                        pairs.append(TrackPair("unmatched", lt, None, 0.0, None, False))
                    else:
                        d = (rt.duration_s or 0) - (lt.duration_s or 0)
                        pairs.append(TrackPair("number", lt, rt, _sim(lt.title, rt.title), d, abs(d) <= 5))
                taken = {id(rt) for rt in remotes if rt is not None}
                pairs += [TrackPair("unmatched", None, rt, 0.0, None, False)
                          for rt in self._tracklists[rel] if id(rt) not in taken]
            for i, lt in enumerate(album.tracks if multi is None else []):
                drift = 0 if c.year == album.year else (2 if i % 3 else 9)
                half = (len(album.tracks) + 1) // 2
                side, n = ("A", i + 1) if i < half else ("B", i + 1 - half)
                vinyl = any("Vinyl" in f or "Cassette" in f for f in c.formats)
                pos = f"{side}{n}" if vinyl else str(i + 1)
                rt = RemoteTrack(pos, 1, i + 1, lt.title,
                                 album.artist, (lt.duration_s or 0) + drift)
                d = rt.duration_s - lt.duration_s
                pairs.append(TrackPair("side" if vinyl else "number", lt, rt, _sim(lt.title, rt.title), d, abs(d) <= 5))
            if pairs and album.title == "Everything Went Black":     # MOCK: last track paired by position only
                pairs[-1] = replace(pairs[-1], method="position")
            if pairs and album.title == "Slip It In":                # MOCK: one file with no Discogs track
                pairs[-1] = TrackPair("unmatched", pairs[-1].local, None, 0.0, None, False)
            # contract "Scoring" (4dc5e69): score = 75% --auto score + 20% track count (all or nothing) + 5%
            # durations (share of timed pairs within 5 s), as shares of the parts known
            auto = c.auto_score if c.auto_score is not None else c.score
            remote_n = (len(self._tracklists[rel]) if multi is not None
                        else sum(1 for p in pairs if p.remote is not None))
            count = 1.0 if remote_n == len(album.tracks) else 0.0
            timed = [p for p in pairs if p.duration_delta_s is not None]
            dur = sum(1 for p in timed if abs(p.duration_delta_s) <= DURATION_TOLERANCE_S) / len(timed) if timed else None
            known = SEARCH_SHARE + TRACK_COUNT_WEIGHT + (DURATIONS_WEIGHT if dur is not None else 0.0)
            search = [replace(sp, weight=round(sp.weight * SEARCH_SHARE / known, 6))
                      for sp in c.breakdown if sp.field not in ("track_count", "durations")]
            parts = search + [
                ScorePart("track_count", TRACK_COUNT_WEIGHT / known, count, str(len(album.tracks)), str(remote_n),
                          count == 1.0),
                ScorePart("durations", DURATIONS_WEIGHT / known if dur is not None else 0.0, dur or 0.0,
                          "", "", dur is not None and dur >= 0.8)]
            score = min((SEARCH_SHARE * auto + TRACK_COUNT_WEIGHT * count
                         + (DURATIONS_WEIGHT * dur if dur is not None else 0.0)) / known, 1.0)
            out.append(replace(c, breakdown=parts, alignment=pairs, fetched=True, score=round(score, 4),
                               auto_score=auto))
        # sorted by score, margin by score (contract); confidence = best score once fetched
        out.sort(key=lambda c: -c.score)
        out = [replace(c, margin=round(c.score - out[i + 1].score, 4) if i + 1 < len(out) else None)
               for i, c in enumerate(out)]
        self._cands[aid] = out
        if out and any(c.fetched for c in out):     # best score once something is fetched
            self._albums[aid] = replace(self._albums[aid], confidence=out[0].score)   # index update, not a user write
            self.events.album_changed.emit(aid)
        self.events.candidate_ready.emit(aid, rel)
        if pace:
            self.events.rate_limited.emit(self._timer.interval() / 1000)

    # -------- library
    def albums(self, filter: AlbumFilter) -> list[Album]:
        """Unsorted, unpaged filter over the mock albums (fake_contract sorts and pages)."""
        return [a for a in self._albums.values()
                if (filter.status is None or a.status in filter.status)
                and (filter.max_confidence is None or (a.confidence or 0) < filter.max_confidence)
                and (filter.text.lower() in f"{a.artist} {a.title}".lower())]

    def album(self, album_id: int) -> Album:
        return self._albums[album_id]

    # -------- matching
    def candidates(self, album_id: int) -> list[Candidate]:
        return self._cands[album_id]

    def _set(self, album_id: int, **kw):
        self._albums[album_id] = replace(self._albums[album_id], **kw)
        self.events.album_changed.emit(album_id)

    def choose(self, album_id: int, release_id: int):
        self._chosen[album_id] = release_id
        self._set(album_id, status=MatchStatus.ACCEPTED, held_for=[])

    def skip(self, album_id: int):
        self._set(album_id, status=MatchStatus.SKIPPED, held_for=[])

    def submit_link(self, album_id: int) -> str:
        return "https://www.discogs.com/release/add"
