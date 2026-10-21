"""Tag batch follow-ups mirrored in the fake (2026-10-21): verify_hashes_available, AlbumPlan.id3v23_files."""

from __future__ import annotations

import dataclasses

import pytest

from mm_ui import fake_service as FS


@pytest.mark.xfail(strict=True, reason="not mirrored yet")
def test_fake_mirrors_the_follow_ups(fake):
    assert [f.name for f in dataclasses.fields(FS.AlbumPlan)][-1] == "id3v23_files"
    assert FS.AlbumPlan(1, 1, None, None, [], []).id3v23_files == 0
    assert fake.settings.verify_hashes_available() == ["sha256"]
