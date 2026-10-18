"""Why an album is held: the structured detail behind disc_ambiguous and layout_ambiguous.

Shared by the engine (match, fs, apply, the CLI) and the service, which returns these
frozen dataclasses on Problem.disc_conflicts and Problem.layout.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

# Why a file's disc cannot be told:
#   numbering            the flat folder's numbers are not the release's per-disc numbering
#   no_title_match       its title matches none of the same-number tracks
#   several_title_matches its title matches more than one of them
#   same_track           its title picks the same track as another file with that number
#   held_with_group      its title picks one track, but a file with the same number can't be
#                        placed, so the whole group is held
#   sources_disagree     its CD folder, disc tag and the folder's .cue/.m3u don't all agree
DiscReason = Literal["numbering", "no_title_match", "several_title_matches", "same_track", "held_with_group",
                     "sources_disagree"]


@dataclass(frozen=True)
class DiscChoice:
    """A release track a file could be."""
    disc: int
    position: str  # as Discogs prints it ("1-03", "B2"); "<disc>-<number>" when Discogs has none
    number: int
    title: str
    title_similarity: float  # the file's title against this track's, 0..1


@dataclass(frozen=True)
class DiscConflict:
    """One file the pairing refused, and what it conflicts with."""
    path: Path
    number: int | None  # the track number in its name (None if it has none)
    title: str  # the title part of its name
    reason: DiscReason
    same_number: list[DiscChoice] = field(default_factory=list)  # release tracks with that number
    title_matches: list[DiscChoice] = field(default_factory=list)  # tracks its title matches, best first
    # sources_disagree only (None = that source says nothing):
    folder_disc: int | None = None  # from its CD1/Disc 2 folder
    tag_disc: int | None = None  # its DISCNUMBER tag
    sidecar_disc: int | None = None  # the disc a .cue/.m3u in the album lists it on
    sidecar: Path | None = None  # that .cue/.m3u


@dataclass(frozen=True)
class DiscFolderMismatch:
    """A track the destination would put into a CD1 / Disc 2 folder of another disc."""
    path: Path  # the file now
    destination: Path  # where the layout would put it
    folder: Path  # the CD/Disc folder in that destination
    folder_disc: int  # the disc that folder's name says
    tag_disc: int  # the disc tag the file gets (the release's disc for it)


def describe_folder_mismatch(m: DiscFolderMismatch) -> str:
    return f"{m.path.name} -> {m.folder.name}/ (disc tag {m.tag_disc})"


LayoutReason = Literal["disc_folders", "album_folders"]


@dataclass(frozen=True)
class LayoutConflict:
    """Why a folder with loose audio is held (see fs.collect_albums)."""
    reason: LayoutReason  # disc_folders: CD1/CD2 with audio; album_folders: other folders with audio
    subfolders: list[Path]  # the subfolders that caused the hold
    loose_files: list[Path]  # the audio files directly in the folder


def _choice_text(c: DiscChoice) -> str:
    return f'{c.position} "{c.title}"'


def describe_disc_conflict(c: DiscConflict, base: Path | None = None) -> str:
    """One line for the CLI and dialogs: the file, then what it conflicts with."""
    name = str(c.path.relative_to(base)) if base is not None and base in c.path.parents else c.path.name
    if c.reason == "sources_disagree":
        def disc(value):
            return "-" if value is None else str(value)
        where = f" ({c.sidecar.name})" if c.sidecar is not None else ""
        return (f"{name}: folder says disc {disc(c.folder_disc)}, DISCNUMBER tag says {disc(c.tag_disc)}, "
                f"cue/m3u{where} says {disc(c.sidecar_disc)}")
    parts = []
    if c.same_number:
        parts.append(f"number {c.number} is " + " or ".join(_choice_text(x) for x in c.same_number))
    elif c.number is not None:
        parts.append(f"number {c.number}")
    if c.title_matches:
        parts.append("title matches " + ", ".join(_choice_text(x) for x in c.title_matches))
    else:
        parts.append("title matches no track")
    why = {
        "numbering": "the folder's numbers don't restart per disc the way the release's do",
        "no_title_match": "its title fits none of the same-number tracks",
        "several_title_matches": "its title fits more than one of them",
        "same_track": "another file with that number picks the same track",
        "held_with_group": "held with the other files numbered " + str(c.number),
    }[c.reason]
    return f"{name}: " + "; ".join(parts) + f" ({why})"
