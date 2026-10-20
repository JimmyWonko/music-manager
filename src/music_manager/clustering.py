# Matrixwell: grouping loose audio files into album clusters by their tags.
#
# Derived from MusicBrainz Picard (GPL-2.0-or-later): picard/cluster.py
# (Cluster.cluster, FileCluster, tokenize). Adapted for Matrixwell: plain
# tuples instead of Picard File objects, an album-artist part in the key,
# disc suffixes ignored, folder name as the album fallback. No MusicBrainz IDs.
#
# Picard, the next-generation MusicBrainz tagger
#
# Copyright (C) 2004 Robert Kaye
# Copyright (C) 2006-2008, 2011 Lukáš Lalinský
# Copyright (C) 2008 Hendrik van Antwerpen
# Copyright (C) 2008 Will
# Copyright (C) 2010-2011, 2014, 2018-2026 Philipp Wolfer
# Copyright (C) 2011-2013 Michael Wiencek
# Copyright (C) 2012 Chad Wilson
# Copyright (C) 2012 Wieland Hoffmann
# Copyright (C) 2013-2015, 2018-2021, 2023-2026 Laurent Monin
# Copyright (C) 2014, 2017 Sophist-UK
# Copyright (C) 2016 Rahul Raturi
# Copyright (C) 2016-2017 Sambhav Kothari
# Copyright (C) 2017 Antonio Larrosa
# Copyright (C) 2018 Vishal Choudhary
# Copyright (C) 2020 Gabriel Ferreira
# Copyright (C) 2020 Ray Bouchard
# Copyright (C) 2021 Petit Minion
# Copyright (C) 2024 Giorgio Fontanive
# Copyright (C) 2025 Bob Swift
# Copyright (C) 2026 James Boucher (Matrixwell adaptation)
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, see <https://www.gnu.org/licenses/>.
"""Group files into album clusters by album-level tags, like Picard's Cluster.cluster. Pure; no I/O."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass

VARIOUS_ARTISTS = "Various Artists"

_re_non_alphanum = re.compile(r"\W", re.UNICODE)
_re_spaces = re.compile(r"\s", re.UNICODE)
# "Album (Disc 2)", "Album [CD 1]", "Album - Disc 3": one album across discs
_re_disc_suffix = re.compile(r"[\s\-–:]*[(\[]?\s*(?:disc|disk|cd)\s*\d+\s*[)\]]?\s*$", re.IGNORECASE)


def tokenize(word: str) -> str:
    """Picard's tokenize: lower case, non-alphanumerics dropped (whitespace dropped if nothing is left)."""
    word = word.lower()
    token = _re_non_alphanum.sub("", word)
    return token if token else _re_spaces.sub("", word)


def album_token(album: str) -> str:
    return tokenize(_re_disc_suffix.sub("", album).strip() or album)


@dataclass(frozen=True)
class Item:
    """One file's album-level tags; key is anything the caller wants back (e.g. the path)."""
    key: object
    album: str
    albumartist: str
    artist: str
    folder_name: str = ""


class FileCluster:
    """Picard's FileCluster: the files of one cluster, with artist and title chosen by vote."""

    def __init__(self):
        self.items: list[Item] = []
        self._artist_counts: Counter = Counter()
        self._artists: defaultdict = defaultdict(Counter)
        self._titles: Counter = Counter()

    def add(self, album: str, artist: str, item: Item) -> None:
        self.items.append(item)
        token = tokenize(artist)
        self._artist_counts[token] += 1
        self._artists[token][artist] += 1
        self._titles[album] += 1

    @property
    def artist(self) -> str:
        tokenized_artist = self._artist_counts.most_common(1)[0][0]
        return self._artists[tokenized_artist].most_common(1)[0][0]

    @property
    def title(self) -> str:
        return self._titles.most_common(1)[0][0]


def cluster(items: list[Item]) -> list[FileCluster]:
    """Group items by album token, plus the album-artist token when the file has an album artist.

    Files without an album artist join the album's cluster whatever their track artist
    (compilations), as in Picard; the cluster's artist is the most common one. A file with
    no album tag uses its folder name. Files with neither are left out. Clusters come back
    in first-seen order.
    """
    groups: dict[tuple[str, str], FileCluster] = {}
    by_album: dict[str, tuple[str, str]] = {}
    pending: list[tuple[str, str, str, Item]] = []
    for item in items:
        album = item.album.strip() or item.folder_name.strip()
        token = album_token(album)
        if not token:
            continue
        if item.albumartist.strip():
            key = (token, tokenize(item.albumartist))
            groups.setdefault(key, FileCluster()).add(album, item.albumartist.strip(), item)
            by_album.setdefault(token, key)
        else:
            pending.append((token, album, item.artist.strip(), item))
    for token, album, artist, item in pending:
        key = by_album.get(token) or (token, "")
        by_album.setdefault(token, key)
        groups.setdefault(key, FileCluster()).add(album, artist or VARIOUS_ARTISTS, item)
    return list(groups.values())
