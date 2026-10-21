"""Picard-compatible tag names (2026-10-21): the keys, names and conventions Picard writes per format
(picard/formats/{vorbis,id3,mp4,apev2,asf}.py), the Discogs -> tag rules for ambiguous fields, explicit key
renames in the preview, and the opt-in migration of keys earlier versions wrote."""

from __future__ import annotations

import pytest

from music_manager.tags.write import write_tags


def full_meta(**extra):
    meta = {
        "title": "Song", "artist": "Artist", "albumartist": "Artist", "album": "Album",
        "track": 3, "tracktotal": 12, "disc": 1, "disctotal": 2,
        "date": "1999-04-01", "originaldate": "1981",
        "label": ["Label A", "Label B"], "catalognumber": ["A-1", "B 2"],
        "genre": ["Rock", "Folk, World, & Country"], "style": ["Folk Rock", "Country Rock"], "write_styles": True,
        "barcode": "094012004026", "media": "CD", "releasecountry": "US",
        "releasetype": ["album", "compilation"], "releasestatus": "official", "discsubtitle": "Live",
        "release_id": 4054414, "master_id": 2967655, "artist_id": ["274724"], "position": "1-3",
    }
    meta.update(extra)
    return meta


def raw(path):
    """The container's own keys -> list of text values (case kept)."""
    from mutagen import File

    audio = File(path)
    out = {}
    for key in audio.tags.keys():  # noqa: SIM118 - a Vorbis comment iterates (key, value) pairs
        value = audio.tags[key]
        if hasattr(value, "text"):
            out[key] = [str(v) for v in value.text]
        elif isinstance(value, list):
            vals = []
            for v in value:
                if isinstance(v, tuple):
                    vals.append(v)
                elif isinstance(v, (bytes, bytearray)):
                    vals.append(bytes(v).decode())
                else:
                    vals.append(str(getattr(v, "value", v)))
            out[key] = vals
        else:
            text = str(value)
            out[key] = text.split("\x00") if "\x00" in text else [text]
    return out


def ci(tags: dict, key: str):
    return next((v for k, v in tags.items() if k.casefold() == key.casefold()), None)


# --- per format keys ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("ext", ["flac", "ogg", "opus"])
def test_vorbis_keys_are_picards(sample, ext):
    f = sample(ext)
    write_tags(f, full_meta())
    t = {k.upper(): v for k, v in raw(f).items()}
    assert t["ALBUMARTIST"] == ["Artist"] and t["DATE"] == ["1999-04-01"] and t["ORIGINALDATE"] == ["1981"]
    assert t["TRACKNUMBER"] == ["3"] and t["TRACKTOTAL"] == ["12"] and t["DISCNUMBER"] == ["1"]
    assert t["DISCTOTAL"] == ["2"]
    assert t["LABEL"] == ["Label A", "Label B"] and t["CATALOGNUMBER"] == ["A-1", "B 2"]
    assert t["GENRE"] == ["Rock", "Folk, World, & Country"] and t["STYLE"] == ["Folk Rock", "Country Rock"]
    assert t["BARCODE"] == ["094012004026"] and t["MEDIA"] == ["CD"] and t["RELEASECOUNTRY"] == ["US"]
    assert t["RELEASETYPE"] == ["album", "compilation"] and t["RELEASESTATUS"] == ["official"]
    assert t["DISCSUBTITLE"] == ["Live"]
    assert t["DISCOGS_RELEASE_ID"] == ["4054414"] and t["DISCOGS_MASTER_ID"] == ["2967655"]
    assert t["DISCOGS_ARTIST_ID"] == ["274724"]
    assert "YEAR" not in t  # Picard writes no YEAR; it is not created any more


def test_id3_frames_are_picards(sample):
    f = sample("mp3")
    write_tags(f, full_meta())
    t = raw(f)
    assert t["TPE2"] == ["Artist"] and t["TDRC"] == ["1999-04-01"] and t["TDOR"] == ["1981"]
    assert t["TRCK"] == ["3/12"] and t["TPOS"] == ["1/2"]
    assert t["TPUB"] == ["Label A", "Label B"] and t["TXXX:CATALOGNUMBER"] == ["A-1", "B 2"]
    assert t["TCON"] == ["Rock", "Folk, World, & Country"] and t["TXXX:STYLE"] == ["Folk Rock", "Country Rock"]
    assert t["TMED"] == ["CD"] and t["TSST"] == ["Live"] and t["TXXX:BARCODE"] == ["094012004026"]
    assert t["TXXX:MusicBrainz Album Release Country"] == ["US"]
    assert t["TXXX:MusicBrainz Album Type"] == ["album", "compilation"]
    assert t["TXXX:MusicBrainz Album Status"] == ["official"]
    assert t["TXXX:DISCOGS_RELEASE_ID"] == ["4054414"] and t["TXXX:DISCOGS_ARTIST_ID"] == ["274724"]


def test_mp4_atoms_are_picards(sample):
    f = sample("m4a")
    write_tags(f, full_meta())
    t = raw(f)
    ff = "----:com.apple.iTunes:"
    assert t["aART"] == ["Artist"] and t["\xa9day"] == ["1999-04-01"]
    assert t["trkn"] == [(3, 12)] and t["disk"] == [(1, 2)]
    assert t["\xa9gen"] == ["Rock", "Folk, World, & Country"] and t[ff + "STYLE"] == ["Folk Rock", "Country Rock"]
    assert t[ff + "LABEL"] == ["Label A", "Label B"] and t[ff + "CATALOGNUMBER"] == ["A-1", "B 2"]
    assert t[ff + "BARCODE"] == ["094012004026"] and t[ff + "MEDIA"] == ["CD"] and t[ff + "DISCSUBTITLE"] == ["Live"]
    assert t[ff + "MusicBrainz Album Release Country"] == ["US"]
    assert t[ff + "MusicBrainz Album Type"] == ["album", "compilation"]
    assert t[ff + "MusicBrainz Album Status"] == ["official"]
    assert t[ff + "ORIGINALDATE"] == ["1981"] and t[ff + "DISCOGS_ARTIST_ID"] == ["274724"]


def test_apev2_keys_are_picards(sample):
    f = sample("wv")
    write_tags(f, full_meta())
    t = raw(f)
    assert ci(t, "Album Artist") == ["Artist"] and ci(t, "Year") == ["1999-04-01"]
    assert ci(t, "Track") == ["3/12"] and ci(t, "Disc") == ["1/2"]
    assert ci(t, "Label") == ["Label A", "Label B"] and ci(t, "CatalogNumber") == ["A-1", "B 2"]
    assert ci(t, "Barcode") == ["094012004026"] and ci(t, "Media") == ["CD"] and ci(t, "DiscSubtitle") == ["Live"]
    assert ci(t, "Releasecountry") == ["US"] and ci(t, "MUSICBRAINZ_ALBUMTYPE") == ["album", "compilation"]
    assert ci(t, "MUSICBRAINZ_ALBUMSTATUS") == ["official"] and ci(t, "Style") == ["Folk Rock", "Country Rock"]
    assert ci(t, "Genre") == ["Rock", "Folk, World, & Country"] and ci(t, "DISCOGS_ARTIST_ID") == ["274724"]
    keys = {k.casefold() for k in t}
    assert "Album Artist" in t and "Barcode" in t and "MUSICBRAINZ_ALBUMTYPE" in t  # Picard's spelling
    assert "tracktotal" not in keys and "disctotal" not in keys


def test_asf_attributes_are_picards(sample):
    f = sample("wma")
    write_tags(f, full_meta())
    t = raw(f)
    assert t["WM/AlbumArtist"] == ["Artist"] and t["WM/Year"] == ["1999-04-01"]
    assert t["WM/OriginalReleaseTime"] == ["1981"] and t["WM/PartOfSet"] == ["1/2"]
    assert t["WM/Publisher"] == ["Label A", "Label B"] and t["WM/CatalogNo"] == ["A-1", "B 2"]
    assert t["WM/Genre"] == ["Rock", "Folk, World, & Country"] and t["WM/Barcode"] == ["094012004026"]
    assert t["WM/Media"] == ["CD"] and t["WM/SetSubTitle"] == ["Live"]
    assert t["MusicBrainz/Album Release Country"] == ["US"] and t["MusicBrainz/Album Status"] == ["official"]
    assert t["MusicBrainz/Album Type"] == ["album", "compilation"]
    assert t["STYLE"] == ["Folk Rock", "Country Rock"] and t["DISCOGS_ARTIST_ID"] == ["274724"]


@pytest.mark.parametrize("ext", ["flac", "mp3", "m4a", "wv", "wma"])
def test_sort_names_are_never_invented_and_unmanaged_tags_survive(sample, ext):
    from music_manager.tags.read import read_easy_tags

    f = sample(ext)
    first = full_meta(artistsort="Prine, John", albumartistsort="Prine, John")
    write_tags(f, first)  # given explicitly: written under Picard's keys
    t = raw(f)
    key = {"flac": "ARTISTSORT", "mp3": "TSOP", "m4a": "soar", "wv": "Artistsort", "wma": "WM/ArtistSortOrder"}[ext]
    assert ci(t, key) == ["Prine, John"]
    write_tags(f, full_meta())  # not given (Discogs has no sort names): left alone
    assert ci(raw(f), key) == ["Prine, John"]
    assert read_easy_tags(f) is not None


def test_unmanaged_tags_are_never_deleted(sample):
    from mutagen.flac import FLAC

    f = sample("flac")
    audio = FLAC(f)
    audio["MOOD"] = "calm"
    audio["COMMENT"] = "keep me"
    audio["MUSICBRAINZ_ALBUMID"] = "abc"
    audio.save()
    write_tags(f, full_meta())
    t = {k.upper(): v for k, v in raw(f).items()}
    assert t["MOOD"] == ["calm"] and t["COMMENT"] == ["keep me"] and t["MUSICBRAINZ_ALBUMID"] == ["abc"]


def test_unknown_values_are_not_written_and_existing_ones_are_kept(sample):
    from mutagen.flac import FLAC

    f = sample("flac")
    audio = FLAC(f)
    audio["RELEASECOUNTRY"] = "GB"
    audio["BARCODE"] = "123"
    audio.save()
    write_tags(f, full_meta(releasecountry=None, barcode=None, media=None))
    t = {k.upper(): v for k, v in raw(f).items()}
    assert t["RELEASECOUNTRY"] == ["GB"] and t["BARCODE"] == ["123"] and "MEDIA" not in t


# --- the Discogs -> tag rules --------------------------------------------------------------------------------------

def release(**extra):
    rel = {
        "id": 4054414, "title": "The Singing Mailman Delivers", "year": 2011, "country": "US",
        "artists": [{"name": "John Prine", "id": 274724}], "artists_sort": "John Prine",
        "labels": [{"name": "Oh Boy Records", "catno": "OBR-040"}],
        "formats": [{"name": "CD", "qty": "2", "descriptions": ["Album"]}],
        "genres": ["Folk, World, & Country"], "styles": ["Folk"],
        "identifiers": [{"type": "Barcode", "value": "094012004026"},
                        {"type": "Barcode", "value": "0 94012 00402 6"},
                        {"type": "Matrix / Runout", "value": "357617"}],
        "tracklist": [{"position": "", "type_": "heading", "title": "The Singing Mailman Delivers"},
                      {"position": "1-1", "type_": "track", "title": "One"},
                      {"position": "", "type_": "heading", "title": "Live On WFMT"},
                      {"position": "2-1", "type_": "track", "title": "Two"}],
    }
    rel.update(extra)
    return rel


def fields_of(rel):
    from music_manager.providers.discogs import flatten_tracklist, release_artists
    from music_manager.tagmap import release_tag_fields

    return release_tag_fields(rel, flatten_tracklist(rel, release_artists(rel)))


def test_release_fields_from_the_prine_release():
    f = fields_of(release())
    assert (f.barcode, f.media, f.releasecountry, f.releasetype, f.releasestatus) == (
        "094012004026", "CD", "US", ["album"], None)
    assert (f.labels, f.catalognumbers, f.genres, f.styles, f.artist_ids) == (
        ["Oh Boy Records"], ["OBR-040"], ["Folk, World, & Country"], ["Folk"], ["274724"])
    assert f.discsubtitles == {1: "The Singing Mailman Delivers", 2: "Live On WFMT"}
    assert f.artistsort is None and f.albumartistsort is None  # Discogs has no sort names
    assert f.notes == []


def test_two_different_barcodes_are_not_written_and_flagged():
    f = fields_of(release(identifiers=[{"type": "Barcode", "value": "111"}, {"type": "Barcode", "value": "222"}]))
    assert f.barcode is None and any("barcode" in n.casefold() for n in f.notes)


@pytest.mark.parametrize(("country", "code"), [("US", "US"), ("UK", "GB"), ("Germany", "DE"), ("Europe", "XE"),
                                                ("Worldwide", "XW"), ("Japan", "JP")])
def test_countries_map_to_iso_codes(country, code):
    f = fields_of(release(country=country))
    assert f.releasecountry == code and f.notes == []


@pytest.mark.parametrize("country", ["UK & Europe", "USA & Canada", "Benelux", "Unknown"])
def test_country_regions_are_not_written_and_flagged(country):
    f = fields_of(release(country=country))
    assert f.releasecountry is None and any("country" in n.casefold() for n in f.notes)


@pytest.mark.parametrize(("formats", "media"), [
    ([{"name": "Vinyl", "qty": "1", "descriptions": ["LP", "Album", '12"']}], '12" Vinyl'),
    ([{"name": "Vinyl", "qty": "1", "descriptions": ['7"', "Single"]}], '7" Vinyl'),
    ([{"name": "Cassette", "qty": "1", "descriptions": ["Album"]}], "Cassette"),
    ([{"name": "File", "qty": "1", "descriptions": ["FLAC", "Album"]}], "Digital Media"),
    ([{"name": "CDr", "qty": "1", "descriptions": ["Album"]}], "CD-R"),
    ([{"name": "CD", "qty": "1"}, {"name": "CD", "qty": "1", "descriptions": ["Album"]}], "CD"),
])
def test_media_from_formats(formats, media):
    assert fields_of(release(formats=formats)).media == media


def test_mixed_media_is_not_written_and_flagged():
    f = fields_of(release(formats=[{"name": "CD", "qty": "1"}, {"name": "DVD", "qty": "1"}]))
    assert f.media is None and any("media" in n.casefold() for n in f.notes)


@pytest.mark.parametrize(("descriptions", "kind", "noted"), [
    (["Album"], ["album"], False), (["LP", "Album"], ["album"], False), (["EP"], ["ep"], False),
    (["Single"], ["single"], False), (["Maxi-Single"], ["single"], False),
    (["Album", "Compilation"], ["album", "compilation"], False),
    (["Compilation"], None, True), (["Album", "EP"], None, True), ([], None, False),
])
def test_release_type_rules(descriptions, kind, noted):
    f = fields_of(release(formats=[{"name": "CD", "qty": "1", "descriptions": descriptions}]))
    assert f.releasetype == kind and any("type" in n.casefold() for n in f.notes) == noted


@pytest.mark.parametrize(("descriptions", "status"), [(["Album", "Promo"], "promotion"),
                                                      (["Unofficial Release"], "bootleg"), (["Album"], None)])
def test_release_status_rules(descriptions, status):
    f = fields_of(release(formats=[{"name": "CD", "qty": "1", "descriptions": descriptions}]))
    assert f.releasestatus == status


def test_labels_and_catalog_numbers_are_parallel_multi_values():
    f = fields_of(release(labels=[{"name": "A", "catno": "A-1"}, {"name": "B", "catno": "B-2"},
                                  {"name": "A", "catno": "A-1"}]))
    assert (f.labels, f.catalognumbers, f.notes) == (["A", "B"], ["A-1", "B-2"], [])
    f = fields_of(release(labels=[{"name": "A", "catno": "A-1"}, {"name": "A", "catno": "A-2"}]))
    assert (f.labels, f.catalognumbers) == (["A"], ["A-1", "A-2"])
    f = fields_of(release(labels=[{"name": "A", "catno": "A-1"}, {"name": "B", "catno": "none"}]))
    assert (f.labels, f.catalognumbers) == (["A", "B"], ["A-1"])
    assert any("catalog" in n.casefold() for n in f.notes)


def test_genre_is_discogs_genres_and_style_is_discogs_styles():
    f = fields_of(release(genres=["Rock", "Pop"], styles=["Indie Rock", "Shoegaze", "Dream Pop"]))
    assert f.genres == ["Rock", "Pop"] and f.styles == ["Indie Rock", "Shoegaze", "Dream Pop"]


@pytest.mark.parametrize("tracklist", [
    [{"position": "1-1", "type_": "track", "title": "One"}, {"position": "2-1", "type_": "track", "title": "Two"}],
    [{"position": "", "type_": "heading", "title": "Side A"}, {"position": "1-1", "type_": "track", "title": "One"},
     {"position": "", "type_": "heading", "title": "Bonus"}, {"position": "1-2", "type_": "track", "title": "Two"},
     {"position": "2-1", "type_": "track", "title": "Three"}],
])
def test_disc_subtitles_only_when_every_disc_has_one_leading_heading(tracklist):
    assert fields_of(release(tracklist=tracklist)).discsubtitles == {}


# --- previews: explicit renames and the opt-in migration -----------------------------------------------------------

def test_a_key_rename_is_an_explicit_preview_change(sample):
    from mutagen.flac import FLAC

    from music_manager.tags.write import preview_tag_changes

    f = sample("flac")
    audio = FLAC(f)
    audio["TOTALTRACKS"] = "12"
    audio.save()
    [c] = [c for c in preview_tag_changes(f, full_meta()) if c.key == "tracktotal"]
    assert (c.before, c.after, c.key_before, c.key_after) == ("12", "12", "TOTALTRACKS", "TRACKTOTAL")


def test_year_written_earlier_is_kept_up_to_date_unless_migrating(sample):
    from mutagen.flac import FLAC

    from music_manager.tags.write import preview_tag_changes

    f = sample("flac")
    audio = FLAC(f)
    audio["YEAR"] = "1990"
    audio.save()
    [c] = [c for c in preview_tag_changes(f, full_meta()) if c.key == "year"]
    assert (c.before, c.after) == ("1990", "1999")  # maintained, not removed
    [c] = [c for c in preview_tag_changes(f, full_meta(migrate_legacy_keys=True)) if c.key == "year"]
    assert (c.before, c.after, c.key_before, c.key_after) == ("1990", None, "YEAR", None)
    write_tags(f, full_meta(migrate_legacy_keys=True))
    assert "YEAR" not in {k.upper() for k in raw(f)}


# --- the service ---------------------------------------------------------------------------------------------------

def test_settings_and_types_are_appended(tmp_path):
    import dataclasses

    from service_fixtures import Library

    from music_manager import service

    Library(tmp_path).start(tmp_path)
    try:
        assert service.settings.get().migrate_tag_keys is False
        assert service.settings.set(migrate_tag_keys=True).migrate_tag_keys is True
        with pytest.raises(service.ValidationError):
            service.settings.set(migrate_tag_keys="yes")
        assert [f.name for f in dataclasses.fields(service.TagChange)][-3:] == ["key_before", "key_after", "migration"]
        assert "migrate_tag_keys" in [f.name for f in dataclasses.fields(service.Settings)]
    finally:
        service.reset()


def test_plan_shows_picard_keys_renames_and_flags(tmp_path):
    from mutagen.flac import FLAC
    from service_fixtures import Library

    from music_manager import service

    lib = Library(tmp_path)
    lib.multi["identifiers"] = [{"type": "Barcode", "value": "111"}, {"type": "Barcode", "value": "222"}]
    lib.start(tmp_path)
    try:
        first = min(lib.double.glob("*.flac"))
        audio = FLAC(first)
        audio["TOTALTRACKS"] = "2"
        audio.save()
        service.jobs.wait(service.library.scan().id)
        aid = lib.album_id(lib.double)
        lib.find(aid)
        service.match.choose(aid, lib.multi["id"])
        plan = service.plan.build([aid])
        [ap] = plan.albums
        change = next(c for c in ap.files if c.path_before == first)
        total = next(t for t in change.tags if t.key == "tracktotal")
        assert (total.key_before, total.key_after) == ("TOTALTRACKS", "TRACKTOTAL")
        assert any("barcode" in w.casefold() for w in plan.warnings)
        keys = {t.key for t in change.tags}
        assert {"genre", "style", "media", "releasecountry"} & keys
    finally:
        service.reset()


# --- structured unwritten fields (Pixel, 2026-10-21) -----------------------------------------------------------------


def _unwritten(rel):
    return [(u.field, u.reason) for u in fields_of(rel).unwritten]


@pytest.mark.parametrize(("changes", "expected"), [
    ({"identifiers": [{"type": "Barcode", "value": "111"}, {"type": "Barcode", "value": "222"}]},
     [("barcode", "conflicting_barcodes")]),
    ({"country": "UK & Europe"}, [("releasecountry", "region_not_country")]),
    ({"country": "Benelux"}, [("releasecountry", "region_not_country")]),
    ({"country": "Unknown"}, [("releasecountry", "unknown_country")]),
    ({"formats": [{"name": "CD", "qty": "1"}, {"name": "DVD", "qty": "1"}]},
     [("media", "mixed_media")]),
    ({"formats": [{"name": "CD", "qty": "1", "descriptions": ["Album", "EP"]}]}, [("releasetype", "no_single_type")]),
    ({"formats": [{"name": "CD", "qty": "1", "descriptions": ["Compilation"]}]}, [("releasetype", "no_single_type")]),
    ({"labels": [{"name": "A", "catno": "A-1"}, {"name": "B", "catno": "none"}]},
     [("catalognumber", "missing_catalog_number")]),
    ({}, []),
])
def test_held_back_fields_are_structured(changes, expected):
    assert _unwritten(release(**changes)) == expected


def test_unwritten_field_detail_matches_the_note():
    f = fields_of(release(country="UK & Europe"))
    [u] = f.unwritten
    assert u.detail and u.detail in f.notes[0]


def test_plan_lists_unwritten_fields_and_keeps_warnings(tmp_path):
    import dataclasses

    from service_fixtures import Library

    from music_manager import service

    assert "unwritten" in [f.name for f in dataclasses.fields(service.AlbumPlan)]  # more fields follow it
    assert [f.name for f in dataclasses.fields(service.UnwrittenField)] == ["field", "reason", "detail", "folder"]
    lib = Library(tmp_path)
    lib.multi["identifiers"] = [{"type": "Barcode", "value": "111"}, {"type": "Barcode", "value": "222"}]
    lib.multi["country"] = "UK & Europe"
    lib.start(tmp_path)
    try:
        service.jobs.wait(service.library.scan().id)
        aid = lib.album_id(lib.double)
        lib.find(aid)
        service.match.choose(aid, lib.multi["id"])
        plan = service.plan.build([aid])
        [ap] = plan.albums
        assert {(u.field, u.reason) for u in ap.unwritten} >= {
            ("barcode", "conflicting_barcodes"), ("releasecountry", "region_not_country")}
        assert all(isinstance(u, service.UnwrittenField) and u.detail for u in ap.unwritten)
        assert any("barcode" in w.casefold() for w in plan.warnings)  # warnings kept
    finally:
        service.reset()


# --- styles are opt-in (Jimmy, 2026-10-21) ---------------------------------------------------------------------------


@pytest.mark.parametrize("ext", ["flac", "mp3", "m4a"])
def test_styles_off_writes_genres_only_and_leaves_an_existing_style_alone(sample, ext):
    f = sample(ext)
    write_tags(f, full_meta(style=["Old Style"]))  # write_styles=True: a STYLE from earlier
    meta = full_meta(write_styles=False, release_id=999)  # off, even across a release change
    write_tags(f, meta)
    t = {k.upper().removeprefix("----:COM.APPLE.ITUNES:").removeprefix("TXXX:"): v for k, v in raw(f).items()}
    assert t["STYLE"] == ["Old Style"]
    genre = t.get("GENRE") or t.get("TCON") or t.get("\xa9GEN")
    assert genre == ["Rock", "Folk, World, & Country"]


def test_styles_off_does_not_create_style(sample):
    f = sample("flac")
    write_tags(f, full_meta(write_styles=False))
    assert "STYLE" not in {k.upper() for k in raw(f)}


def test_styles_in_genre_appends_styles_to_genre(sample):
    f = sample("flac")
    write_tags(f, full_meta(styles_in_genre=True, style=["Folk Rock", "Rock"]))
    t = {k.upper(): v for k, v in raw(f).items()}
    assert t["GENRE"] == ["Rock", "Folk, World, & Country", "Folk Rock"]  # duplicates dropped
    assert t["STYLE"] == ["Folk Rock", "Rock"]


def test_style_settings_are_appended_and_reach_the_plan(tmp_path):
    import dataclasses

    from service_fixtures import Library

    from music_manager import service

    lib = Library(tmp_path)
    lib.multi["genres"], lib.multi["styles"] = ["Rock"], ["Southern Rock", "Classic Rock"]
    lib.start(tmp_path)
    try:
        names = [f.name for f in dataclasses.fields(service.Settings)]
        assert names[names.index("write_styles"):][:2] == ["write_styles", "styles_in_genre"]
        s = service.settings.get()
        assert (s.write_styles, s.styles_in_genre) == (False, False)
        with pytest.raises(service.ValidationError):
            service.settings.set(write_styles="yes")
        service.jobs.wait(service.library.scan().id)
        aid = lib.album_id(lib.double)
        lib.find(aid)
        service.match.choose(aid, lib.multi["id"])

        def tags():
            [ap] = service.plan.build([aid]).albums
            return {t.key: t.after for t in ap.files[0].tags}

        off = tags()
        assert "style" not in off and "Southern Rock" not in str(off.get("genre"))
        service.settings.set(write_styles=True, styles_in_genre=True)
        on = tags()
        assert "Southern Rock" in str(on["style"]) and "Southern Rock" in str(on["genre"])
    finally:
        service.reset()


# --- follow-ups: TagChange.migration, UnwrittenField.folder (2026-10-21) ---------------------------------------------


def test_preview_marks_only_migration_changes(sample):
    from mutagen.flac import FLAC

    from music_manager.tags.write import preview_tag_changes

    f = sample("flac")
    audio = FLAC(f)
    audio["YEAR"] = "1990"
    audio["TOTALTRACKS"] = "12"
    audio.save()
    by = {c.key: c for c in preview_tag_changes(f, full_meta(migrate_legacy_keys=True))}
    assert by["year"].migration is True and by["year"].after is None
    assert by["tracktotal"].migration is False  # an alias rename: happens with or without the setting
    assert not any(c.migration for k, c in by.items() if k != "year")
    plain = preview_tag_changes(f, full_meta())
    assert not any(c.migration for c in plain)
    assert next(c for c in plain if c.key == "tracktotal").key_before == "TOTALTRACKS"


def test_service_types_gain_migration_and_folder_last():
    import dataclasses

    from music_manager import service

    assert [f.name for f in dataclasses.fields(service.TagChange)][-1] == "migration"
    assert service.TagChange("k", None, None).migration is False
    assert [f.name for f in dataclasses.fields(service.UnwrittenField)] == ["field", "reason", "detail", "folder"]


def test_plan_fills_migration_and_unwritten_folder(tmp_path):
    from mutagen.flac import FLAC
    from service_fixtures import Library

    from music_manager import service

    lib = Library(tmp_path)
    lib.multi["country"] = "UK & Europe"
    lib.start(tmp_path)
    try:
        first = min(lib.double.glob("*.flac"))
        audio = FLAC(first)
        audio["YEAR"] = "1990"
        audio["TOTALTRACKS"] = "2"
        audio.save()
        service.jobs.wait(service.library.scan().id)
        aid = lib.album_id(lib.double)
        lib.find(aid)
        service.match.choose(aid, lib.multi["id"])
        service.settings.set(migrate_tag_keys=True)
        [ap] = service.plan.build([aid]).albums
        change = next(c for c in ap.files if c.path_before == first)
        by = {t.key: t for t in change.tags}
        assert by["year"].migration is True and by["tracktotal"].migration is False
        [u] = [u for u in ap.unwritten if u.field == "releasecountry"]
        assert u.folder == ap.folder_before and isinstance(u.folder, type(ap.folder_before))
    finally:
        service.reset()
