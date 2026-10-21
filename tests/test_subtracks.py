"""Subtracks (2026-10-21): Discogs subtracks sharing a base position (2-9a/2-9b, index sub_tracks, 9.1/9.2) that
were ripped as ONE file. Modelled on Discogs release 4054414 (John Prine - The Singing Mailman Delivers, 2CD), whose
2-9a "Hey Good Lookin'" and 2-9b "Jambalaya (On The Bayou)" are one file: 25 tracks against 24 files."""

from __future__ import annotations

import copy

import pytest
from service_fixtures import Library, fixture_release, make_album, search_result

from music_manager import service
from music_manager.fs import AlbumFolder
from music_manager.match import collapse_subtracks
from music_manager.providers.discogs import flatten_tracklist, subtrack_groups

PRINE = 4054414
TIMED = 4054415  # the same tracklist with durations that fit the one file
SLOW = 4054416  # durations that do not fit (2:00 + 2:30 against a 0.05 s sample)
HEY, JAMBA = "Hey Good Lookin'", "Jambalaya (On The Bayou)"
MERGED = f"{HEY} / {JAMBA}"


def prine() -> dict:
    return fixture_release("prine_subtracks")


def timed(rel: dict, sub: tuple[str, str] = ("0:00", "0:00"), other: str = "0:00") -> dict:
    rel = copy.deepcopy(rel)
    for t in rel["tracklist"]:
        if (t.get("type_") or "track") == "track":
            t["duration"] = {"2-9a": sub[0], "2-9b": sub[1]}.get(t["position"], other)
    return rel


def file_names(rel: dict, merge: bool = True) -> list[str]:
    """One name per file of a rip: 2-9a and 2-9b share '2-09'."""
    out = []
    for t in rel["tracklist"]:
        if (t.get("type_") or "track") != "track":
            continue
        disc, num = t["position"].split("-")
        if num == "9b" and merge:
            continue
        title = MERGED.replace("/", "-") if num == "9a" and merge else t["title"]
        out.append(f"{disc}-{int(num.rstrip('ab')):02d} {title}")
    return out


# --- the engine --------------------------------------------------------------------------------------------------

def test_fixture_is_the_trimmed_real_release():
    rel = prine()
    positions = [t["position"] for t in rel["tracklist"] if t.get("type_") == "track"]
    assert len(positions) == 25 and positions[19:21] == ["2-9a", "2-9b"]
    assert all(not t["duration"] for t in rel["tracklist"])  # Discogs lists no durations for 4054414


def test_subtrack_groups_finds_letter_suffixes():
    [g] = subtrack_groups(prine())
    assert (g.disc, g.base, g.positions, g.titles) == (2, "2-9", ("2-9a", "2-9b"), (HEY, JAMBA))
    assert g.durations == (None, None)


def test_subtrack_groups_finds_index_sub_tracks_and_dotted_positions():
    rel = {"tracklist": [
        {"position": "1", "type_": "track", "title": "One", "duration": "3:00"},
        {"position": "", "type_": "index", "title": "Suite", "duration": "", "sub_tracks": [
            {"position": "2a", "type_": "track", "title": "Part A", "duration": "1:00"},
            {"position": "2b", "type_": "track", "title": "Part B", "duration": "1:30"}]},
        {"position": "3.1", "type_": "track", "title": "Left", "duration": ""},
        {"position": "3.2", "type_": "track", "title": "Right", "duration": ""},
        {"position": "4", "type_": "track", "title": "Four", "duration": ""},
    ]}
    groups = subtrack_groups(rel)
    assert [(g.base, g.positions) for g in groups] == [("2", ("2a", "2b")), ("3", ("3.1", "3.2"))]
    assert groups[0].titles == ("Suite: Part A", "Suite: Part B") and groups[0].durations == (60, 90)
    merged = flatten_tracklist(rel, "X", merge=[g.positions for g in groups])
    assert [(t.number, t.title, t.subtracks) for t in merged] == [
        (1, "One", ()), (2, "Suite: Part A / Part B", ("2a", "2b")), (3, "Left / Right", ("3.1", "3.2")),
        (4, "Four", ())]
    assert merged[1].duration_s == 150 and merged[2].duration_s is None and merged[1].position == "2"


def test_vinyl_side_letters_are_not_subtracks():
    assert subtrack_groups(fixture_release("vinyl_lp")) == []


def test_flatten_merge_keeps_discogs_numbers():
    rel = prine()
    plain = flatten_tracklist(rel, "John Prine")
    assert len(plain) == 25
    merged = flatten_tracklist(rel, "John Prine", merge=[("2-9a", "2-9b")])
    disc2 = [t for t in merged if t.disc == 2]
    assert len(merged) == 24 and [t.number for t in disc2] == list(range(1, 14))
    nine = disc2[8]
    assert (nine.position, nine.title, nine.subtracks) == ("2-9", MERGED, ("2-9a", "2-9b"))
    assert disc2[9].title == "Quiet Man" and disc2[9].number == 10


def folder_of(folder):
    return AlbumFolder(folder, sorted(folder.glob("*.flac")))


def _album(tmp_path, rel, merge=True):
    folder = make_album(tmp_path / "John Prine - The Singing Mailman Delivers", file_names(rel, merge))
    return folder


@pytest.mark.parametrize(("rel", "reason", "merged"), [
    (timed(prine()), "durations_fit", True),
    (prine(), "durations_unknown", False),
    (timed(prine(), ("2:00", "2:30")), "durations_differ", False),
])
def test_collapse_only_when_counts_and_durations_fit(tmp_path, rel, reason, merged):
    album = folder_of(_album(tmp_path, rel))
    dtracks, decisions = collapse_subtracks(rel, "John Prine", album, 0.6)
    [d] = decisions
    assert (d.reason, d.merged, d.positions) == (reason, merged, ("2-9a", "2-9b"))
    assert len(dtracks) == (24 if merged else 25)


def test_no_collapse_when_counts_already_match(tmp_path):
    rel = timed(prine())
    album = folder_of(_album(tmp_path, rel, merge=False))
    dtracks, [d] = collapse_subtracks(rel, "John Prine", album, 0.6)
    assert (d.reason, d.merged, len(dtracks)) == ("not_needed", False, 25)


def test_ambiguous_when_merging_every_group_overshoots(tmp_path):
    rel = timed(prine())
    rel["tracklist"] = [dict(t, position={"2-12": "2-12a", "2-13": "2-12b"}.get(t["position"], t["position"]))
                        for t in rel["tracklist"]]
    names = [n for n in file_names(rel) if not n.startswith("2-12")] + ["2-12 Twelve", "2-13 Thirteen"]
    album = folder_of(make_album(tmp_path / "John Prine - The Singing Mailman Delivers", names))
    dtracks, decisions = collapse_subtracks(rel, "John Prine", album, 0.6)
    assert {d.reason for d in decisions} == {"ambiguous"} and len(dtracks) == 25


# --- the service -------------------------------------------------------------------------------------------------

@pytest.fixture
def lib(tmp_path):
    library = Library(tmp_path)
    rel = prine()
    fast = timed(rel)
    fast["id"] = TIMED
    slow = timed(rel, ("2:00", "2:30"))
    slow["id"] = SLOW
    for r in (rel, fast, slow):
        library.discogs.releases[r["id"]] = r
    library.discogs.searches["the singing mailman delivers"] = [search_result(fast), search_result(rel),
                                                                search_result(slow)]
    library.prine = _album(library.root / "incoming", rel)
    library.start(tmp_path)
    library.prine_id = library.album_id(library.prine)
    library.find(library.prine_id)
    yield library
    service.reset()


def _file(lib, prefix):
    return next(p for p in sorted(lib.prine.iterdir()) if p.name.startswith(prefix))


def test_timed_release_collapses_into_plan_view_and_score(lib):
    aid = lib.prine_id
    cands = {c.release_id: c for c in service.match.candidates(aid)}
    assert cands[TIMED].score > cands[PRINE].score and cands[TIMED].score > cands[SLOW].score
    sub = next(r for r in cands[TIMED].reasons if r.field == "subtracks")
    assert sub.ok and "2-9a" in sub.text and "2-9b" in sub.text
    assert not next(r for r in cands[PRINE].reasons if r.field == "subtracks").ok

    view = service.match.view(aid, TIMED)
    assert view.problems == [] and view.unmatched_files == [] and view.unmatched_tracks == []
    [group] = view.subtrack_groups
    assert (group.disc, group.position, group.positions, group.titles, group.merged, group.reason) == (
        2, "2-9", ["2-9a", "2-9b"], [HEY, JAMBA], True, "durations_fit")
    assert group.path == _file(lib, "2-09")
    [pair] = [p for p in view.pairs if p.remote and p.remote.subtracks]
    assert pair.local.path == _file(lib, "2-09") and pair.remote.subtracks == ["2-9a", "2-9b"]
    assert (pair.remote.number, pair.remote.title) == (9, MERGED)

    service.match.choose(aid, TIMED)
    [ap] = service.plan.build([aid]).albums
    assert ap.problems == []
    nine = next(c for c in ap.files if c.path_before == _file(lib, "2-09"))
    assert nine.merged_subtracks == ["2-9a", "2-9b"]
    tags = {t.key: t.after for t in nine.tags}
    assert tags["title"] == MERGED and str(tags["tracknumber"]).split("/")[0].lstrip("0") == "9"
    ten = next(c for c in ap.files if c.path_before == _file(lib, "2-10"))
    ten_tags = {t.key: t.after for t in ten.tags}
    assert ten.merged_subtracks == [] and str(ten_tags["tracknumber"]).split("/")[0] == "10"
    assert ten_tags["title"] == "Quiet Man"


@pytest.mark.parametrize(("rid", "reason"), [(PRINE, "durations_unknown"), (SLOW, "durations_differ")])
def test_untimed_or_misfit_release_keeps_the_hold(lib, rid, reason):
    view = service.match.view(lib.prine_id, rid)
    hold = [p for p in view.problems if p.kind == "disc_track_count"]
    assert hold and "subtrack" in hold[0].message.casefold()
    [group] = view.subtrack_groups
    assert (group.merged, group.reason, group.path) == (False, reason, None)
    service.match.choose(lib.prine_id, rid)
    assert "disc_track_count" in [p.kind for p in service.plan.build([lib.prine_id]).albums[0].problems]


def test_a_group_pin_maps_one_file_to_the_subtracks(lib):
    aid, f = lib.prine_id, _file(lib, "2-09")
    pin = service.match.pin(aid, f, positions=["2-9a", "2-9b"], release_id=PRINE)
    assert (pin.position, pin.positions, pin.release_id) == ("2-9a+2-9b", ["2-9a", "2-9b"], PRINE)
    assert service.match.pins(aid) == [pin]
    view = service.match.view(aid, PRINE)
    assert view.problems == [] and view.unmatched_tracks == [] and view.unmatched_files == []
    [group] = view.subtrack_groups
    assert (group.merged, group.reason, group.path) == (True, "pinned", f)
    [manual] = [p for p in view.pairs if p.method == "manual"]
    assert manual.local.path == f and manual.remote.subtracks == ["2-9a", "2-9b"]
    service.match.choose(aid, PRINE)
    [ap] = service.plan.build([aid]).albums
    assert ap.problems == []
    nine = next(c for c in ap.files if c.path_before == f)
    assert nine.pinned and nine.merged_subtracks == ["2-9a", "2-9b"]
    assert {t.key: t.after for t in nine.tags}["title"] == MERGED


def test_a_single_position_pin_still_works_with_subtracks(lib):
    service.match.choose(lib.prine_id, TIMED)
    f = _file(lib, "2-10")
    service.match.pin(lib.prine_id, f, "2-10")
    assert service.match.view(lib.prine_id).problems == []


@pytest.mark.parametrize("positions", [["2-9a", "2-10"], ["2-9a", "2-9a"], ["2-9b", "2-9a"], ["2-9a", "9z"]])
def test_a_group_pin_that_is_not_one_subtrack_group_is_a_pin_conflict(lib, positions):
    aid = lib.prine_id
    service.match.pin(aid, _file(lib, "2-09"), positions=positions, release_id=PRINE)
    view = service.match.view(aid, PRINE)
    causes = [c for p in view.problems for c in p.pin_conflicts]
    assert [c.reason for c in causes] == ["not_subtrack_group"] and causes[0].position == "+".join(positions)
    assert [g.merged for g in view.subtrack_groups] == [False]


def test_pin_argument_validation(lib):
    aid, f = lib.prine_id, _file(lib, "2-09")
    with pytest.raises(service.ValidationError):
        service.match.pin(aid, f)  # neither
    with pytest.raises(service.ValidationError):
        service.match.pin(aid, f, "2-9a", positions=["2-9a", "2-9b"])  # both
    with pytest.raises(service.ValidationError):
        service.match.pin(aid, f, positions=["2-9a"])  # a group is two or more
    single = service.match.pin(aid, f, "2-9a", release_id=PRINE)
    assert single.positions == ["2-9a"]


def test_appended_fields_keep_their_order():
    import dataclasses

    names = lambda cls: [f.name for f in dataclasses.fields(cls)]
    assert names(service.FileChange)[-1] == "merged_subtracks"
    assert names(service.RemoteTrack)[-1] == "subtracks"
    assert names(service.Pin)[-1] == "positions"
    assert names(service.MatchView)[-1] == "subtrack_groups"
    assert names(service.SubtrackGroup) == ["disc", "position", "positions", "titles", "merged", "reason", "path",
                                            "duration_s"]
