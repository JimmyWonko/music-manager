"""Where the UI tests find mm_ui and the real backend; read once, before any fixture clears the env.

Works from both homes of these tests:
- the working copy (/workspace/pixel-ui/tests): mm_ui is ../mm_ui; the real backend defaults to
  Bassline's checkout /workspace/mm-fix/music-manager;
- the repository (music-manager/tests/ui): mm_ui is ../../src/mm_ui; the real backend defaults to the
  repository's own src/music_manager.
MM_BACKEND_PATH (a checkout or its src/) or MM_TEST_BASSLINE (a checkout) override the real backend.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _cand in (HERE.parent, HERE.parent.parent / "src"):
    if (_cand / "mm_ui").is_dir():
        if str(_cand) not in sys.path:
            sys.path.insert(0, str(_cand))
        break

_repo = HERE.parent.parent
_default = _repo if (_repo / "src" / "music_manager").is_dir() else Path("/workspace/mm-fix/music-manager")
_given = os.environ.get("MM_BACKEND_PATH", "").strip()
if _given:
    BACKEND_PATH = Path(_given)
    BASSLINE = BACKEND_PATH.parent if BACKEND_PATH.name == "src" else BACKEND_PATH
else:
    BASSLINE = Path(os.environ.get("MM_TEST_BASSLINE", str(_default)))
    BACKEND_PATH = BASSLINE
# Suite-level backend switch, read once before the per-test fixture clears MM_BACKEND:
#   MM_BACKEND=fake (default): real-backend integration tests are skipped.
#   MM_BACKEND=real: they must run; a missing checkout/ffmpeg/dependency is a failure, not a skip.
SUITE_BACKEND = os.environ.get("MM_BACKEND", "fake").strip().lower()
