"""Picard tag names in the plan (contract "Picard tag names (2026-10-21)") on the fake backend: key renames,
'Not written' markers from AlbumPlan.unwritten, and the three opt-in tag settings."""
import pytest
from test_ui_flows import built

from mm_ui import problems as P
from mm_ui.review import ApplyDialog
from mm_ui.tagsettings import LABELS, TagSettingsDialog

KEYS = ("migrate_tag_keys", "write_styles", "styles_in_genre")


@pytest.fixture
def album(fake):
    aid = next(a.id for a in fake.albums(None, limit=50).items if a.status.name == "MATCHED"
               and not built(fake, [a.id]).albums[0].problems)
    fake._c.add_picard_demo(aid)
    return aid


def _rows(dlg):
    def walk(it):
        for i in range(it.childCount()):
            yield it.child(i); yield from walk(it.child(i))
    return list(walk(dlg.tree.invisibleRootItem()))


def _dialog(fake, qtbot, aid):
    d = ApplyDialog(built(fake, [aid])); qtbot.addWidget(d)
    return d


def test_rename_rows_show_old_and_new_key_with_the_value(fake, qtbot, album):
    d = _dialog(fake, qtbot, album)
    renames = [r for r in _rows(d) if r.text(0) == "TOTALTRACKS renamed to TRACKTOTAL"]
    assert renames and all(r.text(1) == "12" and r.text(2) == "TRACKTOTAL" for r in renames)
    assert not any("YEAR" in r.text(0) for r in _rows(d))          # migration off: YEAR is not touched


def test_migration_on_shows_year_removal_in_the_preview(fake, qtbot, album):
    fake.settings.set(migrate_tag_keys=True)
    d = _dialog(fake, qtbot, album)
    year = [r for r in _rows(d) if r.text(0) == "YEAR"]
    assert year and all(r.text(1) == "1984" and r.text(2) == "removed by migration" for r in year)
    t = next(t for f in built(fake, [album]).albums[0].files for t in f.tags if t.key_before == "YEAR")
    assert t.migration is True
    assert not any(r.text(0).startswith("YEAR renamed") for r in _rows(d))


def test_unwritten_markers_per_album_with_labels_and_unknown_reason_detail(fake, qtbot, album):
    d = _dialog(fake, qtbot, album)
    marks = {r.text(0): r.text(1) for r in _rows(d) if r.text(0).startswith("⚠ Not written")}
    assert marks["⚠ Not written: barcode"].startswith("the release's barcodes disagree")
    assert marks["⚠ Not written: releasecountry"].startswith("Discogs gives a region, not a country")
    assert marks["⚠ Not written: media"].startswith("the release mixes media")
    assert marks["⚠ Not written: releasetype"].startswith("the release has no single release type")
    assert marks["⚠ Not written: catalognumber"].startswith("some labels have no catalog number")
    assert marks["⚠ Not written: releasestatus"] == "Discogs lists this release as a test pressing"   # unknown code
    top = d.tree.topLevelItem(0)
    assert sum(top.child(i).text(0).startswith("⚠ Not written") for i in range(top.childCount())) == 6


def test_unknown_country_label_and_text_helpers(fake):
    U = fake.types.UnwrittenField
    assert P.unwritten_text(U("releasecountry", "unknown_country", "Atlantis")) == (
        "Not written: releasecountry — Discogs' country isn't one we can name (Atlantis)")
    assert P.unwritten_text(U("x", "new_code", "")) == "Not written: x — new_code"


def test_tag_settings_default_off_independent_and_saved_only_on_save(fake, qtbot):
    d = TagSettingsDialog(fake); qtbot.addWidget(d)
    assert [d.boxes[k].text() for k in KEYS] == [LABELS[k] for k in KEYS]
    assert LABELS["migrate_tag_keys"] == "Remove tag keys Picard doesn't write (e.g. Vorbis YEAR)"
    assert not any(d.boxes[k].isChecked() for k in KEYS) and all(d.boxes[k].isEnabled() for k in KEYS)
    d.boxes["styles_in_genre"].setChecked(True)          # independent of write_styles (contract)
    assert d.boxes["styles_in_genre"].isEnabled() and not d.boxes["write_styles"].isChecked()
    assert fake.settings.get().styles_in_genre is False   # nothing saved before Save
    d.save()
    st = fake.settings.get()
    assert (st.migrate_tag_keys, st.write_styles, st.styles_in_genre) == (False, False, True)
    d2 = TagSettingsDialog(fake); qtbot.addWidget(d2)
    assert d2.boxes["styles_in_genre"].isChecked()


def test_cancel_keeps_settings_and_nothing_is_journaled(fake, qtbot, album):
    before = fake.journal.list(0, 500).total
    d = TagSettingsDialog(fake); qtbot.addWidget(d)
    d.boxes["migrate_tag_keys"].setChecked(True); d.reject()
    assert fake.settings.get().migrate_tag_keys is False
    _dialog(fake, qtbot, album)
    assert fake.journal.list(0, 500).total == before


def test_held_album_has_no_unwritten_entries(fake, album):
    fake._c.mock_problems[album] = [fake.types.Problem(fake.types.ProblemKind.COLLISION, "x", [])]
    ap = built(fake, [album]).albums[0]
    assert ap.problems and ap.unwritten == []


def test_undo_plan_swaps_key_before_and_after(fake, album):
    plan = built(fake, [album])
    rev = fake._c._reverse(plan, 1)
    t = next(t for f in rev.albums[0].files for t in f.tags if P.is_rename(t))
    assert (t.key_before, t.key_after) == ("TRACKTOTAL", "TOTALTRACKS")


def test_plain_removals_keep_removed_label_and_renames_are_not_migrations(fake, qtbot, album):
    plan = built(fake, [album])
    assert all(not t.migration for f in plan.albums[0].files for t in f.tags if P.is_rename(t))
    from mm_ui.fake_service import TagChange
    assert P.removed_text(TagChange("genre", "Rock", None, "GENRE", None)) == "(removed)"
    assert P.removed_text(TagChange("year", "1984", None, "YEAR", None, migration=True)) == "removed by migration"


def test_unwritten_folder_is_filled_and_shown_in_tooltips(fake, qtbot, album):
    plan = built(fake, [album])
    ap = plan.albums[0]
    assert ap.unwritten and all(u.folder == ap.folder_before for u in ap.unwritten)
    assert all(any(w.startswith(f"{ap.folder_before}: {u.field}: ") for w in plan.warnings) for u in ap.unwritten)
    d = ApplyDialog(plan); qtbot.addWidget(d)
    marks = [r for r in _rows(d) if r.text(0).startswith("⚠ Not written")]
    assert marks and all(str(ap.folder_before) in r.toolTip(0) for r in marks)
