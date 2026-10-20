"""Naming rules: folder and file names (port of the restored Picard script).

Every naming decision (folder and file names, when the original year is shown) lives here,
so it can be driven by a user-configurable template without touching the tagging or move code.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..providers.discogs import DiscogsTrack


SWAP_PREFIXES = ("The", "Die", "Le")

ARTIST_REPLACEMENTS = (
    ('"', ""),
    (" meets ", " & "),
    (" X ", " & "),
    (" × ", " & "),
    ("; ", " & "),
    (" x ", " & "),
    (" | ", " & "),
    (" • ", " & "),
    (" ⋅ ", " & "),
    (" and ", " & "),
    ("+", "&"),
    (" with ", " & "),
    (" _ ", " & "),
    (" vs ", " & "),
    (" vs. ", " & "),
)

CATNO_STRIP = re.compile(r"""[_⋅/: *?"<>| ]""")
ARTIST_NUM_SUFFIX = re.compile(r"\s*\(\d+\)$")
YEAR_RE = re.compile(r"(?:19|20)\d{2}")
POS_VINYL = re.compile(r"^([A-Z]{1,2})\s*(\d*)([a-z]|\.\d+)?$", re.I)


def swapprefix(name: str, prefixes: tuple[str, ...] = SWAP_PREFIXES) -> str:
    stripped = name.strip()
    for prefix in prefixes:
        token = prefix + " "
        if stripped.startswith(token) or stripped.casefold().startswith(token.casefold()):
            rest = stripped[len(prefix) :].lstrip()
            if rest:
                return f"{rest}, {prefix}"
    return stripped


def format_artist(name: str) -> str:
    name = ARTIST_NUM_SUFFIX.sub("", name or "")
    if name.strip().casefold() == "various":
        name = "Various Artists"
    name = swapprefix(name)
    for old, new in ARTIST_REPLACEMENTS:
        name = name.replace(old, new)
    return name.strip()


def format_album_title(title: str) -> str:
    title = (title or "").replace('"', "").replace(":", "-")
    return title[:100].strip()


def format_catalog(catno: str | None) -> str | None:
    if not catno:
        return None
    first = re.split(r";\s*", catno.strip())[0].strip()
    if first.casefold() in {"", "none", "[none]"}:
        return None
    cleaned = CATNO_STRIP.sub("", first).upper()
    return cleaned or None


def sanitize_filename(name: str) -> str:
    name = name.replace("/", "_").replace("\\", "_")
    for ch in ':*?"<>|':
        name = name.replace(ch, "_")
    return name.strip(" .") or "unknown"


def year4(value: str | int | None) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text[:4] if len(text) >= 4 and text[:4].isdigit() else ""


def reissue_original_year(master_year: str | int | None, release_year: str | int | None) -> str:
    """The original year for the ORIGINALDATE tags, or "" when this release is the original.

    A release is a reissue when its master's year is earlier than its own
    year. Same year, no master, or an unknown year on either side: not a
    reissue, so no original-date tag is written. (The folder name always
    has a leading year; see folder_lead_year.)
    """
    master, release = year4(master_year), year4(release_year)
    if master and release and int(master) < int(release):
        return master
    return ""


def folder_lead_year(master_year: str | int | None, release_year: str | int | None) -> str:
    """Leading year of the album folder, so an artist's albums sort chronologically.

    The master's year when this is a reissue, otherwise the release's own
    year (or the master's year when the release year is unknown).
    """
    return reissue_original_year(master_year, release_year) or year4(release_year) or year4(master_year)


def album_folder_name(album: str, original_year: str, release_year: str, catno: str | None, discs: int) -> str:
    """[LEADYEAR]  Album [2CD] [RELEASEYEAR] [CATNO]

    original_year is the master's year.
    - LEADYEAR: the master's year for a reissue, else the release year
      (see folder_lead_year), so each artist's albums sort chronologically.
    - RELEASEYEAR: only for a reissue (master year earlier than release year).
    - Unknown parts are left out with their brackets and spaces.

    Reissue:      [1979]  Nervous Breakdown [1988] [SSTCD001]
    Not reissue:  [1984]  My War [SST023]
    No year:      My War [SST023]
    """
    lead = folder_lead_year(original_year, release_year)
    title = format_album_title(album)
    parts = [f"[{lead}]  {title}" if lead else title]
    if discs > 1:
        parts.append(f"{discs}CD")
    if reissue_original_year(original_year, release_year):
        parts.append(f"[{year4(release_year)}]")
    catalog = format_catalog(catno)
    if catalog:
        parts.append(f"[{catalog}]")
    return sanitize_filename(" ".join(parts))


def track_filename(number: int | str, title: str, suffix: str) -> str:
    """"N. Title.ext", or "A1. Title.ext" when given a vinyl/cassette side position."""
    return sanitize_filename(f"{number}. {title}") + suffix.lower()


# Vinyl and cassette tracks are named after their side position (A1, A2, B1 ...)
# by default, as collectors expect. --sequential-sides switches back to 1..N.
SIDE_NAMES = True


def side_label(position: str) -> str | None:
    """Normalised side position for a file name ("a1" -> "A1", "B 2" -> "B2"), or None."""
    pos = (position or "").strip()
    match = POS_VINYL.match(pos)
    if not match:
        return None
    return match.group(1).upper() + match.group(2) + (match.group(3) or "")


def track_prefix(dt: "DiscogsTrack", side_names: bool | None = None) -> str:
    """What goes before ". Title" in a track's file name."""
    use_sides = SIDE_NAMES if side_names is None else side_names
    if use_sides and dt.side:
        return dt.side
    return str(dt.number)
