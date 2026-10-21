"""Process-wide service state: config, library index, job runner, Discogs client, live plans."""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import config as config_mod
from .. import fs as fs_mod
from .. import journal as journal_mod
from ..index import LibraryIndex
from ..providers import discogs as discogs_mod
from ._events import Events
from ._jobs import JobRunner
from ._types import ConfigError

# The one events object. It outlives configure()/reset() so UI connections stay valid.
events = Events()


@dataclass
class State:
    config_path: Path
    data_dir: Path
    index: LibraryIndex
    runner: JobRunner
    discogs: Any = None  # Discogs client (or a stand-in with the same methods); created on first use
    plans: dict[int, Any] = field(default_factory=dict)  # plan id -> _PlanRecord / _UndoRecord
    undo_previews: dict[int, int] = field(default_factory=dict)  # operation id -> latest preview plan id
    # plan.build's background release fetches: (album id, release id) -> job id, and recent failures
    fetching: dict[tuple[int, int], int] = field(default_factory=dict)
    fetch_failures: dict[tuple[int, int], tuple[float, str]] = field(default_factory=dict)
    cover_fetching: dict[str, int] = field(default_factory=dict)  # Discogs cover URL -> FETCH_COVER job id
    clusters: dict[str, Any] = field(default_factory=dict)  # cluster id -> Cluster (last library.clusters)
    cluster_keys: dict[str, str] = field(default_factory=dict)  # cluster id -> FileCluster.key
    lock: threading.RLock = field(default_factory=threading.RLock)
    ids: Any = field(default_factory=lambda: itertools.count(1))
    _config: config_mod.Config | None = None

    def config(self) -> config_mod.Config:
        with self.lock:
            if self._config is None:
                try:
                    self._config = config_mod.load_config(self.config_path)
                except config_mod.ConfigFileError as exc:
                    raise ConfigError(str(exc)) from exc
            return self._config

    def save_config(self, cfg: config_mod.Config) -> None:
        with self.lock:
            config_mod.save_config(cfg, self.config_path)
            self._config = cfg

    def next_id(self) -> int:
        with self.lock:
            return next(self.ids)

    def client(self):
        with self.lock:
            if self.discogs is None:
                token, key, secret = discogs_mod.load_token()
                if not token and not (key and secret):
                    raise ConfigError("No Discogs token. Generate one at https://www.discogs.com/settings/developers "
                                      "and save it with: discogs-tag --save-token")
                self.discogs = discogs_mod.Discogs(token, key, secret)
            if getattr(self.discogs, "on_wait", None) is None:
                try:
                    self.discogs.on_wait = events.rate_limited.emit
                except AttributeError:
                    pass
            return self.discogs


_state: State | None = None
_state_lock = threading.Lock()


_saved_paths: tuple | None = None  # module paths as they were before configure() moved them


def _redirect_paths(data_dir: Path | None, config_dir: Path | None) -> None:
    """Point every file the engine writes or reads inside the configured directories.

    data_dir: discogs-tag journals (data_dir/state/journal), the Discogs
    response cache (data_dir/cache/discogs) and the trash (data_dir/Trash).
    config_dir: the Discogs token (config_dir/discogs-token). reset() puts them back.
    """
    global _saved_paths
    if data_dir is None and config_dir is None:
        return
    _saved_paths = (journal_mod.STATE_DIR, discogs_mod.CACHE_DIR, discogs_mod.TOKEN_PATH, fs_mod.TRASH_DIR)
    if data_dir is not None:
        journal_mod.STATE_DIR = data_dir / "state"
        discogs_mod.CACHE_DIR = data_dir / "cache" / "discogs"
        fs_mod.TRASH_DIR = data_dir / "Trash"
    if config_dir is not None:
        discogs_mod.TOKEN_PATH = config_dir / "discogs-token"


def _restore_paths() -> None:
    global _saved_paths
    if _saved_paths is not None:
        journal_mod.STATE_DIR, discogs_mod.CACHE_DIR, discogs_mod.TOKEN_PATH, fs_mod.TRASH_DIR = _saved_paths
        _saved_paths = None


def configure(data_dir: Path | None = None, config_dir: Path | None = None, discogs: Any = None) -> None:
    """(Re)start the service with these directories and, optionally, a Discogs client stand-in.

    Defaults: $XDG_DATA_HOME/music-manager and $XDG_CONFIG_HOME/music-manager.
    A directory passed here also holds the files the engine would otherwise
    keep under ~ (see _redirect_paths), so an embedder that configures both
    never touches the home folder. Mainly for tests and for embedding; the UI
    just imports the module.
    """
    global _state
    with _state_lock:
        _shutdown_locked()
        _redirect_paths(Path(data_dir) if data_dir else None, Path(config_dir) if config_dir else None)
        data = Path(data_dir) if data_dir else config_mod.data_dir()
        conf = Path(config_dir) if config_dir else config_mod.config_dir()
        _state = State(config_path=conf / "config.toml", data_dir=data,
                       index=LibraryIndex(data / "library.db"), runner=JobRunner(events), discogs=discogs)


def reset() -> None:
    """Stop the worker and close the index; the next call starts fresh from the environment."""
    with _state_lock:
        _shutdown_locked()


def _shutdown_locked() -> None:
    global _state
    if _state is not None:
        _state.runner.shutdown()
        _state.index.close()
        _state = None
    _restore_paths()


def state() -> State:
    global _state
    with _state_lock:
        if _state is None:
            data, conf = config_mod.data_dir(), config_mod.config_dir()
            _state = State(config_path=conf / "config.toml", data_dir=data,
                           index=LibraryIndex(data / "library.db"), runner=JobRunner(events))
        return _state
