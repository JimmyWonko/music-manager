"""The TypedDicts (review #13) describe what the code really writes."""

import json
import typing

from conftest import release
from test_apply import run_apply
from test_journal_undo import latest_journal, setup_album

from music_manager.journal import JournalEntry
from music_manager.providers.discogs import DiscogsRelease
from music_manager.tags.fields import TrackMeta, tag_values
from music_manager.tags.snapshot import TagSnapshot


def _keys(td) -> set[str]:
    return set(typing.get_type_hints(td))


def test_journal_lines_only_use_journal_entry_keys(tmp_path):
    src = setup_album(tmp_path)
    out = tmp_path / "lib"
    out.mkdir()
    run_apply(src, out, release("vinyl_lp"))
    lines = [json.loads(l) for l in latest_journal().read_text(encoding="utf-8").splitlines()]
    assert lines and all(set(line) <= _keys(JournalEntry) for line in lines)
    snapshots = [line["snapshot"] for line in lines if "snapshot" in line]
    assert snapshots and all(set(s) <= _keys(TagSnapshot) for s in snapshots)


def test_track_meta_keys_match_tag_values():
    meta = {k: "" for k in _keys(TrackMeta)} | {"track": 1, "tracktotal": 1, "disc": 1, "disctotal": 1,
                                                   "release_id": 1, "master_id": 0}
    assert tag_values(meta)  # every key tag_values reads is present


def test_release_fixture_keys_are_described():
    rel = release("vinyl_lp")
    assert set(rel) & _keys(DiscogsRelease) >= {"title", "tracklist"}
