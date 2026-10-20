#!/usr/bin/env python3
"""Run discogs-tag from a checkout. The implementation is music_manager.cli.discogs_tag (src/)."""

import sys
from pathlib import Path

# Resolve the launcher (it is often run through a symlink) so src/ is found next to it.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from music_manager.cli.discogs_tag import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
