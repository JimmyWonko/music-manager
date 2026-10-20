"""music_manager.service: the public API of the core (see API-CONTRACT.md).

Namespaces: library, match, provider, plan, journal, templates, settings,
jobs, and the events object. Types, enums and errors are importable from
here too. Nothing writes to disk except plan.apply and journal.undo; slow
work runs as a Job on one background worker thread.

Configuration and data live under $XDG_CONFIG_HOME/music-manager and
$XDG_DATA_HOME/music-manager; configure() points them elsewhere (tests).
"""

from __future__ import annotations

from . import jobs, journal, library, match, plan, provider, settings, templates
from ._events import Events
from ._state import configure, events, reset
from ._types import (
    DISCOGS_CREDIT,
    Album,
    AlbumFilter,
    AlbumPlan,
    AlbumRow,
    AlbumSort,
    AutoRules,
    Candidate,
    Cluster,
    ClusterFile,
    ClusterRow,
    Collision,
    ConfigError,
    Destination,
    DiscChoice,
    DiscConflict,
    DiscFolderMismatch,
    FileChange,
    Job,
    JobKind,
    JobState,
    LayoutConflict,
    LocalTrack,
    ManualPair,
    MatchStatus,
    MatchView,
    MMError,
    NotFound,
    Operation,
    Page,
    Pin,
    Plan,
    PlanStale,
    Problem,
    ProblemKind,
    ProviderCredit,
    ProviderError,
    RemoteTrack,
    ScorePart,
    Settings,
    TagChange,
    Template,
    TemplatePreview,
    TrackPair,
    ValidationError,
)

__all__ = [
    "DISCOGS_CREDIT", "Album", "AlbumFilter", "AlbumPlan", "AlbumRow", "AlbumSort", "AutoRules", "Candidate",
    "Cluster", "ClusterFile", "ClusterRow", "Collision", "ConfigError", "Destination", "DiscChoice", "DiscConflict", "DiscFolderMismatch", "Events", "FileChange", "Job", "JobKind", "JobState", "LayoutConflict", "LocalTrack",
    "MMError", "ManualPair", "MatchStatus", "MatchView", "NotFound", "Operation", "Page", "Pin", "Plan", "PlanStale", "Problem",
    "ProblemKind",
    "ProviderCredit", "ProviderError", "RemoteTrack", "ScorePart", "Settings", "TagChange", "Template",
    "TemplatePreview", "TrackPair", "ValidationError", "configure", "events", "jobs", "journal", "library", "match",
    "plan", "provider", "reset", "settings", "templates",
]
