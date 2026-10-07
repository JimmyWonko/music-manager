# music-manager

Utilities for the local music library. `discogs-tag` looks up a Discogs
release, writes tags, and renames the album into this library's folder
shape. `album-m3u` writes one playlist per album folder. Both use the
small shared helper `mm_common.py` next to them (found through the
symlink, so keep it in this directory).

## Install

Python 3.12 or newer, plus `requests` and `mutagen`:

```sh
python3 -c "import requests, mutagen"
```

`~/.local/bin` is on `PATH`. `discogs-tag` and `discogs-tag.py` both run
this directory's `discogs-tag.py`:

```sh
discogs-tag --help
```

## Discogs token

Create a personal access token (not an OAuth application) at
<https://www.discogs.com/settings/developers>, then:

```sh
discogs-tag --save-token
```

The token is saved at `~/.config/discogs-tag/token` with mode `600`.
`DISCOGS_TOKEN` overrides that file. `DISCOGS_KEY` and `DISCOGS_SECRET`
are the consumer key and secret alternative.

## Tag an album

Preview a folder. Nothing is written:

```sh
discogs-tag --dry-run "/path/to/album"
```

Apply it. On a terminal it asks before writing. `--yes` skips that
question. `--auto` applies only when the top Discogs hit scores at least
90% and leads the next hit by 8 points.

```sh
discogs-tag "/path/to/album"
discogs-tag --auto --yes "/path/to/Featuring"
```

Force one release, or cap a tree walk:

```sh
discogs-tag --release-id 249504 "/path/to/album"
discogs-tag --skip-tagged --limit 10 "/path/to/tree"
```

Search results are cached under `~/.cache/discogs-tag` for 30 days.
`--limit` counts albums that were actually looked up; albums skipped by
`--skip-tagged` do not use up the limit.

Each album is applied in two steps: first every file is tagged in place,
then the files, extra folders and sidecars are moved. If any file cannot
be tagged, nothing is moved. If a move fails partway (disk full, drive
unplugged), the error lists every file that was moved and where, and
every file that was not. One album failing never stops the run; failed
albums are listed at the end.

## Where files go

```text
DESTINATION/AlbumArtist/[original year]  Album [press year] [CATNO]/N. Title.ext
```

`%date%` is the press/release year after the title. The leading
`[original year]` is only used for a reissue: when the Discogs master's
year is earlier than the release's year. An original release (same year,
no master, or no master year) gets no leading year and no original-date
tag. A catalog number is appended when Discogs has one. Examples:

```text
Abrasive Wheels/[1982]  When the Punks Go Marching In! [2006] [AHOYDPX25]/   (2006 reissue of a 1982 album)
Some Artist/Some Album [2001] [CAT001]/                                         (original release)
```

When an album this tool tagged before turns out not to be a reissue, the
original-date tags it wrote earlier are removed. An original date written
by another tagger is left alone.

`DESTINATION` is the first match:

1. `--dest`
2. The `Featuring` directory, when the path is under one
3. `$DEFAULT_MUSIC` (default `/run/media/xen_is/Tunes/Music` when that
   volume is mounted, otherwise `~/Music`)

Albums are filed under the Discogs album artist, not inside the source
folder. Extra files and folders (Scans, Covers, Artwork, logs, cues,
playlists, nfo, leftover files in CD1/CD2, …) move with the album and
keep their relative paths. Empty source directories are removed up to
the destination root.

Artist names follow the Picard rules in the script: a leading `The`,
`Die`, or `Le` moves to the end (`Beatles, The`), and joiner words such
as `and`, `vs`, and `meets` become `&`. Catalog numbers lose spaces and
punctuation and are stored uppercase (`DOJO CD 167` becomes `DOJOCD167`).
Multi-disc albums get a `2CD` marker in the folder name. Track files are
`1. Title.flac`.

Vinyl and cassette sides are numbered straight through in Discogs order:
`A1 A2 A3 B1 B2` become tracks 1 to 5 (file names and tags). The side
position is kept in the `DISCOGS_POSITION` tag. Files that an older
version named with its `B1 = 21` scheme are recognised and renumbered.
Discogs "index" tracks (a work with movements) become one file per
movement, titled `Work: Movement`.

Tags written: artist, album artist, album, title, track and total, disc
and total, date, original date, label, catalog number, genre,
`DISCOGS_RELEASE_ID`, `DISCOGS_MASTER_ID`, and `DISCOGS_POSITION`, using
each format's own names (Picard conventions):

| Format | Tags |
| --- | --- |
| FLAC, Ogg Vorbis, Opus | Vorbis comments (`TITLE`, `TRACKNUMBER`, `ORIGINALDATE`, `LABEL`, ...) |
| MP3, WAV, AIFF | ID3v2.4 frames (`TIT2`, `TRCK`, `TDOR`, `TPUB`, `TXXX:CATALOGNUMBER`, ...) |
| M4A/MP4 | iTunes atoms plus `----:com.apple.iTunes:ORIGINALDATE`, `LABEL`, `CATALOGNUMBER`, `DISCOGS_*` |
| WMA | ASF attributes (`Title`, `Author`, `WM/AlbumTitle`, `WM/TrackNumber`, `WM/Publisher`, `WM/CatalogNo`, ...) |
| APE, WavPack | APEv2 (`Title`, `Track`, `Album Artist`, `Year`, ...) |

Only the fields above are replaced. Before one is written, other
spellings of the same field (`TOTALTRACKS` next to `TRACKTOTAL`, a
`TXXX:CATALOG` next to `TXXX:CATALOGNUMBER`, ...) are removed so no old,
contradictory copy is left. A field Discogs has no value for is left as
it is, except: the `DISCOGS_*` ids are always replaced as a group, and
when a file was tagged from a different Discogs release before, its
date, original date, label, catalog number, and genre are replaced too.
ReplayGain, AccurateRip, encoder and gapless info, embedded art, lyrics,
comments, and MusicBrainz ids are kept. `--clear-other-tags` also removes
the other text tags (comments, MusicBrainz ids, composer, ...), but still
keeps ReplayGain/R128, AccurateRip/CTDB, encoder info, art, lyrics, ISRC,
ratings, and embedded cue sheets or logs. `--dry-run` reads the files and
shows which tags would be removed.

A playlist written by `album-m3u` that comes along with the album lists
the old file names, so it is not moved: it is deleted and a fresh one is
written in the new folder. Hand-made playlists are moved as they are.

## Options

| Option | What it does |
| --- | --- |
| `path` | Album folder or a tree of albums. Required except for `--save-token` and `--self-test`. |
| `--dest DIR` | Library root to write into. |
| `--dry-run` | Search and print the plan. Write nothing. |
| `--auto` | Apply only a high-confidence match. |
| `-y`, `--yes` | Do not ask before writing. |
| `--skip-tagged` | Skip an album whose first file already has a Discogs release id. |
| `--release-id ID` | Use this release. One album only. |
| `--limit N` | Process at most N albums. `0` means all. Albums skipped by `--skip-tagged` do not count. |
| `--clear-other-tags` | Also remove text tags this tool does not write. See above for what is always kept. |
| `--save-token` | Prompt for a token, save it, and exit. |
| `--self-test` | Run the naming checks and exit. |
| `-h`, `--help` | Show this help. |

Without a TTY, pass `--release-id` or `--auto`. Otherwise the album is
reported as failed instead of waiting for a prompt.

## `album-m3u`

Writes one `.m3u` per album folder under `/run/media/xen_is/Tunes/Music`.

```sh
~/.local/bin/music-manager/album-m3u rebuild --dry-run
~/.local/bin/music-manager/album-m3u update
```

Playlists start with `#EXTM3U` and a `# generated by album-m3u` line, use
paths relative to the album folder, and are UTF-8 (file names that are
not valid UTF-8 are written byte for byte). A multi-disc album with
`CD1`, `CD2`, ... folders gets one playlist in the album folder listing
every disc in order.

Only generated playlists are replaced or deleted: files with that marker
line, and plain playlists from older versions (named after their folder,
listing only that folder's audio files in order). Every other `.m3u` or
`.m3u8` is left alone, even by `rebuild`. A hand-made playlist that lists
files that no longer exist is reported so you can review it.

`update` creates missing playlists, rewrites generated ones that are out
of date (renamed or added tracks), and removes generated playlists that a
newer one replaces (old per-disc playlists, a playlist named after the
album folder's previous name).

## Tests

```sh
python3 discogs-tag.py --self-test
python3 -m pytest tests        # needs pytest; no network access
```

`tests/samples` holds tiny silent files made with ffmpeg
(`tests/make_samples.sh`), and `tests/fixtures` holds saved Discogs JSON.
