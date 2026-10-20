"""The engine pieces the service builds on: they plan without writing, and match what apply does."""

import shutil

from conftest import SAMPLES, release

from music_manager.apply import apply_album, check_layout, layout_album
from music_manager.fs import collect_albums
from music_manager.journal import read_journal, undo_plan
from music_manager.match import align_tracks, match_tracks
from music_manager.providers.discogs import Discogs, flatten_tracklist, parse_duration, release_artists
from music_manager.tags import preview_tags, write_tags


def make_album(folder, names, ext="flac"):
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        shutil.copyfile(SAMPLES / f"silent.{ext}", folder / name)
    return next(a for a in collect_albums(folder) if a.path == folder)


def snapshot_tree(root):
    return {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*")}


def test_align_tracks_reports_how_each_file_was_paired(tmp_path):
    rel = release("multi_cd")
    dtracks = flatten_tracklist(rel, release_artists(rel))
    album = make_album(tmp_path / "in", ["1-01 First.flac", "1-02 Second.flac", "Third Thing.flac", "zzz.flac"])
    triples, legacy = align_tracks(album, dtracks)
    assert not legacy
    methods = {local.name: method for local, _dt, method in triples}
    assert methods["1-01 First.flac"] == "number"
    assert methods["1-02 Second.flac"] == "number"
    pairs, _ = match_tracks(album, dtracks)
    assert [(local, dt) for local, dt, _ in triples] == pairs
    for _local, dt, method in triples:
        assert (dt is None) == (method == "unmatched")


def test_parse_duration():
    assert parse_duration("3:45") == 225.0
    assert parse_duration("1:02:03") == 3723.0
    assert parse_duration("") is None
    assert parse_duration("abc") is None
    assert parse_duration(None) is None


def test_layout_album_is_read_only_and_matches_apply(tmp_path):
    rel = release("vinyl_lp")
    dtracks = flatten_tracklist(rel, release_artists(rel))
    names = [f"{n:02d} Track.flac" for n in range(1, 11)]
    album = make_album(tmp_path / "in" / "Side Story", names)
    dest = tmp_path / "lib"
    dest.mkdir()
    before = snapshot_tree(tmp_path)
    lines = []
    layout = layout_album(album, dest, rel, dtracks, "1985", log=lines.append)
    assert snapshot_tree(tmp_path) == before
    assert lines[0] == f"  dest  {layout.target_dir}"
    planned = {item.source: item.dest for item in layout.tracks}
    done = dict(apply_album(album, dest, rel, dtracks, "1985", dry_run=False))
    for src, target in planned.items():
        assert done[src] == target
        assert target.exists()


def test_check_layout_lists_problems_without_raising(tmp_path):
    album = make_album(tmp_path / "album", ["01 a.flac"])
    problems = check_layout(album, tmp_path / "album" / "lib", tmp_path / "album" / "lib" / "X")
    assert [kind for kind, _msg, _paths in problems] == ["dest_inside_source"]
    problems = check_layout(album, tmp_path, tmp_path)
    assert [kind for kind, _msg, _paths in problems] == ["source_inside_target"]
    (tmp_path / "album" / "link.flac").symlink_to(tmp_path / "elsewhere.flac")
    problems = check_layout(album, tmp_path / "lib", tmp_path / "lib" / "A")
    assert [kind for kind, _msg, _paths in problems] == ["symlink_outside"]
    assert problems[0][2] == [tmp_path / "album" / "link.flac"]


def test_preview_tags_never_saves_and_agrees_with_write(tmp_path, meta):
    for sample in sorted(SAMPLES.iterdir()):
        path = tmp_path / sample.name
        shutil.copyfile(sample, path)
        data = path.read_bytes()
        changes = preview_tags(path, meta)
        assert path.read_bytes() == data, sample.name
        keys = {key for key, _before, _after in changes}
        assert {"title", "album", "artist"} <= keys, sample.name
        assert ("title", None, "Söng") in changes, sample.name
        write_tags(path, meta)
        assert preview_tags(path, meta) == [], sample.name


def test_undo_steps_carry_structured_fields(tmp_path):
    rel = release("multi_cd")
    dtracks = flatten_tracklist(rel, release_artists(rel))
    album = make_album(tmp_path / "in" / "Double", ["1-01 a.flac", "1-02 b.flac", "2-01 c.flac", "2-02 d.flac"])
    journal_dir = tmp_path / "journals"
    apply_album(album, tmp_path / "lib", rel, dtracks, "1998", dry_run=False, journal_dir=journal_dir)
    (path,) = journal_dir.glob("*.jsonl")
    steps = undo_plan(read_journal(path))
    actions = {s.action for s in steps}
    assert {"rename", "tags", "mkdir"} <= actions
    for step in steps:
        assert step.index >= 0
        if step.action == "rename":
            assert step.current is not None and step.original is not None
            assert step.original.parent == album.path
        if step.action == "tags":
            assert step.snapshot is not None and not step.legacy


def test_discogs_reports_rate_limit_waits(monkeypatch):
    client = Discogs.__new__(Discogs)
    waits = []
    slept = []
    client.on_wait = waits.append
    monkeypatch.setattr("music_manager.providers.discogs.time.sleep", slept.append)
    client._sleep(2.5)
    assert waits == [2.5] and slept == [2.5]
