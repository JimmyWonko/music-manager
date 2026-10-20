from music_manager.match import ascii_fold, norm, score_result, similar
from music_manager.providers.discogs import Discogs, rank_by_year

"""Bug 6 (Unicode similarity) and bug 9 (year ranking)."""


def test_japanese_and_cyrillic_match():
    assert similar("荒井由実", "荒井由実") == 1.0
    assert similar("ひこうき雲", "ひこうき雲 ") == 1.0
    assert similar("Кино", "КИНО") == 1.0
    assert similar("Группа крови", "Группа Крови!") == 1.0
    assert similar("ひこうき雲", "ミスリム") < 0.5


def test_norm_keeps_letters_of_any_script():
    assert norm("Sigur Rós – Ágætis byrjun") == "sigur rós ágætis byrjun"
    assert norm("ＡＢＣ　１２３") == "abc 123"  # NFKC: full-width -> ASCII
    assert norm("Aesop Rock (2)") == "aesop rock"
    assert norm("मेरा नाम") == "मेरा नाम"  # combining vowel signs kept


def test_accented_latin_also_compared_folded():
    assert similar("Björk", "Bjork") == 1.0
    assert similar("Motörhead", "Motorhead") == 1.0
    assert similar("Straße", "Strasse") == 1.0
    # kana are not folded (would merge か and が)
    assert ascii_fold("が") is None


def test_auto_match_possible_for_japanese():
    guess = {"artist": "荒井由実", "album": "ひこうき雲", "year": "1973", "catno": ""}
    result = {"title": "荒井由実 - ひこうき雲", "year": "1973"}
    assert score_result(result, guess) >= 0.80
    guess["catno"] = "ETP-72071"
    result["catno"] = "ETP-72071"
    assert score_result(result, guess) >= 0.90


def test_year_reorders_but_never_drops():
    results = [{"id": 1, "year": "2001"}, {"id": 2, "year": ""}, {"id": 3, "year": "1999"}, {"id": 4, "year": "1998"}]
    ranked = rank_by_year(results, "1999")
    assert [r["id"] for r in ranked] == [3, 4, 1, 2]
    assert rank_by_year(results, "") == results


def test_search_passes_no_year_filter(monkeypatch):
    seen = []

    class FakeSession:
        headers = {}

    api = Discogs.__new__(Discogs)

    def fake_get(url, params, kind, key):
        seen.append(dict(params or {}))
        return {"results": [{"id": 1, "year": "2000"}, {"id": 2, "year": "1990"}]}

    api._get = fake_get
    out = api.search_releases("A", "B", "", "1990")
    assert [r["id"] for r in out] == [2, 1]
    assert all("year" not in p for p in seen)
