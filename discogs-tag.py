#!/usr/bin/env python3
"""Run discogs-tag from a checkout. The implementation is discogs_tag.py."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from discogs_tag import main

if __name__ == "__main__":
    raise SystemExit(main())
