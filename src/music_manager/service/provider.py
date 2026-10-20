"""service.provider: links to the metadata provider (Discogs)."""

from __future__ import annotations

from ._common import album_record

SUBMIT_URL = "https://www.discogs.com/release/add"


def submit_link(album_id: int) -> str:
    """Where to add a release to Discogs, for an album that has no match there."""
    album_record(album_id)
    return SUBMIT_URL


__all__ = ["submit_link"]
