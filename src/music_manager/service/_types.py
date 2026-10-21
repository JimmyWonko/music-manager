"""Types of the service API (API-CONTRACT.md v0). All dataclasses are frozen."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, StrEnum
from pathlib import Path
from typing import Generic, Literal, TypeVar

from ..conflicts import DiscChoice, DiscConflict, DiscFolderMismatch, LayoutConflict  # noqa: F401  (re-exported)

T = TypeVar("T")


# --- errors -------------------------------------------------------------------

class MMError(Exception):
    """Base of every error the service raises."""


class NotFound(MMError):
    """No album, plan, operation, template ... with that id."""


class PlanStale(MMError):
    """The plan or undo preview no longer matches the disk or is not the latest one: rebuild it."""


class ValidationError(MMError):
    """Bad input (template, setting, sort key ...). field names the offending field."""

    def __init__(self, message: str, field: str = ""):
        super().__init__(message)
        self.field = field


class ProviderError(MMError):
    """Discogs failed. retry_after: seconds to wait before retrying, when known."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class ConfigError(MMError):
    """Missing or unreadable configuration (e.g. no library root configured, no Discogs token)."""


# --- enums --------------------------------------------------------------------

class JobKind(Enum):
    SCAN = "scan"
    MATCH = "match"
    FETCH_CANDIDATES = "fetch_candidates"
    APPLY = "apply"
    UNDO = "undo"
    CLUSTER = "cluster"  # library.scan_clusters
    FETCH_COVER = "fetch_cover"  # coverart.fetch / Plan.cover_job: Discogs front covers into the image cache


class JobState(Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MatchStatus(Enum):
    UNMATCHED = "unmatched"
    QUEUED = "queued"
    NEEDS_REVIEW = "needs_review"
    MATCHED = "matched"
    ACCEPTED = "accepted"
    SKIPPED = "skipped"
    NO_MATCH = "no_match"
    APPLIED = "applied"
    HELD = "held"  # sorts right after NEEDS_REVIEW; set by plan.apply on refused albums


# AlbumSort.STATUS review order
STATUS_ORDER = (
    MatchStatus.NEEDS_REVIEW, MatchStatus.HELD, MatchStatus.UNMATCHED, MatchStatus.QUEUED, MatchStatus.MATCHED,
    MatchStatus.ACCEPTED, MatchStatus.SKIPPED, MatchStatus.NO_MATCH, MatchStatus.APPLIED,
)


class AlbumSort(StrEnum):  # every sort ends with folder path as the tiebreak, so pages are stable
    ARTIST = "artist"  # albumartist sort name, then lead year, then title
    TITLE = "title"
    YEAR = "year"  # lead year, then artist
    ADDED = "added"  # newest scan first
    CONFIDENCE = "confidence"  # lowest first, unmatched (None) last
    STATUS = "status"  # review order: NEEDS_REVIEW, HELD, UNMATCHED, QUEUED, ... APPLIED
    FOLDER = "folder"


class ProblemKind(StrEnum):
    # Match on the string value, never on position: new kinds are appended at the end.
    DEST_INSIDE_SOURCE = "dest_inside_source"  # library or target folder is inside the album
    SOURCE_INSIDE_TARGET = "source_inside_target"
    SYMLINK_OUTSIDE = "symlink_outside"  # paths lists every offending link
    COLLISION = "collision"
    TRACK_MISMATCH = "track_mismatch"  # release track count differs from local files
    FILE_CHANGED = "file_changed"  # edited/moved since plan or apply: rebuild / skipped on undo
    LEGACY_JOURNAL = "legacy_journal"  # undo of an old-format step needs trust_legacy=True
    RELEASE_NOT_FETCHED = "release_not_fetched"  # plan.build: release still downloading (Plan.fetch_job)
    DISC_AMBIGUOUS = "disc_ambiguous"  # multi-disc: files whose disc can't be told (paths); never overridden
    LAYOUT_AMBIGUOUS = "layout_ambiguous"  # loose audio next to disc folders / other albums; never overridden
    DISC_FOLDER_MISMATCH = "disc_folder_mismatch"  # destination puts tracks in another disc's CD folder
    DISC_TRACK_COUNT = "disc_track_count"  # a disc's file and track counts differ, titles don't settle it (paths)
    PIN_CONFLICT = "pin_conflict"  # a manual pin (match.pin) can't be used as it is (paths: its files)


# --- jobs ---------------------------------------------------------------------

@dataclass(frozen=True)
class Job:
    id: int
    kind: JobKind
    state: JobState
    done: int
    total: int | None
    message: str


@dataclass(frozen=True)
class Page(Generic[T]):
    items: list[T]
    total: int
    offset: int


# --- library ------------------------------------------------------------------

@dataclass(frozen=True)
class ProviderCredit:
    name: str
    text: str
    url: str


DISCOGS_CREDIT = ProviderCredit("Discogs", "Data provided by Discogs", "https://www.discogs.com/")


PinConflictReason = Literal["stale", "unknown_position", "duplicate_position", "override_hold_required",
                            "disc_folder_mismatch", "not_subtrack_group"]


@dataclass(frozen=True)
class PinConflict:
    """Why one manual pin (match.pin) is not used."""
    path: Path
    position: str  # as pinned
    # stale: the file changed or left the album since it was pinned; unknown_position: not on the release;
    # duplicate_position: another pin names the same track; override_hold_required: the engine holds this
    # file (disc_ambiguous / disc_track_count); disc_folder_mismatch: the file sits in another disc's CD
    # folder. Those two are used with override_hold=True. not_subtrack_group: a group pin (positions) that
    # is not exactly one subtrack group of the release, in order (2026-10-21).
    reason: PinConflictReason


@dataclass(frozen=True)
class Problem:
    kind: ProblemKind
    message: str  # plain text for the dialog
    paths: list[Path]
    # DISC_AMBIGUOUS: one entry per refused file, in file order (the same files as paths).
    disc_conflicts: list[DiscConflict] = field(default_factory=list)
    # LAYOUT_AMBIGUOUS ("" / [] otherwise); paths stays [album folder].
    layout_reason: str = ""  # "disc_folders" (CD1/CD2 with audio) or "album_folders" (other folders with audio)
    blocking_subdirs: list[Path] = field(default_factory=list)  # the subfolders that caused the hold
    loose_files: list[Path] = field(default_factory=list)  # the audio files directly in the album folder
    # DISC_FOLDER_MISMATCH: one per track the destination puts into another disc's CD folder (same files as paths).
    folder_mismatches: list[DiscFolderMismatch] = field(default_factory=list)
    # PIN_CONFLICT: one per refused pin (the same files as paths).
    pin_conflicts: list[PinConflict] = field(default_factory=list)


@dataclass(frozen=True)
class AlbumFilter:
    status: set[MatchStatus] | None = None
    max_confidence: float | None = None  # e.g. 0.90 for "conf < 90%"
    text: str = ""


@dataclass(frozen=True)
class LocalTrack:
    id: int
    path: Path
    disc: int
    number: int | None
    title: str
    duration_s: float | None


@dataclass(frozen=True)
class AlbumRow:
    id: int
    folder: Path
    artist: str
    title: str
    year: str
    catno: str
    track_count: int
    status: MatchStatus
    confidence: float | None
    artwork: Path | None
    artist_sort: str  # ALBUMARTISTSORT tag if present, else artist; what ARTIST sorts by
    added_at: datetime  # when a scan first saw this folder (timezone-aware UTC)
    held_for: list[Problem] = field(default_factory=list)  # empty unless status is HELD
    loose_tracks: bool = False  # loose files next to subfolders with audio: artist = folder name, title ""
    cluster_id: str | None = None  # set when this album is an adopted cluster (library.adopt_cluster)


@dataclass(frozen=True)
class Album(AlbumRow):
    tracks: list[LocalTrack] = field(default_factory=list)
    extras: list[Path] = field(default_factory=list)


# --- matching -----------------------------------------------------------------

@dataclass(frozen=True)
class ScorePart:
    field: str  # "album", "artist", "catno", "year", "track_count", "durations"
    weight: float  # share of the total score
    value: float  # 0..1 for this field
    local: str
    remote: str
    ok: bool


@dataclass(frozen=True)
class RemoteTrack:
    position: str
    disc: int
    number: int
    title: str
    artist: str
    duration_s: float | None
    subtracks: list[str] = field(default_factory=list)  # merged subtrack positions ["2-9a", "2-9b"] (2026-10-21)


@dataclass(frozen=True)
class TrackPair:
    # manual: match.pin; subtracks: the file is merged subtracks (automatic or a group pin), see positions
    method: Literal["number", "side", "title", "position", "manual", "unmatched", "subtracks"]
    local: LocalTrack | None  # None: Discogs track with no local file
    remote: RemoteTrack | None  # None: local file with no Discogs track
    title_similarity: float
    duration_delta_s: float | None
    ok: bool
    positions: list[str] = field(default_factory=list)  # method "subtracks": ["2-9a", "2-9b"]; else [] (2026-10-21)


FoundBy = Literal["barcode", "artist_album", "catno", "album", "text", "manual", "manual_search", ""]


@dataclass(frozen=True)
class MatchReason:
    """One plain-language reason for (or against) a candidate, for the review screen."""
    field: str  # "barcode", "album", "artist", "catno", "year", "track_count", "durations"
    ok: bool  # True: speaks for this release
    text: str  # e.g. "Catalog number LT-2CD matches"


@dataclass(frozen=True)
class Candidate:
    release_id: int
    master_id: int | None
    artist: str
    title: str
    year: str
    country: str
    label: str
    catno: str
    formats: list[str]
    score: float  # 0..1
    margin: float | None  # score minus runner-up's; None if only candidate
    breakdown: list[ScorePart]
    alignment: list[TrackPair] | None  # None until the full release is fetched
    artwork_url: str | None
    thumb_url: str | None
    artwork: Path | None
    fetched: bool
    source: ProviderCredit
    auto_score: float | None = None  # the --auto score (search-result parts only); auto_accept uses it
    # which search found it: barcode, artist_album, catno, album, text (title only), manual (release id/URL),
    # manual_search (match.search); "" when unknown (found before 2026-10-21)
    found_by: FoundBy = ""
    reasons: list[MatchReason] = field(default_factory=list)  # barcode first (if known), then breakdown order


@dataclass(frozen=True)
class AutoRules:
    min_score: float = 0.90  # AUTO_MIN_SCORE
    min_margin: float = 0.10  # AUTO_MIN_MARGIN, lead over the runner-up
    require_track_count: bool = True  # False = --allow-track-mismatch
    title_match_min: float = 0.80  # TITLE_MATCH_MIN for title-pass pairing


@dataclass(frozen=True)
class Destination:
    library_root: Path
    folder: Path  # absolute; always inside library_root
    files: list[tuple[Path, Path]]  # before, after (empty until fetched)
    problems: list[Problem]


# --- clusters and the album-match view -------------------------------------------

@dataclass(frozen=True)
class ClusterFile:
    path: Path
    disc: int  # DISCNUMBER tag, else CD folder / "2-01" name, else 1
    number: int | None
    title: str
    duration_s: float | None
    album: str  # the file's own album tag ("" if none); what it was clustered by
    artist: str  # its albumartist tag, else artist tag


@dataclass(frozen=True)
class Cluster:
    id: str  # "cl-" + hash of its files: stable while the set of files is the same
    artist: str  # most common (album)artist of its files
    title: str  # most common album tag (folder name for files without one)
    folder: Path  # the folder holding the files (their common parent)
    files: list[ClusterFile]  # path order
    disc_count: int  # distinct disc numbers
    album_id: int | None = None  # the album row once adopted (library.adopt_cluster), else None
    kind: str = "tag_cluster"  # or "unclustered": files moved out of every cluster (move_to_cluster None)


@dataclass(frozen=True)
class ClusterRow:
    """One line of match.clusters: a folder album, a tag cluster (adopted or not), or unclustered files."""
    id: str  # "album-<album id>" for a folder album, else the Cluster id ("cl-..." / "un-...")
    kind: Literal["folder", "tag_cluster", "unclustered"]
    artist: str
    title: str
    file_count: int
    status: MatchStatus | None  # None for a cluster that is not adopted yet
    album_id: int | None
    loose_tracks: bool  # its files are loose files of a folder that also holds albums
    folder: Path


@dataclass(frozen=True)
class Pin:
    """A manual pair (drag-and-drop): this file is this release track."""
    path: Path
    position: str  # the release track's position as Discogs lists it ("4", "B2", "2-3")
    release_id: int  # pins apply to this release only
    override_hold: bool  # the user accepted overriding a disc hold for this file
    stale: bool  # the file changed since it was pinned: ignored (a PIN_CONFLICT) until pinned again
    # the pinned positions: [position] for one track; a subtrack group ["2-9a", "2-9b"] for one file that is
    # several subtracks (then position is "2-9a+2-9b"). 2026-10-21.
    positions: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ManualPair:
    """A manual pair recorded in an apply journal (journal.manual_pairs)."""
    album_id: int
    path: Path  # the file before the apply
    position: str
    override_hold: bool


@dataclass(frozen=True)
class SubtrackGroup:
    """Release tracks sharing one base position (2-9a + 2-9b, 9.1 + 9.2, an index track's sub_tracks), which
    a rip often has as ONE file. merged: they are one release track here (position "2-9", title "A / B").

    reason: "pinned" (a group pin), "durations_fit" (merged); "not_needed", "ambiguous", "durations_unknown",
    "durations_differ", "still_held", "split" (match.split_subtracks), "merge_off" (settings merge_subtracks
    False) - not merged; see match.collapse_subtracks.
    """
    disc: int
    position: str  # the base: "2-9"
    positions: list[str]  # ["2-9a", "2-9b"]
    titles: list[str]
    merged: bool
    reason: str
    path: Path | None  # the file paired with the merged track; None when not merged (or unpaired)
    duration_s: float | None  # summed Discogs length; None unless every subtrack lists one


@dataclass(frozen=True)
class MatchView:
    album_id: int | None  # None: a cluster not adopted yet (no candidates, every file unmatched)
    cluster_id: str | None
    release_id: int | None  # the release shown: asked for, else chosen, else best candidate
    fetched: bool  # its full tracklist is known (pairs are empty until then)
    candidates: list[Candidate]  # best first, as match.candidates
    pairs: list[TrackPair]  # every pairing, as Candidate.alignment (local file -> release track)
    unmatched_files: list[LocalTrack]  # local files no release track was paired with
    unmatched_tracks: list[RemoteTrack]  # release tracks with no local file
    problems: list[Problem]  # what plan.build would hold this album for with this release
    pins: list[Pin] = field(default_factory=list)  # the album's pins for this release
    # the release's subtrack groups (2-9a/2-9b ...), merged or not and why; the UI shows a merged one as one row
    subtrack_groups: list[SubtrackGroup] = field(default_factory=list)


# --- plans ----------------------------------------------------------------------

@dataclass(frozen=True)
class TagChange:
    key: str
    before: str | None
    after: str | None


@dataclass(frozen=True)
class FileChange:
    kind: str  # "track", "extra", "playlist"
    path_before: Path
    path_after: Path
    tags: list[TagChange]
    skip_reason: str | None = None  # None means the step runs
    pinned: bool = False  # a track paired by the user (match.pin)
    merged_subtracks: list[str] = field(default_factory=list)  # this file is these subtracks ["2-9a", "2-9b"]


CoverSource = Literal["folder_first", "discogs_first", "folder", "discogs"]
COVER_SOURCES = ("folder_first", "discogs_first", "folder", "discogs")


@dataclass(frozen=True)
class CoverArt:
    """A front cover image: what plan.apply would embed, or what a track has embedded."""
    source: Literal["folder", "discogs", "embedded"]
    mime: str  # "image/jpeg" or "image/png" (embedded art may be anything)
    width: int  # pixels; 0 when unknown
    height: int
    size: int  # bytes
    path: Path | None  # folder art: the image file; embedded: the track it came from
    url: str | None  # Discogs: the image URL
    data: bytes = field(default=b"", repr=False)  # the image itself, for the preview


@dataclass(frozen=True)
class CoverPreview:
    """coverart.preview: the album's cover art as plan.build would handle it now."""
    album_id: int
    cover: CoverArt | None  # what would be embedded (None: nothing to embed, see message)
    existing: CoverArt | None  # the first track's embedded front cover, if any
    files_to_embed: int  # tracks that would get cover
    files_keeping: int  # tracks that keep their own front cover (replace_cover_art False or same image)
    pending_url: str | None  # a Discogs cover that is not downloaded yet (coverart.fetch)
    message: str  # why there is no cover, or ""


@dataclass(frozen=True)
class CoverDefaults:
    embed_cover_art: bool = False  # opt-in: saving folder.jpg (save_cover_art) is the norm
    replace_cover_art: bool = False  # False: tracks with front art keep it
    cover_source: CoverSource = "folder_first"
    cover_max_bytes: int = 0  # 0: no limit; larger images are not embedded (never resized)


@dataclass(frozen=True)
class AlbumPlan:
    album_id: int
    release_id: int
    folder_before: Path
    folder_after: Path
    files: list[FileChange]
    problems: list[Problem]  # any problem blocks apply for this album
    artist: str = ""  # the release's artist and title ("" when unknown, e.g. an old journal);
    # for loose tracks (AlbumRow.loose_tracks) the folder's name and ""
    title: str = ""
    cover: CoverArt | None = None  # front cover plan.apply embeds (FileChange.tags has "cover_art" per file)


@dataclass(frozen=True)
class Plan:
    id: int
    albums: list[AlbumPlan]
    warnings: list[str]
    files_to_move: int
    tags_to_write: int
    bytes_to_copy: int  # copies only for cross-drive
    skipped: int = 0  # steps with FileChange.skip_reason set; counted nowhere else
    fetch_job: int | None = None  # job fetching releases this plan lacks; rebuild when it finishes
    cover_job: int | None = None  # job downloading Discogs covers this plan lacks (JobKind.FETCH_COVER)


@dataclass(frozen=True)
class Operation:
    id: int
    started: datetime
    finished: datetime | None
    albums: int
    files: int
    kind: str  # "apply", "normalize", "undo"
    undoable: bool
    undone: bool
    undone_by: int | None
    undo_of: int | None
    summary: str
    journal_format: int = 2  # 1 = old pickle snapshots, 2 = JSON
    legacy: bool = False  # journal_format < 2


# --- templates and settings -------------------------------------------------------

@dataclass(frozen=True)
class Template:
    id: str  # "archivist", "simple", "edition-detail", "label-archive", or "user:<slug>"
    name: str
    text: str
    builtin: bool


@dataclass(frozen=True)
class Collision:
    target: Path
    album_ids: list[int]


@dataclass(frozen=True)
class TemplatePreview:
    moves: list[tuple[Path, Path]]
    collisions: list[Collision]
    warnings: list[str]


@dataclass(frozen=True)
class Settings:
    library_roots: list[Path]
    active_template: str
    sequential_sides: bool = False
    save_cover_art: bool = True  # save folder.jpg from Discogs when the album folder has no cover/folder/front image
    clear_other_tags: bool = False
    # cover art embedding: effective values (the user's, else the active template's preset default);
    # settings.set(name=None) goes back to the preset default
    embed_cover_art: bool = False  # opt-in
    replace_cover_art: bool = False
    cover_source: CoverSource = "folder_first"
    cover_max_bytes: int = 0
    merge_subtracks: bool = True  # automatic subtrack merging (2026-10-21); group pins merge either way
