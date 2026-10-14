# music-manager

Utilities for a local music library. `discogs-tag` looks up a Discogs
release, writes tags, and renames the album into the folder shape below.
`album-m3u` writes one playlist per album folder. Both use `mm_common.py`.

The tool retags files and moves them. Start with `--dry-run` on a copy
of an album.

## Install

Python 3.12 or newer.

```sh
pip install .
discogs-tag --help
album-m3u --help
```

That installs both commands and their dependencies, `requests` and
`mutagen`. For the test suite, `pip install -e ".[dev]"` also installs
pytest.

From a checkout, without installing:

```sh
python3 discogs-tag.py --help
python3 album-m3u --help
```

`discogs-tag.py` and `album-m3u` are launchers. The implementation is
`discogs_tag.py`, `album_m3u.py`, and `mm_common.py`. A symlink to a
launcher still finds `mm_common.py`.

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

Search results are cached under `~/.cache/discogs-tag` for at most 6 hours,
as Discogs' API terms require. Older answers are fetched again.
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
DESTINATION/AlbumArtist/[LEADYEAR]  Album [RELEASEYEAR] [CATNO]/N. Title.ext
```

The folder starts with a year, so each artist's albums sort
chronologically. For a reissue (the Discogs master's year is earlier
than the release's year) that is the master's year; otherwise it is the
release's own year. The release year after the title only appears for a
reissue. Anything unknown is left out together with its brackets and
spaces: no catalog number means no `[CATNO]`, and with no year at all
the name is just `Album [CATNO]` or `Album`. Examples from the library:

```text
Black Flag/[1979]  Nervous Breakdown [1988] [SSTCD001]/   (1988 reissue of a 1979 release)
Black Flag/[1984]  My War [SST023]/                       (original release)
Black Flag/[1983]  The First Four Years [1990]/           (reissue, no catalog number)
Black Flag/[2013]  What The…/                             (original release, no catalog number)
```

Tags are stricter than folder names: `ORIGINALDATE`/`ORIGINALYEAR` (and
each format's equivalents) are only written for a reissue. When an album
this tool tagged before turns out not to be a reissue, the original-date
tags it wrote earlier are removed. An original date written by another
tagger is left alone.

`DESTINATION` is the first match:

1. `--dest`
2. The `Featuring` directory, when the path is under one
3. `$DEFAULT_MUSIC`, only if you set it

There is no built-in library path. If none of these applies, `discogs-tag`
stops with an error before reading tags or moving anything. Put
`export DEFAULT_MUSIC=/path/to/your/library` in your shell profile to keep
the old "file into my library" behaviour.

Albums are filed under the Discogs album artist, not inside the source
folder. Extra files and folders (Scans, Covers, Artwork, logs, cues,
playlists, nfo, leftover files in CD1/CD2, hidden files and folders, …)
move with the album and keep their relative paths. Only trash folders
(`.Trash-1000`, `.Trashes`) and `.git` stay behind; a git checkout (such
as a copy of this tool) is never treated as an album.

Every real run is journaled and can be undone. Before each change, a
line goes into a journal in `~/.local/state/discogs-tag/journal/`
(`$XDG_STATE_HOME` if set): every file's tags before and after (all of
them, cover art included), every move with the file's SHA-256, every
folder created or removed, every playlist trashed or written. The end of
the run prints the journal path. `discogs-tag --undo-journal FILE` shows
what undo would do without changing anything; add `--apply` to do it.
Undo works newest first, and anything that changed since (a file edited
or replaced, a name taken again) is skipped and reported, never
overwritten. Tags and audio come back as they were; FLAC and other files
may differ in tag padding, so the file is not always byte-identical.

After a successful move, the old album folder is removed, together with
its emptied `CD1`/`CD2` folders and every parent folder that became
empty, up to and including the folder you passed on the command line
(nothing above it). Inside the library this stops below the library
root, so an emptied artist folder goes but the root never does. Folders
are removed with `rmdir` only, so a folder that still holds anything at
all (a hidden file, an unmatched track that was left in place) is kept.

Moving to another drive is a real move: each file is copied to a hidden
partial file, flushed to disk, read back and compared (size and
SHA-256), renamed into place, and only then is the original deleted. If
the check fails, the copy is removed and the original stays.

Artist names follow the Picard rules in the script: a leading `The`,
`Die`, or `Le` moves to the end (`Beatles, The`), and joiner words such
as `and`, `vs`, and `meets` become `&`. Catalog numbers lose spaces and
punctuation and are stored uppercase (`DOJO CD 167` becomes `DOJOCD167`).
Multi-disc albums get a `2CD` marker in the folder name. Track files are
`1. Title.flac`.

Vinyl and cassette tracks are named by side position, the way collectors
list them: `A1. Title.flac`, `A2. Title.flac`, `B1. Title.flac`. The
track-number tag still counts straight through in Discogs order (`A1 A2 A3
B1 B2` are tracks 1 to 5) so players sort and display them normally, and
the side position is kept in the `DISCOGS_POSITION` tag. Pass
`--sequential-sides` to name the files `1. 2. 3.` instead. A disc whose
positions are not all distinct sides (mixed or repeated positions) falls
back to numbers. Files already named `A1. Title`, `1. Title`, or with an
older version's `B1 = 21` scheme are all recognised.
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
the old file names, so it is not moved: it goes to the trash
(`~/.local/share/Trash`, where your file manager can restore it) and a
fresh one is written in the new folder. Hand-made playlists are moved as they are.

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
| `--normalize-folders` | Rename album folders under `path` to the current naming rule. Preview unless `--apply`. |
| `--undo-journal FILE` | Reverse everything recorded in a journal (an apply or `--normalize-folders`). Preview unless `--apply`. |
| `--apply` | Really make the change (only with `--normalize-folders` or `--undo-journal`). |
| `-h`, `--help` | Show this help. |

Without a TTY, pass `--release-id` or `--auto`. Otherwise the album is
reported as failed instead of waiting for a prompt.

## Clean up folder names (`--normalize-folders`)

Folders made by older versions repeat the year for non-reissues
(`[1984]  My War [1984] [SST023]`). This renames them to the current rule
without contacting Discogs and without changing any file:

```sh
discogs-tag --normalize-folders /path/to/library           # preview
discogs-tag --normalize-folders --apply /path/to/library   # rename
discogs-tag --undo-journal ~/.local/state/discogs-tag/journal/normalize-folders-YYYYMMDD-HHMMSS.jsonl          # preview the undo
discogs-tag --undo-journal ~/.local/state/discogs-tag/journal/normalize-folders-YYYYMMDD-HHMMSS.jsonl --apply  # undo
```

- Only album folders (folders with audio) are considered, never artist
  folders. Only names in the exact shape this tool writes,
  `[Y1]  Album[ 2CD] [Y2][ [CATNO]]`, are touched.
- `Y1 == Y2`: the second year is removed. If the album's tags have an
  `ORIGINALDATE`/`ORIGINALYEAR` earlier than `DATE` (a reissue), the
  folder is skipped instead. `Y1 < Y2` (a reissue) is already right.
  An old `[]  Album []` with no year becomes `Album`.
- Everything else is skipped and listed with the reason: other naming
  styles, `[Y]  Album [CATNO] [Y]`, a leading year later than the second,
  a catalog number that looks like a year, brackets in the title.
- If the new name already exists, or two folders would get the same
  name, both are left alone and reported as conflicts. Nothing is merged
  or overwritten.
- A playlist generated by `album-m3u` and named after the folder is
  renamed with it. Files are never retagged.
- The preview prints every `old -> new`, the skipped folders with
  reasons, the conflicts, and the counts. `--apply` writes every rename
  to a JSON-lines journal (old and new absolute paths, time, inode) in
  `~/.local/state/discogs-tag/journal/` (`$XDG_STATE_HOME` if set), and
  prints the undo command.
- `--undo-journal FILE --apply` reverses the journal newest first. An
  entry is only reversed when the renamed folder is still there (same
  inode) and its old name is free; anything that changed since is skipped
  and reported. The undo writes its own journal.

## `album-m3u`

Writes one `.m3u` per album folder under the library root (`--root`, or `$DEFAULT_MUSIC`).

```sh
album-m3u rebuild --dry-run --root /path/to/library
album-m3u update --root /path/to/library
```

Playlists start with `#EXTM3U` and a `# generated by album-m3u` line, use
paths relative to the album folder, and are UTF-8 (file names that are
not valid UTF-8 are written byte for byte). A multi-disc album with
`CD1`, `CD2`, ... folders gets one playlist in the album folder listing
every disc in order.

Only generated playlists are replaced or sent to the trash: files with that marker
line, and plain playlists from older versions (named after their folder,
listing only that folder's audio files in order). Every other `.m3u` or
`.m3u8` is left alone, even by `rebuild`. A hand-made playlist that lists
files that no longer exist is reported so you can review it.

`update` creates missing playlists, rewrites generated ones that are out
of date (renamed or added tracks), and removes generated playlists that a
newer one replaces (sent to the trash, never deleted outright: old per-disc playlists, a playlist named after the
album folder's previous name).

## Tests

```sh
python3 discogs-tag.py --self-test
python3 -m pytest tests        # needs pytest; no network access
```

`tests/samples` holds tiny silent files made with ffmpeg
(`tests/make_samples.sh`), and `tests/fixtures` holds saved Discogs JSON.

## License

music-manager is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version. It is distributed WITHOUT ANY WARRANTY. See the [LICENSE](LICENSE) file for the full text.

Copyright (C) 2026 Jami L. Boucher
