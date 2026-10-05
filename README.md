# music-manager

Utilities for managing and tagging a local music library.

## `discogs-tag`

`discogs-tag.py` uses Discogs metadata and Mutagen to tag audio files and
normalize album folder and track names.

Create a Discogs personal token at <https://www.discogs.com/settings/developers>,
then save it locally:

```sh
./discogs-tag.py --save-token
```

Preview changes for an album without modifying files:

```sh
./discogs-tag.py --dry-run "/path/to/album"
```

Apply changes:

```sh
./discogs-tag.py "/path/to/album"
```
