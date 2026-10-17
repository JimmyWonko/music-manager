"""Matching: guessing artist/album from a folder, scoring Discogs search results, and pairing
local files with a release's tracks.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

from .fs import DISC_DIR, AlbumFolder
from .naming import ARTIST_NUM_SUFFIX, YEAR_RE, year4
from .providers.discogs import DiscogsTrack, SearchResult, is_side_position, split_result_title
from .tags.read import read_easy_tags

JUNK_BITS = re.compile(
    r"""(?ix)
    (?:
        \[(?:web|cd|vinyl|flac|mp3|v0|v2|320|16-44\.1|24-96|24-192)[^\]]*\]
        | \{[^}]*\}
        | \((?:web|cd|flac|mp3|v0)[^)]*\)
        | \b(?:web|flac|mp3|v0|eac|log|cue)\b
    )
    """
)
NAMED_ALBUM_DIR = re.compile(
    r"^\[(?P<y1>\d{4}|)\]\s+(?P<title>.+?)(?:\s+\[(?P<y2>\d{4})\])?(?:\s+\[(?P<cat>[^\]]*)\])?\s*$"
)
TRACK_PREFIX = re.compile(r"^(\d{1,3})\s*[\.\-_]\s*")


# "06 Title": a leading number separated only by spaces (used by match_tracks
# after TRACK_PREFIX; matched only against track numbers the release has)
SPACED_TRACK_PREFIX = re.compile(r"^(\d{1,3})\s+(?=\S)")


# "2-05 Title" / "2.05 Title": disc 2, track 5 (only trusted on multi-disc releases)
DISC_TRACK_PREFIX = re.compile(r"^(\d{1,2})[-.](\d{1,3})(?=[\s._-])")


# "B2. Title" / "A1 - Title": vinyl/cassette side + track (only trusted when
# the release really has that position)
SIDE_PREFIX = re.compile(r"^([A-Za-z]{1,2}\d{1,2})(?=[\s._-])")


def strip_junk(text: str) -> str:
    text = JUNK_BITS.sub(" ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def parse_album_dirname(name: str) -> dict[str, str]:
    info: dict[str, str] = {}
    match = NAMED_ALBUM_DIR.match(name)
    if match:
        title = match.group("title") or ""
        title = re.sub(r"\s+\d+CD$", "", title).strip()
        info["album"] = title
        info["originalyear"] = match.group("y1") or ""
        info["year"] = match.group("y2") or match.group("y1") or ""
        cat = (match.group("cat") or "").strip()
        if cat and not YEAR_RE.fullmatch(cat):
            info["catno"] = cat
        elif cat and YEAR_RE.fullmatch(cat) and not match.group("y2"):
            info["year"] = cat
        return info

    cleaned = strip_junk(name)
    years = YEAR_RE.findall(cleaned)
    if years:
        info["year"] = years[0]
        cleaned = YEAR_RE.sub(" ", cleaned, count=1)
        cleaned = re.sub(r"\(\s*\)", "", cleaned)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" -_")

    if " - " in cleaned:
        left, right = cleaned.split(" - ", 1)
        # Artist - 2013 - Title  (year already stripped, may leave extra dash)
        right = right.strip(" -_")
        if len(left.split()) <= 6:
            info["artist"] = left.strip()
            info["album"] = right.strip()
            return info

    info["album"] = cleaned
    return info


def guess_from_album_dir(album_dir: Path, tracks: list[Path]) -> dict[str, str]:
    tags = read_easy_tags(tracks[0]) if tracks else {}
    parsed = parse_album_dirname(album_dir.name)
    parent = album_dir.parent.name

    artist = (
        tags.get("albumartist")
        or tags.get("artist")
        or parsed.get("artist")
        or (parent if not NAMED_ALBUM_DIR.match(parent) else "")
    )
    album = tags.get("album") or parsed.get("album") or album_dir.name
    guess = {
        "artist": ARTIST_NUM_SUFFIX.sub("", artist or "").strip(),
        "album": strip_junk(album),
        "year": year4(tags.get("date")) or parsed.get("year", ""),
        "originalyear": year4(tags.get("originaldate")) or parsed.get("originalyear", ""),
        "catno": parsed.get("catno", ""),
    }
    return guess


def _keep_char(ch: str) -> bool:
    # Letters, marks (needed by Devanagari, Thai, ...) and digits of any script.
    return unicodedata.category(ch)[0] in "LMN"


def norm(text: str) -> str:
    """Unicode-aware comparison form: NFKC, casefold, letters/digits only.

    Japanese, Cyrillic, Greek, ... names keep their letters, so they can
    match. Punctuation and symbols become spaces.
    """
    text = unicodedata.normalize("NFKC", text or "").casefold()
    text = ARTIST_NUM_SUFFIX.sub("", text)
    text = "".join(ch if _keep_char(ch) else " " for ch in text)
    return " ".join(text.split())


def ascii_fold(normed: str) -> str | None:
    """'björk' -> 'bjork'. None unless the result is plain ASCII.

    Only accented Latin text gets an extra folded form; stripping marks from
    kana or Devanagari would merge unrelated words.
    """
    decomposed = unicodedata.normalize("NFKD", normed)
    folded = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    folded = folded.replace("ß", "ss").replace("æ", "ae").replace("ø", "o").replace("œ", "oe").replace("đ", "d").replace("ł", "l")
    return folded if folded.isascii() else None


def _ratio(a: str, b: str) -> float:
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def similar(a: str, b: str) -> float:
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    best = _ratio(na, nb)
    fa, fb = ascii_fold(na), ascii_fold(nb)
    if fa and fb:
        best = max(best, _ratio(fa, fb))
    return best


# --auto only applies a match when the best search hit scores at least
# AUTO_MIN_SCORE and beats the runner-up by at least AUTO_MIN_MARGIN.
# The margin used to be 0.08; 0.10 means two hits that differ only by the
# year bonus (0.07) or a near-identical title can never be auto-picked.
AUTO_MIN_SCORE = 0.90
AUTO_MIN_MARGIN = 0.10


def score_components(result: SearchResult, guess: dict[str, str]) -> dict[str, float]:
    """Weighted parts of score_result, so --auto can show why it picked a hit."""
    r_artist, r_album = split_result_title(result.get("title") or "")
    parts = {
        "album": 0.40 * similar(guess.get("album", ""), r_album),
        "artist": 0.35 * similar(guess.get("artist", ""), r_artist),
        "catno": 0.0,
        "year": 0.0,
    }
    cat = (guess.get("catno") or "").replace(" ", "").upper()
    rcat = (result.get("catno") or "").replace(" ", "").upper()
    if cat and rcat and (cat == rcat or cat in rcat or rcat in cat):
        parts["catno"] = 0.18
    gy, ry = guess.get("year", ""), str(result.get("year") or "")
    if gy and ry and gy == ry:
        parts["year"] = 0.07
    return parts


def format_score_components(parts: dict[str, float]) -> str:
    return "  ".join(f"{k} {v:.2f}" for k, v in parts.items())


def score_result(result: SearchResult, guess: dict[str, str]) -> float:
    return min(sum(score_components(result, guess).values()), 1.0)


# ---------------------------------------------------------------------------
# Matching local files to Discogs tracks
# ---------------------------------------------------------------------------

def _disc_from_folder(local: Path, album_path: Path) -> int | None:
    parent = local.parent
    if parent == album_path or not DISC_DIR.match(parent.name):
        return None
    match = re.search(r"\d+", parent.name)
    return int(match.group(0)) if match else None


# Minimum similar() score for pairing an unnumbered file with a track by title.
TITLE_MATCH_MIN = 0.80


def _title_from_stem(stem: str) -> str:
    """The title part of a file name: track/side/disc prefixes stripped."""
    for pattern in (SIDE_PREFIX, DISC_TRACK_PREFIX, TRACK_PREFIX, SPACED_TRACK_PREFIX):
        m = pattern.match(stem)
        if m:
            stem = stem[m.end():]
            break
    return stem.strip(" .-_")


def match_tracks(album: AlbumFolder, dtracks: list[DiscogsTrack]) -> tuple[list[tuple[Path, DiscogsTrack | None]], bool]:
    """Pair local files with Discogs tracks (see align_tracks for the rules).

    Returns the pairs and whether the old vinyl numbering was detected.
    """
    triples, use_legacy = align_tracks(album, dtracks)
    return [(local, dt) for local, dt, _method in triples], use_legacy


def align_tracks(
    album: AlbumFolder, dtracks: list[DiscogsTrack]
) -> tuple[list[tuple[Path, DiscogsTrack | None, str]], bool]:
    """Pair local files with Discogs tracks, saying how each pair was made.

    1. A file named after a side position the release has ("B2. Title")
       gets that track.
    2. A leading number ("06 Title", "6. Title", "2-06 Title") is matched
       against the track numbers; a CD1/CD2 folder limits it to that disc.
       Files renamed by older versions (B1 = 21) are recognised and matched
       by that old number when that fits the folder better.
    3. Anything left is matched by title similarity (TITLE_MATCH_MIN).
    4. If the folder has exactly as many files as the release has tracks,
       whatever is still left takes the remaining tracks in order. Otherwise
       it stays unmatched (None): reported and not tagged or moved.

    Returns (local file, track or None, method) for every local file, where
    method is "side", "number", "title", "position" (steps 1-4) or
    "unmatched", and whether the old vinyl numbering was detected.
    """
    discs = {dt.disc for dt in dtracks}
    max_disc = max(discs, default=1)
    side_positions = {
        dt.position.casefold(): i for i, dt in enumerate(dtracks) if dt.position and is_side_position(dt.position)
    }

    info: list[tuple[Path, int | None, int | None, int | None]] = []
    for local in album.tracks:
        disc_hint = _disc_from_folder(local, album.path)
        number = side_index = None
        stem = local.stem
        side = SIDE_PREFIX.match(stem)
        disc_track = DISC_TRACK_PREFIX.match(stem)
        plain = TRACK_PREFIX.match(stem) or SPACED_TRACK_PREFIX.match(stem)
        if side and side.group(1).casefold() in side_positions:
            side_index = side_positions[side.group(1).casefold()]
        elif disc_track and len(discs) > 1 and disc_hint is None and 1 <= int(disc_track.group(1)) <= max_disc:
            disc_hint, number = int(disc_track.group(1)), int(disc_track.group(2))
        elif plain:
            number = int(plain.group(1))
        info.append((local, disc_hint, number, side_index))

    use_legacy = False
    if any(dt.legacy_number is not None and dt.legacy_number != dt.number for dt in dtracks):
        numbers = [n for _l, _d, n, _s in info if n is not None]
        new_hits = sum(1 for n in numbers if any(dt.number == n for dt in dtracks))
        old_hits = sum(1 for n in numbers if any(dt.legacy_number == n for dt in dtracks))
        use_legacy = old_hits > new_hits

    def number_of(dt: DiscogsTrack) -> int:
        if use_legacy and dt.legacy_number is not None:
            return dt.legacy_number
        return dt.number

    used: set[int] = set()
    chosen: list[int | None] = [None] * len(info)
    method: list[str] = ["unmatched"] * len(info)
    for k, (_local, disc_hint, number, side_index) in enumerate(info):
        if side_index is not None and side_index not in used:
            chosen[k] = side_index
            method[k] = "side"
            used.add(side_index)
            continue
        if number is None:
            continue
        for i, dt in enumerate(dtracks):
            if i in used or number_of(dt) != number:
                continue
            if disc_hint is not None and dt.disc != disc_hint:
                continue
            chosen[k] = i
            method[k] = "number"
            used.add(i)
            break

    # 3. Leftovers: match by normalized title, best pairs first.
    candidates: list[tuple[float, int, int]] = []
    for k, (local, disc_hint, _number, _side) in enumerate(info):
        if chosen[k] is not None:
            continue
        local_title = _title_from_stem(local.stem)
        if not local_title:
            continue
        for i, dt in enumerate(dtracks):
            if i in used or (disc_hint is not None and dt.disc != disc_hint):
                continue
            score = similar(local_title, dt.title)
            if score >= TITLE_MATCH_MIN:
                candidates.append((score, k, i))
    for _score, k, i in sorted(candidates, key=lambda c: (-c[0], c[1], c[2])):
        if chosen[k] is None and i not in used:
            chosen[k] = i
            method[k] = "title"
            used.add(i)

    # 4. Position fallback, only when the folder has exactly as many files as
    #    the release has tracks (so every leftover file has a leftover track).
    #    Otherwise unmatched files stay None: reported and left untagged.
    if len(info) == len(dtracks):
        for k, (_local, disc_hint, _number, _side) in enumerate(info):
            if chosen[k] is not None:
                continue
            free = [i for i in range(len(dtracks)) if i not in used]
            if disc_hint is not None:
                free = [i for i in free if dtracks[i].disc == disc_hint] or free
            if free:
                chosen[k] = free[0]
                method[k] = "position"
                used.add(free[0])

    triples = [(info[k][0], dtracks[i] if i is not None else None, method[k]) for k, i in enumerate(chosen)]
    return triples, use_legacy
