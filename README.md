# music-manager

Utilities for the local music library. `discogs-tag` looks up a Discogs
release, writes tags, and renames the album into this library's folder
shape. `album-m3u` writes one playlist per album folder.

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

## Where files go

```text
DESTINATION/AlbumArtist/[original year]  Album [press year] [CATNO]/N. Title.ext
```

`%originaldate%` is the first year (master/original). `%date%` is the
press/release year after the title, even when the two match. A catalog
number is appended when Discogs has one. Example from the library:

```text
Abrasive Wheels/[1982]  When the Punks Go Marching In! [2006] [AHOYDPX25]/
```

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

Tags written include artist, album artist, album, title, track, disc,
date, original date, label, catalog number, genre, and
`DISCOGS_RELEASE_ID`.

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
| `--limit N` | Process at most N albums. `0` means all. |
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
