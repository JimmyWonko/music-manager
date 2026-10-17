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
    Collision,
    ConfigError,
    Destination,
    FileChange,
    Job,
    JobKind,
    JobState,
    LocalTrack,
    MatchStatus,
    MMError,
    NotFound,
    Operation,
    Page,
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
    "Collision", "ConfigError", "Destination", "Events", "FileChange", "Job", "JobKind", "JobState", "LocalTrack",
    "MMError", "MatchStatus", "NotFound", "Operation", "Page", "Plan", "PlanStale", "Problem", "ProblemKind",
    "ProviderCredit", "ProviderError", "RemoteTrack", "ScorePart", "Settings", "TagChange", "Template",
    "TemplatePreview", "TrackPair", "ValidationError", "configure", "events", "jobs", "journal", "library", "match",
    "plan", "provider", "reset", "settings", "templates",
]
