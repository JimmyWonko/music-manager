"""Naming templates: the presets' golden examples from API-CONTRACT.md, and archivist == today's layout."""

from pathlib import Path

import pytest
from conftest import release

from music_manager.apply import default_file_name, release_info, template_paths
from music_manager.naming import album_folder_name, format_artist
from music_manager.naming.templates import (
    PRESET_IDS,
    PRESETS,
    TemplateError,
    compile_template,
    first_catalog_raw,
    format_field,
)
from music_manager.providers.discogs import DiscogsTrack, flatten_tracklist, release_artists

LIB = Path("/lib")
TEXT = {pid: text for pid, _name, text in PRESETS}


def bf_release(title, year, catno, fmt="CD", qty="1", label="SST Records", tracks=("Track",), positions=None):
    positions = positions or [str(i) for i in range(1, len(tracks) + 1)]
    return {
        "id": 1, "master_id": 2, "title": title, "year": year, "released": str(year),
        "artists": [{"name": "Black Flag"}], "labels": [{"name": label, "catno": catno}],
        "formats": [{"name": fmt, "qty": qty}],
        "tracklist": [{"position": p, "title": t, "type_": "track"} for p, t in zip(positions, tracks)],
    }


NERVOUS = bf_release("Nervous Breakdown", 1988, "SST CD 001", tracks=("Nervous Breakdown", "Fix Me"))
MY_WAR = bf_release("My War", 1984, "SST 023", tracks=("My War", "Can't Decide"))


def render(template_id_or_text, rel, original_year, side_names=None):
    template = None if template_id_or_text is None else compile_template(template_id_or_text)
    dtracks = flatten_tracklist(rel, release_artists(rel))
    target, namer = template_paths(template, LIB, rel, dtracks, original_year, side_names)
    return [str((target / namer(dt, Path("x.FLAC"))).relative_to(LIB)) for dt in dtracks]


def test_presets_in_contract_order():
    assert PRESET_IDS == ("archivist", "simple", "edition-detail", "label-archive")
    assert [name for _id, name, _text in PRESETS] == ["Archivist (default)", "Simple", "Edition detail", "Label archive"]
    assert "\u2044" in TEXT["edition-detail"] and "\u2022" in TEXT["edition-detail"]
    for text in TEXT.values():
        compile_template(text)


def test_contract_examples():
    assert render(TEXT["archivist"], NERVOUS, "1979")[0] == \
        "Black Flag/[1979]  Nervous Breakdown [1988] [SSTCD001]/1. Nervous Breakdown.flac"
    assert render(None, NERVOUS, "1979")[0] == \
        "Black Flag/[1979]  Nervous Breakdown [1988] [SSTCD001]/1. Nervous Breakdown.flac"
    assert render(TEXT["simple"], MY_WAR, "1984")[0] == "Black Flag/My War (1984)/01 - My War.flac"
    assert render(TEXT["edition-detail"], NERVOUS, "1979")[0] == \
        "Black Flag/Nervous Breakdown (1979 \u2044 1988) [SSTCD001 \u2022 CD]/01 Nervous Breakdown.flac"
    assert render(TEXT["label-archive"], MY_WAR, "1984")[0] == "SST Records/[SST023] Black Flag - My War (1984)/1. My War.flac"


def test_empty_fields_drop_with_brackets_and_connectors():
    assert render(TEXT["edition-detail"], MY_WAR, "1984")[0] == \
        "Black Flag/My War (1984) [SST023 \u2022 CD]/01 My War.flac"
    no_cat = bf_release("My War", 1984, "none", tracks=("My War",))
    assert render(TEXT["edition-detail"], no_cat, "")[0] == "Black Flag/My War (1984) [CD]/01 My War.flac"
    assert render(TEXT["archivist"], no_cat, "")[0] == "Black Flag/[1984]  My War/1. My War.flac"
    no_year = dict(no_cat, year="", released="")
    assert render(TEXT["archivist"], no_year, "")[0] == "Black Flag/My War/1. My War.flac"
    assert render(TEXT["simple"], no_year, "")[0] == "Black Flag/My War/01 - My War.flac"


def test_empty_label_folder_becomes_unknown_label():
    rel = bf_release("My War", 1984, "SST 023", label="", tracks=("My War",))
    assert render(TEXT["label-archive"], rel, "1984")[0] == "Unknown label/[SST023] Black Flag - My War (1984)/1. My War.flac"


@pytest.mark.parametrize("name", ["vinyl_lp", "cassette", "index_tracks"])
@pytest.mark.parametrize("side_names", [True, False])
def test_archivist_text_matches_todays_layout(name, side_names):
    rel = release(name)
    for orig in ("", "1970", str(rel.get("year"))):
        assert render(TEXT["archivist"], rel, orig, side_names) == render(None, rel, orig, side_names)
        dtracks = flatten_tracklist(rel, release_artists(rel))
        info = release_info(rel, dtracks, orig)
        legacy_folder = f"{format_artist(info.album_artist)}/" + album_folder_name(
            info.title, orig, info.release_year, info.catno, info.discs)
        assert render(None, rel, orig, side_names)[0].rsplit("/", 1)[0] == legacy_folder


def test_builtin_archivist_keeps_the_disc_count_the_text_cannot_express():
    rel = release("multi_cd")
    builtin = render(None, rel, "1998")
    text = render(TEXT["archivist"], rel, "1998")
    assert "Double Feature 2CD [LT-2CD]/" in builtin[0] and "Double Feature [LT-2CD]/" in text[0]
    assert [p.rsplit("/", 1)[1] for p in builtin] == [p.rsplit("/", 1)[1] for p in text]


def test_default_file_name_is_the_builtin_namer():
    dt = DiscogsTrack(disc=1, number=3, title="A/B: C?", artist="X", position="A3", side="A3")
    assert default_file_name(dt, Path("f.MP3")) == "A3. A_B_ C_.mp3"


def test_functions():
    rel = MY_WAR
    assert render("%albumartist%/$upper(%album%)/$lower(%title%)", rel, "")[0] == "Black Flag/MY WAR/my war.flac"
    assert render("$left(%albumartist%,5)/%album%/$num(%tracknumber%,3)", rel, "")[0] == "Black/My War/001.flac"
    assert render("$if(%reissueyear%,Reissues,Originals)/%album%/%title%", rel, "1979")[0] == "Reissues/My War/My War.flac"
    assert render("$if(%reissueyear%,Reissues,Originals)/%album%/%title%", rel, "")[0] == "Originals/My War/My War.flac"
    assert render("$if2(%reissueyear%,%leadyear%)/%album%/%title%", rel, "")[0] == "1984/My War/My War.flac"
    assert render("%catno_raw%/%format%/%discnumber%-%tracknumber% %title%", rel, "")[0] == "SST 023/CD/1-1 My War.flac"


def test_escapes_and_separator():
    assert render(r"%albumartist%/100\% \$ %album%/%title%", MY_WAR, "")[0] == "Black Flag/100% $ My War/My War.flac"
    # "/" inside a field value never makes a folder
    rel = bf_release("A/B", 1984, "X", tracks=("C/D",))
    assert render(TEXT["simple"], rel, "")[0] == "Black Flag/A_B (1984)/01 - C_D.flac"


@pytest.mark.parametrize("text, field", [
    ("%albumartist%/%nope%/%title%", "nope"),
    ("%albumartist%/$frob(%album%)/%title%", "frob"),
    ("%albumartist%/$num(%album%)/%title%", "num"),
    ("%albumartist%/%album/%title%", "album/"),
    ("%albumartist%/$upper(%album%/%title%", "upper"),
    ("%title%", "text"),
    ("", "text"),
    ("%album%//%title%", "text"),
    ("%album%/Literal", "text"),
])
def test_bad_templates_name_the_field(text, field):
    with pytest.raises(TemplateError) as err:
        compile_template(text)
    assert err.value.field == field


def test_catalog_and_format_helpers():
    assert first_catalog_raw('SST 023; SST-023') == "SST 023"
    assert first_catalog_raw('A/B:C*D?"<>|') == "A_B_C_D_____"
    assert first_catalog_raw("none") == ""
    assert format_field([{"name": "CD", "qty": "2"}]) == "2xCD"
    assert format_field([{"name": "Vinyl", "qty": "1"}]) == "Vinyl"
    assert format_field([]) == ""
