"""Discogs sub-tracks in the Match window (contract "Subtracks (2026-10-21)") on the fake backend, with the Prine
fixture modeled on Discogs 4054414: 2-9a + 2-9b ripped as one file, 24 files for 25 tracks, no Discogs lengths.
The real-backend version is in test_real_backend.py."""
from pathlib import Path

import pytest

from mm_ui import problems as P
from mm_ui.match import MANUAL, MERGE_LABEL, MatchWindow

GROUP_ROW = "2-9a + 2-9b"
JOINED = "Hey Good Lookin' / Jambalaya (On The Bayou)"


def _window(fake, qtbot, aid):
    w = MatchWindow(fake); qtbot.addWidget(w)
    w.told = []
    w.inform = lambda title, text: w.told.append(text)
    w.confirm = lambda title, text: True
    w.select(f"album-{aid}")
    assert w.view is not None and w.view.album_id == aid and w.view.fetched
    return w


def _rows(item):
    for i in range(item.childCount()):
        ch = item.child(i)
        yield ch
        yield from _rows(ch)


def _row(w, label):
    return next((it for it in _rows(w.tracks.invisibleRootItem()) if it.text(0) == label), None)


def _medley(w):
    return next(Path(t.path) for t in list(w.svc.library.album(w.view.album_id).tracks) if "Jambalaya" in t.path.name)


@pytest.fixture
def prine(fake):
    return fake._c.fake.add_prine_subtracks()


@pytest.fixture
def prine_lengths(fake):
    return fake._c.fake.add_prine_subtracks(lengths=True)


def test_held_group_is_one_row_with_lengths_unknown_and_nothing_paired_on_the_disc(fake, qtbot, prine):
    w = _window(fake, qtbot, prine)
    g = _row(w, GROUP_ROW)
    assert g is not None, "the subtrack group is one grouped row"
    assert g.text(1) == JOINED and g.text(2) == "lengths unknown"
    assert g.text(3) == "— no file" and g.text(6) == "not merged"
    assert [g.child(i).text(0) for i in range(g.childCount())] == ["2-9a", "2-9b"]
    assert _row(w, "2-9") is None
    # held: disc 2 is not paired by guessing, and the user is told what to do
    assert len(w.view.unmatched_files) == 13
    assert "Discogs lists no lengths" in w.subtracks_note.text() and "pin it" in w.subtracks_note.text()
    assert any(P.kind_of(p) == P.DISC_TRACK_COUNT for p in w.view.problems)
    assert "2-9a + 2-9b may be one file" in w.problems.text()
    w.tracks.setCurrentItem(g)
    assert not w.split_btn.isEnabled() and not w.unsplit_btn.isEnabled()
    assert fake.match.pins(prine) == []


def test_drop_file_on_group_row_pins_whole_group_and_shows_manual(fake, qtbot, prine):
    w = _window(fake, qtbot, prine)
    path = _medley(w)
    w.drop_right(str(path), _row(w, GROUP_ROW))
    (pin,) = fake.match.pins(prine)
    assert pin.path == path and pin.positions == ["2-9a", "2-9b"]
    g = _row(w, GROUP_ROW)
    assert g.text(3) == path.name and g.text(6) == f"✓ {MANUAL} 9a + 9b" and g.text(1) == JOINED
    assert g.childCount() == 0 and _row(w, "2-9a") is None
    assert not any(P.kind_of(p) == P.DISC_TRACK_COUNT for p in w.view.problems) and not w.view.unmatched_files
    assert not w.told
    pair = next(p for p in w.view.pairs if p.local is not None and Path(p.local.path) == path)
    assert pair.method == "subtracks" and pair.positions == ["2-9a", "2-9b"]
    # Unpin brings the hold back
    w.tracks.setCurrentItem(g)
    assert w.unpin_btn.isEnabled()
    w.unpin_selected()
    assert fake.match.pins(prine) == [] and _row(w, GROUP_ROW).text(6) == "not merged"


def test_group_pin_that_is_not_a_group_is_explained_and_undone(fake, qtbot, prine):
    w = _window(fake, qtbot, prine)
    assert w.pin_group(_medley(w), ["2-8", "2-9a"]) is False
    assert w.told and "2-8+2-9a" in w.told[0]
    assert fake.match.pins(prine) == []


def test_auto_merged_row_has_badge_split_and_undo_split(fake, qtbot, prine_lengths):
    w = _window(fake, qtbot, prine_lengths)
    g = _row(w, GROUP_ROW)
    assert g.text(6) == "merged 9a + 9b" and g.text(1) == JOINED and g.text(2) == "3:15"
    assert g.text(3).startswith("09 Hey Good Lookin'")
    assert not any(P.kind_of(p) == P.DISC_TRACK_COUNT for p in w.view.problems)
    w.tracks.setCurrentItem(g)
    assert w.split_btn.isEnabled() and not w.unsplit_btn.isEnabled()
    w.split_btn.click()                                  # one click
    assert fake.match.subtrack_splits(prine_lengths) == ["2-9"]
    g = _row(w, GROUP_ROW)
    assert g.text(6) == "split" and [g.child(i).text(0) for i in range(g.childCount())] == ["2-9a", "2-9b"]
    assert any(P.kind_of(p) == P.DISC_TRACK_COUNT for p in w.view.problems)
    w.tracks.setCurrentItem(g)
    assert w.unsplit_btn.isEnabled() and not w.split_btn.isEnabled()
    w.unsplit_btn.click()
    assert fake.match.subtrack_splits(prine_lengths) == []
    assert _row(w, GROUP_ROW).text(6) == "merged 9a + 9b"


def test_existing_split_is_shown_when_the_album_opens(fake, qtbot, prine_lengths):
    fake.match.split_subtracks(prine_lengths, "2-9")
    w = _window(fake, qtbot, prine_lengths)
    g = _row(w, GROUP_ROW)
    assert g.text(6) == "split" and "split by you" in w.subtracks_note.text()
    w.tracks.setCurrentItem(g)
    assert w.unsplit_btn.isEnabled()


def test_pin_beats_a_split_and_the_stored_split_can_still_be_undone(fake, qtbot, prine_lengths):
    fake.match.split_subtracks(prine_lengths, "2-9")
    w = _window(fake, qtbot, prine_lengths)
    w.drop_right(str(_medley(w)), _row(w, GROUP_ROW))
    g = _row(w, GROUP_ROW)
    assert g.text(6) == f"✓ {MANUAL} 9a + 9b" and "your pin wins" in w.subtracks_note.text()
    w.tracks.setCurrentItem(g)
    assert w.unsplit_btn.isEnabled() and not w.split_btn.isEnabled()
    w.unsplit_btn.click()
    assert fake.match.subtrack_splits(prine_lengths) == []


def test_merge_setting_toggle_defaults_on_and_turns_auto_merge_off(fake, qtbot, prine_lengths):
    w = _window(fake, qtbot, prine_lengths)
    assert w.merge_box.text() == MERGE_LABEL and w.merge_box.isChecked() and w.merge_box.isEnabled()
    assert fake.settings.get().merge_subtracks is True
    w.merge_box.setChecked(False)
    assert fake.settings.get().merge_subtracks is False
    g = _row(w, GROUP_ROW)
    assert g.text(6) == "not merged" and "automatic merging is off" in w.subtracks_note.text()
    # a group pin still merges with the setting off
    w.drop_right(str(_medley(w)), g)
    assert _row(w, GROUP_ROW).text(6) == f"✓ {MANUAL} 9a + 9b"
    w.merge_box.setChecked(True)
    assert fake.settings.get().merge_subtracks is True


def test_nothing_is_written_before_apply(fake, qtbot, prine_lengths):
    """Pin, split, undo split and the setting add nothing to the journal: only Apply writes."""
    before = fake.journal.list(0, 500).total
    w = _window(fake, qtbot, prine_lengths)
    w.tracks.setCurrentItem(_row(w, GROUP_ROW)); w.split_btn.click()
    w.tracks.setCurrentItem(_row(w, GROUP_ROW)); w.unsplit_btn.click()
    w.drop_right(str(_medley(w)), _row(w, GROUP_ROW))
    w.merge_box.setChecked(False)
    assert fake.journal.list(0, 500).total == before


def test_mock_contract_shapes(fake, prine):
    m, T = fake.match, fake.types
    path = Path(next(t.path for t in fake.library.album(prine).tracks))
    with pytest.raises(T.ValidationError):
        m.pin(prine, path, "2-1", positions=["2-9a", "2-9b"])
    with pytest.raises(T.ValidationError):
        m.pin(prine, path, positions=["2-9a"])
    with pytest.raises(T.ValidationError):
        fake.settings.set(merge_subtracks="no")
    with pytest.raises(T.ValidationError):
        m.split_subtracks(prine, "")
    (g,) = m.view(prine).subtrack_groups
    assert (g.disc, g.position, g.positions, g.merged, g.reason, g.path, g.duration_s) == (
        2, "2-9", ["2-9a", "2-9b"], False, "durations_unknown", None, None)


# ---------------------------------------------------------------- 3d31a39: split validation and events
def test_split_rejects_blank_and_unknown_bases_and_unknown_album_or_release(fake, prine_lengths):
    m, T = fake.match, fake.types
    for blank in ("", "   "):
        with pytest.raises(T.ValidationError) as e:
            m.split_subtracks(prine_lengths, blank)
        assert e.value.field == "base_position"
        with pytest.raises(T.ValidationError):
            m.unsplit_subtracks(prine_lengths, blank)
    with pytest.raises(T.ValidationError) as e:
        m.split_subtracks(prine_lengths, "2-99")
    assert e.value.field == "base_position" and "2-9" in str(e.value)       # lists the valid groups
    with pytest.raises(T.NotFound):
        m.split_subtracks(10**6, "2-9")
    with pytest.raises(T.NotFound):
        m.split_subtracks(prine_lengths, "2-9", release_id=1)
    assert m.subtrack_splits(prine_lengths) == []


def test_split_base_is_trimmed_stored_as_the_release_spells_it_and_unsplit_none_clears_all(fake, prine_lengths):
    m = fake.match
    m.split_subtracks(prine_lengths, "  2-9 ")
    m.split_subtracks(prine_lengths, "2-9")                                  # idempotent
    assert m.subtrack_splits(prine_lengths) == ["2-9"]
    m.unsplit_subtracks(prine_lengths, None)
    assert m.subtrack_splits(prine_lengths) == []


def test_merge_setting_emits_match_view_changed_once_per_affected_album(fake, qtbot, prine, prine_lengths):
    seen = []
    fake.events.match_view_changed.connect(seen.append)
    fake.settings.set(merge_subtracks=True)                                  # unchanged: nothing
    assert seen == []
    fake.settings.set(merge_subtracks=False)
    assert sorted(seen) == sorted([prine, prine_lengths])
    seen.clear()
    fake.settings.set(save_cover_art=False)
    assert seen == []


def test_window_follows_match_view_changed_from_other_callers(fake, qtbot, prine_lengths):
    """The window re-reads the view on the service's event, e.g. a split made elsewhere."""
    w = _window(fake, qtbot, prine_lengths)
    fake.match.split_subtracks(prine_lengths, "2-9")
    assert _row(w, GROUP_ROW).text(6) == "split"
    fake.settings.set(merge_subtracks=False)
    fake.match.unsplit_subtracks(prine_lengths)
    assert _row(w, GROUP_ROW).text(6) == "not merged"
    fake.settings.set(merge_subtracks=True)
    assert _row(w, GROUP_ROW).text(6) == "merged 9a + 9b"
