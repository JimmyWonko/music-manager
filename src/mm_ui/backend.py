"""Service adapter: the one place that decides which backend the UI talks to.

    MM_BACKEND=fake   (default) in-memory mock (FakeService behind a contract facade)
    MM_BACKEND=real   Bassline's `music_manager.service`, imported from MM_BACKEND_PATH
                      (default: this repository's src when mm_ui sits in it, else /workspace/mm-fix/music-manager).
                      No fallback: if it can't be loaded, get_service() raises BackendUnavailable.
    MM_BACKEND=auto   try real, fall back to fake with a warning.
    MM_DATA_DIR / MM_CONFIG_DIR   (real only) passed to service.configure(): where the index, journals and
                      config.toml live instead of the XDG defaults.

Real backend notes: service.events are Qt Signals emitted from the service's one worker thread; the UI
connects bound methods with the default AutoConnection so slots run queued on the Qt thread. Calls that
can block (plan.build may fetch from Discogs) go through mm_ui.worker.run_async, never on the Qt thread.

The real checkout is treated as READ-ONLY: bytecode writing is disabled for the whole process
before importing from it (sys.dont_write_bytecode + PYTHONDONTWRITEBYTECODE), nothing is built,
installed or written there.

UI code must only call contract methods on the returned Backend (library/match/provider/plan/
journal/templates/settings/jobs/events/types) plus the small helpers defined on Backend below.
Anything mock-only is reached through Backend helpers that degrade on the real backend.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

# Inside the music-manager repository (src/mm_ui next to src/music_manager) the default is that src;
# the standalone working copy defaults to Bassline's checkout.
_HERE_SRC = Path(__file__).resolve().parent.parent
DEFAULT_REAL_PATH = (str(_HERE_SRC) if (_HERE_SRC / "music_manager" / "service").is_dir()
                     else "/workspace/mm-fix/music-manager")
SERVICE_MODULE = "music_manager.service"

# Contract surface the UI relies on (API-CONTRACT.md v0). Checked when loading the real backend.
REQUIRED = {
    "library": ["albums", "status_counts", "album"],
    "match": ["candidates", "choose", "destination", "skip"],
    "provider": ["submit_link"],
    "plan": ["build", "apply"],
    "journal": ["list", "undo_plan", "undo"],
    "templates": ["list", "save", "delete", "preview"],
    "settings": ["get", "set"],
    "events": ["job_finished", "job_progress", "album_changed", "candidate_ready", "rate_limited"],
}
REQUIRED_TYPES = ["AlbumFilter", "AlbumSort", "MatchStatus", "JobState", "MMError", "ValidationError"]


class BackendUnavailable(RuntimeError):
    """The requested backend could not be loaded. str() is a user-facing explanation."""


class Backend:
    """Contract namespaces + a few UI helpers. Same surface for fake and real."""

    def __init__(self, contract, kind: str, origin: str):
        self.kind, self.origin, self._c = kind, origin, contract
        for ns in ("library", "match", "provider", "plan", "journal", "templates", "settings", "jobs", "events"):
            setattr(self, ns, getattr(contract, ns, None))
        self.types = getattr(contract, "types", None) or contract  # real: types live on the module

    is_fake = property(lambda self: self.kind == "fake")
    label = property(lambda self: "mock data" if self.is_fake else f"backend: {SERVICE_MODULE}")
    # full origin (where the module was imported from) for tooltips / About; the footer shows the short label
    label_detail = property(lambda self: "In-memory mock (FakeService)" if self.is_fake
                            else f"{SERVICE_MODULE} from {self.origin}")

    # -------- tiny helpers so UI code can name statuses/sorts without importing a backend's enums
    def statuses(self, names) -> set | None:
        """{"NEEDS_REVIEW", ...} -> set of this backend's MatchStatus members (None = all)."""
        if names is None:
            return None
        return {self.types.MatchStatus[n] for n in names}

    def album_filter(self, status_names=None, text: str = "", max_confidence=None):
        return self.types.AlbumFilter(status=self.statuses(status_names), max_confidence=max_confidence, text=text)

    def sort(self, key: str):
        """AlbumSort member by value ("artist", "year", ...)."""
        return self.types.AlbumSort(key)

    def albums(self, status_names=None, text: str = "", offset: int = 0, limit: int = 200, sort=None,
               descending: bool = False):
        return self.library.albums(self.album_filter(status_names, text),
                                   sort if sort is not None else self.types.AlbumSort.ARTIST, offset, limit,
                                   descending=descending)

    def active_template(self) -> str:
        """Template.id of Settings.active_template (what `template` parameters take)."""
        return self.settings.get().active_template

    @staticmethod
    def relative(path, root) -> str:
        """Contract paths are absolute; show them relative to their library_root when inside it."""
        try:
            return str(path.relative_to(root))
        except (ValueError, AttributeError, TypeError):
            return str(path)

    def sample_album_ids(self, n: int = 6) -> list[int]:
        return [a.id for a in self.albums(None, "", 0, n).items]


# ---------------------------------------------------------------- loading
def _load_real(path: str) -> Backend:
    root = Path(path).expanduser()
    if not root.is_dir():
        raise BackendUnavailable(f"Real backend path does not exist: {root}\n"
                                 f"Set MM_BACKEND_PATH to Bassline's checkout.")
    # READ-ONLY checkout: never leave __pycache__ there.
    sys.dont_write_bytecode = True
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    search = [p for p in (root / "src", root) if (p / "music_manager").is_dir()]
    if not search:
        raise BackendUnavailable(
            f"No 'music_manager' package in {root} (looked in {root / 'src'} and {root}).\n"
            f"Missing: {SERVICE_MODULE} — the service layer from Bassline's refactor isn't in this checkout yet.")
    pkg_root = str(search[0])
    if pkg_root not in sys.path:
        sys.path.insert(0, pkg_root)
    try:
        mod = importlib.import_module(SERVICE_MODULE)
    except ModuleNotFoundError as e:
        what = SERVICE_MODULE if e.name in ("music_manager", SERVICE_MODULE) else f"dependency '{e.name}'"
        raise BackendUnavailable(f"Could not import {SERVICE_MODULE} from {pkg_root}.\nMissing: {what} ({e})") from e
    except Exception as e:
        raise BackendUnavailable(f"Importing {SERVICE_MODULE} from {pkg_root} failed: {type(e).__name__}: {e}") from e
    origin = str(Path(getattr(mod, "__file__", "?")).resolve())
    if not origin.startswith(str(Path(pkg_root).resolve())):
        raise BackendUnavailable(f"{SERVICE_MODULE} was imported from {origin}, not from {pkg_root} "
                                 f"(another copy is installed and shadows the checkout).")
    missing = [f"{ns}.{fn}" for ns, fns in REQUIRED.items() for fn in fns
               if not hasattr(getattr(mod, ns, None), fn)]
    missing += [t for t in REQUIRED_TYPES if not hasattr(mod, t)]
    if missing:
        raise BackendUnavailable(f"{SERVICE_MODULE} at {origin} is missing contract pieces the UI needs: "
                                 + ", ".join(missing))
    # Optional: point the service's config/index/journals somewhere else (tests, sandboxes). The real service
    # otherwise uses $XDG_CONFIG_HOME/music-manager and $XDG_DATA_HOME/music-manager (API-DEVIATIONS #14).
    data_dir, config_dir = os.environ.get("MM_DATA_DIR"), os.environ.get("MM_CONFIG_DIR")
    if (data_dir or config_dir) and hasattr(mod, "configure"):
        mod.configure(data_dir=Path(data_dir) if data_dir else None, config_dir=Path(config_dir) if config_dir else None)
    return Backend(mod, "real", origin)


def fake_backend(fake=None, synthetic: int | None = None, **fake_kw) -> Backend:
    from .fake_contract import FakeContract
    from .fake_service import FakeService
    fake = fake or FakeService(**fake_kw)
    contract = getattr(fake, "_contract", None)        # one facade (journal/templates) per FakeService
    if contract is None:
        if synthetic is None:
            synthetic = int(os.environ.get("MM_FAKE_SYNTHETIC", "100000"))
        contract = fake._contract = FakeContract(fake, synthetic=synthetic)
    return Backend(contract, "fake", "FakeService")


def _stderr(msg: str, fatal: bool) -> None:
    print(("ERROR: " if fatal else "WARNING: ") + msg, file=sys.stderr, flush=True)


_service: Backend | None = None


def get_service(notify=_stderr) -> Backend:
    """Process-wide backend (created once). notify(msg, fatal) reports load problems."""
    global _service
    if _service is not None:
        return _service
    mode = os.environ.get("MM_BACKEND", "fake").strip().lower() or "fake"
    path = os.environ.get("MM_BACKEND_PATH", DEFAULT_REAL_PATH)
    if mode == "fake":
        _service = fake_backend()
    elif mode in ("real", "auto"):
        try:
            _service = _load_real(path)
        except BackendUnavailable as e:
            if mode == "real":
                notify(f"{e}\n(MM_BACKEND=real, so not falling back to the mock.)", True)
                raise
            notify(f"{e}\nFalling back to the mock backend (MM_BACKEND=auto).", False)
            _service = fake_backend()
    else:
        msg = f"Unknown MM_BACKEND={mode!r}; use fake, real or auto."
        notify(msg, True)
        raise BackendUnavailable(msg)
    return _service


def reset_service() -> None:
    """Tests only: forget the cached backend."""
    global _service
    _service = None


def as_backend(svc=None) -> Backend:
    """Windows accept None (use get_service()), a Backend, or a raw FakeService (old scripts)."""
    if svc is None:
        return get_service()
    if isinstance(svc, Backend):
        return svc
    from .fake_service import FakeService
    if isinstance(svc, FakeService):
        return fake_backend(svc)
    raise TypeError(f"not a backend: {svc!r}")
