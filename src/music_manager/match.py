"""Matching: guessing artist/album from a folder, scoring Discogs search results, and pairing
local files with a release's tracks.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from .conflicts import DiscChoice, DiscConflict, DiscReason
from .discsources import disc_sources
from .fs import DISC_DIR, AlbumFolder
from .naming import ARTIST_NUM_SUFFIX, YEAR_RE, year4
from .providers.discogs import (
    DiscogsTrack,
    SearchResult,
    is_side_position,
    split_result_title,
)
from .tags.read import read_disc_number, read_easy_tags

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


def match_tracks(album: AlbumFolder, dtracks: list[DiscogsTrack], title_match_min: float = TITLE_MATCH_MIN
                 ) -> tuple[list[tuple[Path, DiscogsTrack | None]], bool]:
    """Pair local files with Discogs tracks (see align_tracks for the rules).

    Returns the pairs and whether the old vinyl numbering was detected.
    """
    triples, use_legacy = align_tracks(album, dtracks, title_match_min)
    return [(local, dt) for local, dt, _method in triples], use_legacy


def _tag_disc_hints(info, dtracks: list[DiscogsTrack], number_of) -> dict[int, int] | None:
    """Disc numbers from the files' tags, when they are usable for the whole album.

    Usable means: every numbered file has a disc tag, no two numbered files claim the
    same (disc, number), and the release has every (disc, number) they claim. Otherwise
    the tags are ignored (None) and the folder / file name hints stand.
    """
    discs = {dt.disc for dt in dtracks}
    release_keys = {(dt.disc, number_of(dt)) for dt in dtracks}
    hints: dict[int, int] = {}
    keys: list[tuple[int, int]] = []
    for k, (local, _disc_hint, number, side_index) in enumerate(info):
        if side_index is not None:
            continue
        disc = read_disc_number(local)
        if number is not None:
            if disc is None:
                return None
            keys.append((disc, number))
        if disc is not None and disc in discs:
            hints[k] = disc
    if not keys or len(set(keys)) != len(keys) or not all(key in release_keys for key in keys):
        return None
    return hints


def _title_picks(group: list[tuple[int, Path]], candidates: list[int], dtracks: list[DiscogsTrack],
                 title_match_min: float) -> tuple[dict[int, int] | None, dict[int, DiscReason]]:
    """Files sharing one track number, each placed on the one same-number track its title matches.

    Returns (picks, reasons): picks is None unless every file's title matches exactly one
    candidate (at title_match_min) and no two files pick the same track; reasons says, for
    every file of a refused group, why (see conflicts.DiscReason).
    """
    good: dict[int, list[int]] = {}
    for k, local in group:
        title = _title_from_stem(local.stem)
        good[k] = [i for i in candidates if title and similar(title, dtracks[i].title) >= title_match_min]
    single = [g[0] for g in good.values() if len(g) == 1]
    reasons: dict[int, DiscReason] = {}
    for k, g in good.items():
        if not g:
            reasons[k] = "no_title_match"
        elif len(g) > 1:
            reasons[k] = "several_title_matches"
        elif single.count(g[0]) > 1:
            reasons[k] = "same_track"
    if reasons:
        return None, {k: reasons.get(k, "held_with_group") for k in good}
    return {k: g[0] for k, g in good.items()}, {}


def _choice(dt: DiscogsTrack, number: int, title: str) -> DiscChoice:
    return DiscChoice(dt.disc, dt.position or f"{dt.disc}-{dt.number}", number, dt.title,
                      similar(title, dt.title) if title else 0.0)


@dataclass(frozen=True)
class TrackCountHold:
    """A disc whose file count is not its track count and whose titles don't settle the pairing.

    Every file of that disc (paths) is left unpaired (method "track_count"): pairing by number
    would shift every title after the missing or merged track. medleys: files whose title is
    several consecutive tracks of the disc joined (a medley ripped as one file), with those
    tracks' positions.
    """
    disc: int
    file_count: int
    track_count: int
    paths: tuple[Path, ...]
    medleys: tuple[tuple[Path, tuple[str, ...]], ...] = ()


def _file_titles(local: Path) -> list[str]:
    """The file's title from its name and from its title tag (either may be missing)."""
    titles = [_title_from_stem(local.stem)]
    try:
        tag = (read_easy_tags(local).get("title") or "").strip()
    except Exception:  # noqa: BLE001 - unreadable: the name is all there is
        tag = ""
    if tag:
        titles.append(tag)
    return [t for t in titles if t]


def _title_score(titles: list[str], title: str) -> float:
    return max((similar(t, title) for t in titles), default=0.0)


def _plain_titles(tracks: list[DiscogsTrack]) -> list[str]:
    """Titles without the "Work: " prefix index sub-tracks share."""
    out = [dt.title for dt in tracks]
    heads = {t.split(": ", 1)[0] for t in out}
    if len(out) > 1 and len(heads) == 1 and all(": " in t for t in out):
        out = [t.split(": ", 1)[1] for t in out]
    return out


def _medley(titles: list[str], run: list[DiscogsTrack], title_match_min: float) -> bool:
    """The file's title is these tracks' titles joined, and fits that better than any one of them."""
    joined = _title_score(titles, " / ".join(_plain_titles(run)))
    single = max(_title_score(titles, dt.title) for dt in run)
    return joined >= max(title_match_min, TITLE_MATCH_MIN) and joined > single


def _count_guard(info, chosen: list[int | None], method: list[str], dtracks: list[DiscogsTrack],
                 title_match_min: float) -> list[TrackCountHold]:
    """Re-check every disc whose file count differs from its track count (see align_tracks_explained)."""
    discs = sorted({dt.disc for dt in dtracks})
    multi = len(discs) > 1
    holds: list[TrackCountHold] = []
    for d in discs:
        tracks_d = [i for i, dt in enumerate(dtracks) if dt.disc == d]
        files_d = [k for k in range(len(info))
                   if (chosen[k] is not None and dtracks[chosen[k]].disc == d)
                   or (chosen[k] is None and (info[k][1] == d or (not multi and info[k][1] is None)))]
        if not files_d or len(files_d) == len(tracks_d):
            continue
        titles = {k: _file_titles(info[k][0]) for k in files_d}
        # A file whose title is several consecutive tracks joined (a medley ripped as one file)
        # holds the disc: pairing it with one of them would drop the others' titles.
        medleys = []
        for k in files_d:
            for size in (2, 3, 4):
                runs = [tracks_d[j:j + size] for j in range(len(tracks_d) - size + 1)]
                hit = next((r for r in runs if _medley(titles[k], [dtracks[i] for i in r], title_match_min)), None)
                if hit:
                    medleys.append((info[k][0], tuple(dtracks[i].position or f"{d}-{dtracks[i].number}"
                                                      for i in hit)))
                    break
        manual = {k for k in files_d if method[k] == "manual"}  # the user's pairs: kept, count as agreeing
        if not medleys:
            paired = [k for k in files_d if chosen[k] is not None and k not in manual]
            if all(_title_score(titles[k], dtracks[chosen[k]].title) >= title_match_min for k in paired):
                continue  # every pair's title agrees: nothing was shifted
            free_d = [i for i in tracks_d if all(chosen[m] != i for m in manual)]
            rest = [k for k in files_d if k not in manual]
            good = {k: [i for i in free_d if _title_score(titles[k], dtracks[i].title) >= title_match_min]
                    for k in rest}
            picks = [g[0] for g in good.values() if len(g) == 1]
            if len(picks) == len(rest) and len(set(picks)) == len(picks):
                for k in rest:
                    chosen[k] = good[k][0]
                    method[k] = "title"
                continue
        for k in files_d:
            if k in manual:
                continue
            chosen[k] = None
            method[k] = "track_count"
        holds.append(TrackCountHold(d, len(files_d), len(tracks_d), tuple(info[k][0] for k in files_d
                                                                          if k not in manual),
                                    tuple(medleys)))
    return holds


def align_tracks(
    album: AlbumFolder, dtracks: list[DiscogsTrack], title_match_min: float = TITLE_MATCH_MIN
) -> tuple[list[tuple[Path, DiscogsTrack | None, str]], bool]:
    """Pair local files with Discogs tracks (see align_tracks_explained). Returns (triples, legacy)."""
    triples, use_legacy, _conflicts = align_tracks_explained(album, dtracks, title_match_min)
    return triples, use_legacy


def align_tracks_explained(
    album: AlbumFolder, dtracks: list[DiscogsTrack], title_match_min: float = TITLE_MATCH_MIN
) -> tuple[list[tuple[Path, DiscogsTrack | None, str]], bool, list[DiscConflict]]:
    """align_tracks_detailed without the track-count holds (their files have method "track_count")."""
    triples, use_legacy, conflicts, _holds = align_tracks_detailed(album, dtracks, title_match_min)
    return triples, use_legacy, conflicts


def align_tracks_detailed(
    album: AlbumFolder, dtracks: list[DiscogsTrack], title_match_min: float = TITLE_MATCH_MIN,
    pins: dict[Path, int] | None = None,
) -> tuple[list[tuple[Path, DiscogsTrack | None, str]], bool, list[DiscConflict], list[TrackCountHold]]:
    """Pair local files with Discogs tracks, saying how each pair was made.

    1. A file named after a side position the release has ("B2. Title")
       gets that track.
    2. A leading number ("06 Title", "6. Title", "2-06 Title") is matched
       against the track numbers. On a multi-disc release the number is
       paired with its disc: the disc number tags come first (when they are
       consistent for the whole album, see _tag_disc_hints), then a CD1/CD2
       folder, then a "2-06" file name. Files renamed by older versions
       (B1 = 21) are recognised and matched by that old number when that
       fits the folder better.
       A file whose CD folder, disc tag and the album's .cue/.m3u don't all
       agree (see discsources) is refused the same way (reason
       "sources_disagree"), whichever two of them agree.
       Files with no disc that share a number ("1. One", "1. Four" in one
       flat folder) are placed only when the folder's numbers are exactly
       the release's per-disc numbering (numbers restart on each disc) and
       each file's title picks one of the same-number tracks. Otherwise they
       are refused: method "ambiguous_disc", no track, never paired by a
       later step (the caller reports it; nothing is guessed).
    3. Anything left is matched by title similarity: a pair needs at least
       title_match_min (TITLE_MATCH_MIN, 0.80, unless the caller says otherwise).
    4. If the folder has exactly as many files as the release has tracks,
       whatever is still left takes the remaining tracks in order. Otherwise
       it stays unmatched (None): reported and not tagged or moved.

    5. A disc whose file count is not its track count (a medley ripped as one
       file, a missing or bonus track) keeps these pairs only when every paired
       file's title (its name or title tag) matches its track. Otherwise the
       disc is paired by title alone when that gives every file of the disc
       exactly one track, no two files the same; else none of its files is
       paired (method "track_count") and the disc is a TrackCountHold. A file
       whose title is several consecutive tracks joined (a medley) always holds
       its disc. Numbers or positions never shift titles onto other files.

    pins: file -> index into dtracks, decided by the user (method "manual"). They are placed
    first, never refused or moved by a later step, and count as agreeing in step 5; the
    caller validates them (see service match.pin).

    A single-disc release is paired by steps 1-4 as before (no disc tags are
    read); step 5 applies to it too.

    Returns (local file, track or None, method) for every local file, where
    method is "side", "number", "title", "position" (steps 1-4),
    "ambiguous_disc", "track_count" or "unmatched", whether the old vinyl
    numbering was detected, a DiscConflict for every "ambiguous_disc" file (in
    file order) and a TrackCountHold for every held disc. Step 5 is skipped
    when any file is "ambiguous_disc" (the album is held for that already).
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
    refused: set[int] = set()
    conflicts: dict[int, DiscConflict] = {}
    pinned: set[int] = set()
    for k, (local, *_rest) in enumerate(info):
        i = (pins or {}).get(local)
        if i is not None and 0 <= i < len(dtracks) and i not in used:
            chosen[k], method[k] = i, "manual"
            used.add(i)
            pinned.add(k)
    if len(discs) > 1:
        # A file whose CD folder, disc tag and cue/m3u don't all agree is never paired.
        index = {local: k for k, (local, *_rest) in enumerate(info)}
        for src in disc_sources(album):
            if src.agree or src.path not in index or index[src.path] in pinned:
                continue
            k = index[src.path]
            local, _disc_hint, number, _side_index = info[k]
            title = _title_from_stem(local.stem)
            refused.add(k)
            method[k] = "ambiguous_disc"
            same = [_choice(dt, number_of(dt), title) for dt in dtracks if number is not None and number_of(dt) == number]
            conflicts[k] = DiscConflict(local, number, title, "sources_disagree", same, [],
                                        src.folder_disc, src.tag_disc, src.sidecar_disc,
                                        src.sidecars[0] if src.sidecars else None)
        tag_hints = _tag_disc_hints(info, dtracks, number_of)
        if tag_hints is not None:
            info = [(local, tag_hints.get(k, disc_hint), number, side_index)
                    for k, (local, disc_hint, number, side_index) in enumerate(info)]
        loose = [k for k, (_l, disc_hint, number, side_index) in enumerate(info)
                 if number is not None and disc_hint is None and side_index is None and k not in refused
                 and k not in pinned]
        counts = Counter(info[k][2] for k in loose)
        repeated = sorted(n for n, c in counts.items() if c > 1)
        if repeated:
            hinted = {d for _l, d, _n, _s in info if d is not None}
            open_tracks = [i for i, dt in enumerate(dtracks) if dt.disc not in hinted]
            restarts = Counter(number_of(dtracks[i]) for i in open_tracks) == counts
            for n in repeated:
                group = [(k, info[k][0]) for k in loose if info[k][2] == n]
                same = [i for i in open_tracks if number_of(dtracks[i]) == n]
                picks = None
                reasons: dict[int, DiscReason] = dict.fromkeys((k for k, _l in group), "numbering")
                if restarts:
                    picks, reasons = _title_picks(group, same, dtracks, title_match_min)
                if picks is None:
                    for k, local in group:
                        refused.add(k)
                        method[k] = "ambiguous_disc"
                        title = _title_from_stem(local.stem)
                        matches = sorted(
                            (_choice(dt, number_of(dt), title) for dt in dtracks
                             if title and similar(title, dt.title) >= title_match_min),
                            key=lambda c: (-c.title_similarity, c.disc, c.number))
                        conflicts[k] = DiscConflict(local, n, title, reasons[k],
                                                    [_choice(dtracks[i], n, title) for i in same], matches)
                    continue
                for k, i in picks.items():
                    chosen[k] = i
                    method[k] = "number"
                    used.add(i)
    for k, (_local, disc_hint, number, side_index) in enumerate(info):
        if chosen[k] is not None or k in refused:
            continue
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
        if chosen[k] is not None or k in refused:
            continue
        local_title = _title_from_stem(local.stem)
        if not local_title:
            continue
        for i, dt in enumerate(dtracks):
            if i in used or (disc_hint is not None and dt.disc != disc_hint):
                continue
            score = similar(local_title, dt.title)
            if score >= title_match_min:
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
            if chosen[k] is not None or k in refused:
                continue
            free = [i for i in range(len(dtracks)) if i not in used]
            if disc_hint is not None:
                free = [i for i in free if dtracks[i].disc == disc_hint] or free
            if free:
                chosen[k] = free[0]
                method[k] = "position"
                used.add(free[0])

    holds = [] if refused else _count_guard(info, chosen, method, dtracks, title_match_min)
    triples = [(info[k][0], dtracks[i] if i is not None else None, method[k]) for k, i in enumerate(chosen)]
    return triples, use_legacy, [conflicts[k] for k in sorted(conflicts)], holds


def ambiguous_disc_files(triples: list[tuple[Path, DiscogsTrack | None, str]]) -> list[Path]:
    """The files align_tracks refused because their disc cannot be told (method "ambiguous_disc")."""
    return [local for local, _dt, method in triples if method == "ambiguous_disc"]
