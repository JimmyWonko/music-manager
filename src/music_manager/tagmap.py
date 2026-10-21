"""Discogs release -> the Picard-style album tags (2026-10-21).

Picard (MusicBrainz) has a tag for each of these; Discogs describes them differently. Each rule below either
maps a Discogs value unambiguously or leaves the tag unwritten (None / empty) and adds a note that plan.build
shows as a warning. Nothing is guessed:

- barcode: the release's "Barcode" identifiers, digits only; written only when they all give the same digits.
- releasecountry: Discogs' country name mapped to Picard's ISO 3166 code (UK -> GB, Europe -> XE,
  Worldwide -> XW, ...), only for names in COUNTRY_CODES; regions such as "UK & Europe" are not written.
- media: the formats' names mapped to MusicBrainz medium names (CD, CD-R, '12" Vinyl', Cassette, Digital
  Media, ...); written only when every format maps to the same medium.
- releasetype: one primary type from the format descriptions (Album or LP -> album, EP -> ep, Single or
  Maxi-Single -> single) plus "compilation" when described so; none or a conflicting primary: not written.
- releasestatus: Promo -> promotion, Unofficial Release -> bootleg; otherwise not written (Discogs' own
  "status" is its database state, not the release status).
- labels / catalognumbers: every label entry in Discogs order, as parallel multi-values (duplicates
  dropped); a "none" catalog number is left out and noted.
- genres / styles: Discogs genres -> genre, Discogs styles -> style (a custom STYLE tag); never mixed.
- discsubtitles: on a multi-disc release, the one heading directly before each disc's first track, only when
  every disc has exactly one such heading and no other headings.
- artistsort / albumartistsort: Discogs has no sort names (artists_sort is the display name): never written.
- artist_ids: the release artists' Discogs ids (DISCOGS_ARTIST_ID).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .providers.discogs import DiscogsRelease, DiscogsTrack, parse_position

COUNTRY_CODES = {
    "US": "US", "USA": "US", "UK": "GB", "Germany": "DE", "France": "FR", "Japan": "JP", "Canada": "CA",
    "Netherlands": "NL", "Italy": "IT", "Spain": "ES", "Sweden": "SE", "Norway": "NO", "Denmark": "DK",
    "Finland": "FI", "Belgium": "BE", "Austria": "AT", "Switzerland": "CH", "Australia": "AU",
    "New Zealand": "NZ", "Ireland": "IE", "Portugal": "PT", "Greece": "GR", "Poland": "PL", "Russia": "RU",
    "Brazil": "BR", "Argentina": "AR", "Mexico": "MX", "South Korea": "KR", "Taiwan": "TW", "Hong Kong": "HK",
    "China": "CN", "India": "IN", "South Africa": "ZA", "Czech Republic": "CZ", "Hungary": "HU",
    "Israel": "IL", "Turkey": "TR", "Iceland": "IS", "Jamaica": "JM", "Europe": "XE", "Worldwide": "XW",
}

MEDIA_NAMES = {"CD": "CD", "CDr": "CD-R", "SACD": "SACD", "Cassette": "Cassette", "File": "Digital Media",
               "DVD": "DVD", "Blu-ray": "Blu-ray", "Minidisc": "MiniDisc", "DAT": "DAT", "Shellac": "Shellac"}
VINYL_SIZES = ('12"', '10"', '7"')
PRIMARY_TYPES = {"Album": "album", "LP": "album", "EP": "ep", "Single": "single", "Maxi-Single": "single"}


@dataclass
class ReleaseTags:
    barcode: str | None = None
    media: str | None = None
    releasecountry: str | None = None
    releasetype: list[str] | None = None
    releasestatus: str | None = None
    labels: list[str] = field(default_factory=list)
    catalognumbers: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    styles: list[str] = field(default_factory=list)
    artist_ids: list[str] = field(default_factory=list)
    discsubtitles: dict[int, str] = field(default_factory=dict)
    artistsort: str | None = None  # Discogs has none: always None
    albumartistsort: str | None = None
    notes: list[str] = field(default_factory=list)  # tags left unwritten (or partly written), and why


def _unique(values) -> list[str]:
    out: list[str] = []
    for v in values:
        v = str(v or "").strip()
        if v and v not in out:
            out.append(v)
    return out


def _media(formats, notes) -> str | None:
    names = set()
    for fmt in formats:
        name = (fmt.get("name") or "").strip()
        if name == "Vinyl":
            sizes = [s for s in VINYL_SIZES if s in (fmt.get("descriptions") or [])]
            names.add(f"{sizes[0]} Vinyl" if len(sizes) == 1 else "Vinyl")
        elif name in MEDIA_NAMES:
            names.add(MEDIA_NAMES[name])
        elif name and name != "All Media":
            names.add(None)  # an unknown format
    if len(names) == 1 and None not in names:
        return names.pop()
    if names:
        notes.append("media not written: the formats don't name one medium "
                     f"({', '.join(sorted(f.get('name') or '?' for f in formats))})")
    return None


def _types(formats, notes) -> tuple[list[str] | None, str | None]:
    descriptions = [d for fmt in formats for d in (fmt.get("descriptions") or [])]
    primary = _unique(PRIMARY_TYPES[d] for d in descriptions if d in PRIMARY_TYPES)
    kind = None
    if len(primary) == 1:
        kind = primary + (["compilation"] if "Compilation" in descriptions else [])
    elif primary or "Compilation" in descriptions:
        notes.append("release type not written: the format descriptions give "
                     + (f"several types ({', '.join(primary)})" if primary else "no album, EP or single"))
    status = None
    if "Unofficial Release" in descriptions:
        status = "bootleg"
    elif "Promo" in descriptions:
        status = "promotion"
    return kind, status


def _disc_subtitles(release: DiscogsRelease) -> dict[int, str]:
    pending: list[str] = []
    seen_discs: list[int] = []
    out: dict[int, str] = {}
    for item in release.get("tracklist") or []:
        kind = (item.get("type_") or "track").lower()
        if kind == "heading":
            pending.append((item.get("title") or "").strip())
            continue
        disc = max(parse_position((item.get("position") or "").strip())[0], 1)
        if seen_discs and disc == seen_discs[-1]:
            if pending:
                return {}  # a heading inside a disc: not a disc subtitle
            continue
        seen_discs.append(disc)
        if len(pending) != 1 or not pending[0]:
            return {}
        out[disc] = pending[0]
        pending = []
    if pending or len(seen_discs) < 2 or len(out) != len(seen_discs):
        return {}
    return out


def release_tag_fields(release: DiscogsRelease, dtracks: list[DiscogsTrack] | None = None) -> ReleaseTags:
    """The album-level Picard tags of a release by the rules in this module's docstring."""
    notes: list[str] = []
    out = ReleaseTags(notes=notes)
    codes = _unique(re.sub(r"\D", "", i.get("value") or "") for i in release.get("identifiers") or []
                    if (i.get("type") or "").casefold() == "barcode")
    if len(codes) == 1:
        out.barcode = codes[0]
    elif codes:
        notes.append(f"barcode not written: Discogs lists {len(codes)} different barcodes ({', '.join(codes)})")
    country = (release.get("country") or "").strip()
    if country in COUNTRY_CODES:
        out.releasecountry = COUNTRY_CODES[country]
    elif country:
        notes.append(f"release country not written: {country!r} is not one country")
    formats = release.get("formats") or []
    out.media = _media(formats, notes)
    out.releasetype, out.releasestatus = _types(formats, notes)
    pairs = []
    for lab in release.get("labels") or []:
        pair = ((lab.get("name") or "").strip(), (lab.get("catno") or "").strip())
        if pair[0] and pair not in pairs:
            pairs.append(pair)
    out.labels = _unique(name for name, _ in pairs)
    out.catalognumbers = _unique(c for _, c in pairs if c.casefold() not in ("", "none", "[none]"))
    if any(c.casefold() in ("", "none", "[none]") for _, c in pairs) and out.catalognumbers:
        notes.append("catalog numbers written for some labels only: Discogs lists no catalog number for "
                     + ", ".join(n for n, c in pairs if c.casefold() in ("", "none", "[none]")))
    out.genres = _unique(release.get("genres") or [])
    out.styles = _unique(release.get("styles") or [])
    out.artist_ids = _unique(a.get("id") for a in release.get("artists") or [] if a.get("id"))
    out.discsubtitles = _disc_subtitles(release)
    return out
