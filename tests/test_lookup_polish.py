"""Lookup polish: barcode guess, search fallbacks, match reasons, barcode tie-break, manual text search."""

from __future__ import annotations

import pytest
from service_fixtures import Library, search_result

from music_manager import service
from music_manager.match import guess_from_album_dir
from music_manager.service import JobState, MatchStatus

BARCODE = "5012345678900"


@pytest.fixture
def lib(tmp_path):
    library = Library(tmp_path).start(tmp_path)
    yield library
    service.reset()


def wait(job):
    return service.jobs.wait(job.id)


def tag_barcode(folder, value, key="BARCODE"):
    from mutagen import File

    for f in sorted(folder.iterdir()):
        if not f.is_file() or f.suffix not in (".flac", ".mp3", ".ogg"):
            continue
        audio = File(f)
        if audio is None:
            continue
        if audio.tags is None:
            audio.add_tags()
        audio.tags[key] = [value]
        audio.save()


def reason(cand, field):
    return next(r for r in cand.reasons if r.field == field)


# --- the guess ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", ["BARCODE", "UPC", "EAN"])
def test_guess_reads_a_barcode_tag_as_digits(sample, tmp_path, key):
    album = tmp_path / "Band - Album"
    f = sample("flac", folder=album)
    tag_barcode(album, "5 012345 678900", key)
    assert guess_from_album_dir(album, [f])["barcode"] == BARCODE


def test_guess_without_barcode_has_no_barcode_key(sample, tmp_path):
    album = tmp_path / "Band - Album"
    f = sample("flac", folder=album)
    assert "barcode" not in guess_from_album_dir(album, [f])


def test_guess_ignores_a_barcode_that_is_not_one(sample, tmp_path):
    album = tmp_path / "Band - Album"
    f = sample("flac", folder=album)
    tag_barcode(album, "n/a")
    assert "barcode" not in guess_from_album_dir(album, [f])


# --- the Discogs client ---------------------------------------------------------------------

def test_client_barcode_and_text_searches_use_cached_get():
    from music_manager.providers.discogs import Discogs

    api = Discogs.__new__(Discogs)
    seen = []

    def fake_get(url, params, kind, key):
        seen.append((url, dict(params), kind))
        return {"results": [{"id": 1, "year": "2000"}, {"id": 2, "year": "1990"}]}

    api._get = fake_get
    assert [r["id"] for r in api.search_barcode(BARCODE)] == [1, 2]
    assert [r["id"] for r in api.search_text("Tape Loops", year="1990")] == [2, 1]
    assert seen[0][1] == {"type": "release", "per_page": "15", "barcode": BARCODE} and seen[0][2] == "search"
    assert seen[1][1] == {"type": "release", "per_page": "15", "q": "Tape Loops"}


# --- find: fallbacks and found_by -----------------------------------------------------------

def test_barcode_search_comes_first(lib):
    tape = lib.album_id(lib.tape)
    tag_barcode(lib.tape, BARCODE)
    wait(service.library.scan())
    lib.discogs.barcode_searches[BARCODE] = [search_result(lib.cassette, barcode=[BARCODE])]
    lib.find(tape)
    assert lib.discogs.calls[0] == ("barcode", BARCODE) or ("barcode", BARCODE) in lib.discogs.calls
    (cand,) = service.match.candidates(tape)
    assert cand.release_id == lib.cassette["id"] and cand.found_by == "barcode"
    assert reason(cand, "barcode").ok and BARCODE in reason(cand, "barcode").text


def test_catno_then_title_only_fallbacks(lib):
    side = lib.album_id(lib.side)
    tape = lib.album_id(lib.tape)
    lib.discogs.searches["side story"] = []
    lib.discogs.text_searches["side story"] = [search_result(lib.vinyl)]
    lib.discogs.text_searches["tape loops"] = [search_result(lib.cassette)]
    lib.find(side, tape)
    assert service.match.candidates(side)[0].found_by == "text"
    assert service.match.candidates(tape)[0].found_by == "text"
    double = lib.album_id(lib.double)
    lib.discogs.searches["double feature"] = []
    lib.discogs.catno_searches["LT-2CD"] = [search_result(lib.multi)]
    lib.find(double)
    assert service.match.candidates(double)[0].found_by == "catno"


def test_artist_album_hits_say_so(lib):
    d = lib.album_id(lib.double)
    lib.find(d)
    cands = service.match.candidates(d)
    assert {c.found_by for c in cands} == {"artist_album"}
    assert not any(c[0] in ("barcode", "text") for c in lib.discogs.calls)  # first search answered


# --- reasons -----------------------------------------------------------------------------------

def test_reasons_explain_the_score(lib):
    d = lib.album_id(lib.double)
    lib.find(d)
    best = service.match.candidates(d)[0]
    assert best.release_id == 1000004
    fields = [r.field for r in best.reasons]
    assert fields[:4] == ["album", "artist", "catno", "year"]
    assert reason(best, "catno").ok and "LT-2CD" in reason(best, "catno").text
    assert reason(best, "year").ok and "1998" in reason(best, "year").text
    assert reason(best, "track_count").ok and "4" in reason(best, "track_count").text
    assert all(isinstance(r.text, str) and r.text for r in best.reasons)
    other = next(c for c in service.match.candidates(d) if c.release_id == 7777)
    assert not reason(other, "catno").ok and not reason(other, "year").ok


def test_barcode_match_breaks_a_score_tie(lib):
    d = lib.album_id(lib.double)
    tag_barcode(lib.double, BARCODE)
    wait(service.library.scan())
    twin = dict(lib.multi, id=1000005)
    lib.discogs.releases[1000005] = twin
    lib.discogs.searches["double feature"] = [search_result(lib.multi), search_result(twin, barcode=[BARCODE])]
    lib.find(d)
    cands = service.match.candidates(d)
    assert cands[0].score == pytest.approx(cands[1].score)
    assert cands[0].release_id == 1000005 and reason(cands[0], "barcode").ok
    assert cands[0].auto_score == pytest.approx(cands[1].auto_score)  # the --auto score is unchanged


# --- manual text search ------------------------------------------------------------------------

def test_manual_search_adds_candidates(lib):
    tape = lib.album_id(lib.tape)
    lib.find(tape)
    assert service.library.album(tape).status is MatchStatus.NO_MATCH
    lib.discogs.text_searches["kino cassette"] = [search_result(lib.cassette)]
    job = wait(service.match.search(tape, "Kino cassette"))
    assert job.state is JobState.DONE, job.message
    (cand,) = service.match.candidates(tape)
    assert cand.found_by == "manual_search" and cand.release_id == lib.cassette["id"]
    assert service.library.album(tape).status is MatchStatus.NEEDS_REVIEW


def test_manual_search_by_barcode_url_and_empty(lib):
    tape = lib.album_id(lib.tape)
    lib.discogs.barcode_searches[BARCODE] = [search_result(lib.cassette)]
    assert wait(service.match.search(tape, "5 012345 678900")).state is JobState.DONE
    assert service.match.candidates(tape)[0].found_by == "manual_search"
    assert ("barcode", BARCODE) in lib.discogs.calls
    job = wait(service.match.search(tape, f"https://www.discogs.com/release/{lib.vinyl['id']}-x"))
    assert job.state is JobState.DONE
    assert lib.vinyl["id"] in {c.release_id for c in service.match.candidates(tape)}
    with pytest.raises(service.ValidationError):
        service.match.search(tape, "   ")


def test_manual_search_with_no_hits_fails_the_job_and_keeps_status(lib):
    tape = lib.album_id(lib.tape)
    before = service.library.album(tape).status
    job = wait(service.match.search(tape, "nothing like this"))
    assert job.state is JobState.FAILED and "no Discogs results" in job.message
    assert service.library.album(tape).status is before
