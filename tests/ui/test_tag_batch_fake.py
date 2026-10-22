"""Tag batch follow-ups mirrored in the fake (2026-10-21): verify_hashes_available, AlbumPlan.id3v23_files."""

from __future__ import annotations

import dataclasses

from mm_ui import fake_service as FS


def test_fake_mirrors_the_follow_ups(fake):
    assert "id3v23_files" in [f.name for f in dataclasses.fields(FS.AlbumPlan)]
    assert FS.AlbumPlan(1, 1, None, None, [], []).id3v23_files == 0
    assert fake.settings.verify_hashes_available() == ["sha256"]


def test_fake_status_counts_has_a_library_wide_id3v23(fake):
    counts = fake.library.status_counts()
    assert set(counts) == set(FS.MatchStatus) and isinstance(counts.id3v23, int)
    assert fake.library.status_counts(FS.AlbumFilter(text="zzz-no-match")).id3v23 == counts.id3v23
