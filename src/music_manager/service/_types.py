"""Types of the service API (API-CONTRACT.md v0). All dataclasses are frozen."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, StrEnum
from pathlib import Path
from typing import Generic, Literal, TypeVar

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
    DEST_INSIDE_SOURCE = "dest_inside_source"  # library or target folder is inside the album
    SOURCE_INSIDE_TARGET = "source_inside_target"
    SYMLINK_OUTSIDE = "symlink_outside"  # paths lists every offending link
    COLLISION = "collision"
    TRACK_MISMATCH = "track_mismatch"  # release track count differs from local files
    LAYOUT_AMBIGUOUS = "layout_ambiguous"  # loose audio next to disc folders / other albums; never overridden
    DISC_AMBIGUOUS = "disc_ambiguous"  # multi-disc: files whose disc can't be told (paths); never overridden
    FILE_CHANGED = "file_changed"  # edited/moved since plan or apply: rebuild / skipped on undo
    LEGACY_JOURNAL = "legacy_journal"  # undo of an old-format step needs trust_legacy=True
    RELEASE_NOT_FETCHED = "release_not_fetched"  # plan.build: release still downloading (Plan.fetch_job)


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


@dataclass(frozen=True)
class Problem:
    kind: ProblemKind
    message: str  # plain text for the dialog
    paths: list[Path]


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


@dataclass(frozen=True)
class TrackPair:
    method: Literal["number", "side", "title", "position", "unmatched"]
    local: LocalTrack | None  # None: Discogs track with no local file
    remote: RemoteTrack | None  # None: local file with no Discogs track
    title_similarity: float
    duration_delta_s: float | None
    ok: bool


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


@dataclass(frozen=True)
class AlbumPlan:
    album_id: int
    release_id: int
    folder_before: Path
    folder_after: Path
    files: list[FileChange]
    problems: list[Problem]  # any problem blocks apply for this album
    artist: str = ""  # the release's artist and title ("" when unknown, e.g. an old journal)
    title: str = ""


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
    save_cover_art: bool = True
    clear_other_tags: bool = False
