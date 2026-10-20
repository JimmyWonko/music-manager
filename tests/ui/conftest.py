import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"       # Bassline's checkout is read-only: never write bytecode there
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

pytest.importorskip("PySide6", reason="UI tests need PySide6 (pip install .[ui])")
pytest.importorskip("pytestqt", reason="UI tests need pytest-qt")

from ui_env import BACKEND_PATH, BASSLINE, SUITE_BACKEND  # noqa: E402,F401  (also puts mm_ui on sys.path)
from mm_ui import backend as backend_mod  # noqa: E402


def _checkout_commit(root: Path) -> str:
    """HEAD of the checkout, read from .git files directly (no git process, nothing written)."""
    try:
        head = (root / ".git" / "HEAD").read_text().strip()
        if head.startswith("ref: "):
            ref = head[5:]
            f = root / ".git" / ref
            if f.is_file():
                return f"{ref.rsplit('/', 1)[-1]}@{f.read_text().strip()[:7]}"
            for line in (root / ".git" / "packed-refs").read_text().splitlines():
                if line.endswith(" " + ref):
                    return f"{ref.rsplit('/', 1)[-1]}@{line[:7]}"
        return head[:7]
    except OSError:
        return "not a git checkout"


def pytest_report_header(config):
    return (f"mm_ui suite backend: {SUITE_BACKEND} (real backend: {BACKEND_PATH}, "
            f"{_checkout_commit(BASSLINE)})")


def pytest_collection_modifyitems(config, items):
    if SUITE_BACKEND != "real":
        skip = pytest.mark.skip(reason="real-backend integration: run with MM_BACKEND=real")
        for it in items:
            if it.get_closest_marker("real_backend"):
                it.add_marker(skip)


def pytest_configure(config):
    config.addinivalue_line("markers", "real_backend: needs Bassline's real music_manager.service")


@pytest.fixture(autouse=True)
def _clean_backend_env(monkeypatch):
    for k in ("MM_BACKEND", "MM_BACKEND_PATH", "MM_FAKE_SYNTHETIC", "MM_DATA_DIR", "MM_CONFIG_DIR"):
        monkeypatch.delenv(k, raising=False)
    backend_mod.reset_service()
    path_before, bytecode_before = list(sys.path), sys.dont_write_bytecode
    # each test imports its own music_manager (stub checkouts, the configured backend); inside the repository the
    # engine's own tests share this interpreter, so set their modules aside and put them back afterwards
    mods_before = {m: sys.modules.pop(m) for m in list(sys.modules)
                   if m == "music_manager" or m.startswith("music_manager.")}
    yield
    svc_mod = sys.modules.get("music_manager.service")
    if svc_mod is not None and hasattr(svc_mod, "reset"):
        svc_mod.reset()                               # stop the real service's worker thread, close its index
    backend_mod.reset_service()
    sys.path[:] = path_before
    sys.dont_write_bytecode = bytecode_before
    for name in [m for m in sys.modules if m == "music_manager" or m.startswith("music_manager.")]:
        del sys.modules[name]
    sys.modules.update(mods_before)


@pytest.fixture
def fake(qapp):
    """Fake backend with a slow 'Discogs' timer (so it doesn't interfere) and 50 synthetic rows."""
    b = backend_mod.fake_backend(fetch_delay_ms=10**6, synthetic=50)
    yield b
    b._c.fake._timer.stop()


@pytest.fixture(autouse=True)
def _no_modal_message_boxes(monkeypatch):
    """Offscreen tests must never block on a real QMessageBox; record what would have been shown."""
    from PySide6.QtWidgets import QMessageBox
    shown = []
    for name, ret in (("information", QMessageBox.Ok), ("warning", QMessageBox.Ok), ("critical", QMessageBox.Ok)):
        monkeypatch.setattr(QMessageBox, name, staticmethod(lambda *a, _n=name, _r=ret, **k: shown.append((_n, a)) or _r))
    return shown
