"""Read-only check for multi-disc albums whose disc-1 and disc-2 tracks look swapped.

The tags of such an album say disc 1 track 1 is "One" and disc 2 track 1 is "Four", but
the audio in those files is the other way round (what known gap 18 did on a re-apply).
The tags agree with Discogs, so only the audio can tell: the main signal is each file's
real length against the Discogs length at its tagged disc and position. For every track
number that is on two or more discs, the current assignment is compared with the best
other assignment of those files to those discs; a swap is reported only when it fits the
lengths clearly better (SWAP_MARGIN_S). Title tags against the release, and against what the
last apply journal recorded, are secondary evidence.

Strictly read-only: audio files are opened for reading only, nothing is tagged, moved or
indexed, no journal is written and the Discogs cache is only read unless cache writes are
explicitly allowed. The only file written is the report the caller asks for.

Independently of the lengths, every file's CD1/Disc 2 folder is compared with its disc tag,
and the album's .cue/.m3u files that list it break the tie (see discsources). A file whose
sources disagree makes the album "disagree" (flagged, like a swap), with the evidence per file.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from mutagen import File as MutagenFile

from .discsources import DiscSources, disagreements
from .fs import AlbumFolder, collect_albums
from .match import TITLE_MATCH_MIN, similar
from .providers.discogs import (
    DiscogsError,
    DiscogsRelease,
    flatten_tracklist,
    parse_position,
    release_artists,
)
from .tags.snapshot import snapshot_text_map
from .tags.textmap import tag_text_map

# A swap must beat the current assignment by at least this many seconds of length error per
# track (averaged over the files of one number), and leave at most half the error, to count as
# evidence; the current assignment must win by as much to count as "keep".
SWAP_MARGIN_S = 5.0
SWAP_KEEP_RATIO = 0.5


class ReleaseSource(Protocol):
    def release(self, release_id: int) -> DiscogsRelease: ...


@dataclass
class FileFacts:
    path: Path
    length_s: float | None
    disc: int | None
    number: int | None
    title: str
    release_id: str
    position: str  # the discogs_position tag


@dataclass
class TrackEvidence:
    path: Path
    tagged: str  # "1-2"
    title_tag: str
    length_s: float | None
    discogs_s: float | None  # Discogs length at the tagged position
    better: str  # position the best other assignment gives it ("" when it stays)
    better_s: float | None


@dataclass
class NumberEvidence:
    number: int
    discs: list[int]
    current_error_s: float | None
    best_other_error_s: float | None
    verdict: str  # "swap", "keep" or "unclear"
    tracks: list[TrackEvidence]


@dataclass
class AlbumCheck:
    path: Path
    release_id: int | None
    # "likely swapped", "mixed", "disagree", "ok", "inconclusive", "single disc", "skipped", "error"
    status: str
    reason: str = ""
    numbers: list[NumberEvidence] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # secondary: tags, titles, journal
    disagreements: list[DiscSources] = field(default_factory=list)  # folder vs disc tag vs cue/m3u

    @property
    def flagged(self) -> bool:
        return self.status in ("likely swapped", "mixed", "disagree")


def _leading_int(text: str) -> int | None:
    digits = ""
    for ch in (text or "").strip():
        if not ch.isdigit():
            break
        digits += ch
    return int(digits) if digits else None


def read_facts(path: Path) -> FileFacts:
    """Length and tags of one file, opened read-only."""
    try:
        audio = MutagenFile(path)
    except Exception:  # noqa: BLE001 - an unreadable file just has no facts
        audio = None
    length = None
    tags: dict[str, str] = {}
    if audio is not None:
        info = getattr(audio, "info", None)
        length = float(info.length) if info is not None and getattr(info, "length", None) else None
        tags = tag_text_map(audio.tags)
    disc = _leading_int(tags.get("discnumber", ""))
    number = _leading_int(tags.get("tracknumber", ""))
    position = tags.get("discogs_position", "").strip()
    if (disc is None or number is None) and position:
        try:
            p_disc, p_number = parse_position(position)
        except Exception:  # noqa: BLE001
            p_disc = p_number = None
        disc = disc if disc is not None else p_disc
        number = number if number is not None else p_number
    return FileFacts(path, length, disc, number, tags.get("title", ""), tags.get("discogs_release_id", "").strip(),
                     position)


# --- journals (secondary evidence) ---------------------------------------------------------------

def _journal_files(journal_dir: Path | None) -> list[Path]:
    if journal_dir is None or not journal_dir.is_dir():
        return []
    return sorted(journal_dir.glob("*.jsonl"))


def _read_lines(path: Path) -> list[dict]:
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict):
                    out.append(entry)
    except OSError:
        return []
    return out


@dataclass
class JournalFacts:
    release_ids: dict[str, int] = field(default_factory=dict)  # album folder (dest) -> release id
    before: dict[str, tuple[str, dict[str, str]]] = field(default_factory=dict)  # final path -> (journal, tags before)


def read_journals(journal_dir: Path | None) -> JournalFacts:
    """What apply journals recorded: each album's release, and each file's tags before its last apply."""
    facts = JournalFacts()
    for jpath in _journal_files(journal_dir):
        entries = _read_lines(jpath)
        if not any(e.get("action") == "start" and e.get("command") == "apply" for e in entries):
            continue
        tracked: dict[str, dict[str, str]] = {}  # current path -> tags before
        for e in entries:
            action = e.get("action")
            if action == "start" and e.get("command") == "apply" and e.get("release_id"):
                facts.release_ids[str(e.get("dest"))] = int(e["release_id"])
            elif action == "tags" and e.get("phase") == "before" and e.get("path"):
                tracked[str(e["path"])] = snapshot_text_map(e.get("snapshot") or {"kind": "none"})
            elif action == "rename" and e.get("old") and e.get("new"):
                old, new = str(e["old"]), str(e["new"])
                for cur in list(tracked):
                    if cur == old:
                        tracked[new] = tracked.pop(cur)
                    elif e.get("kind") == "dir" and cur.startswith(old.rstrip("/") + "/"):
                        tracked[new.rstrip("/") + cur[len(old.rstrip("/")):]] = tracked.pop(cur)
        for cur, tags in tracked.items():
            facts.before[cur] = (jpath.name, tags)
    return facts


# --- the check -----------------------------------------------------------------------------------

def _album_release_id(files: list[FileFacts], album: AlbumFolder, journals: JournalFacts) -> tuple[int | None, str]:
    ids = {f.release_id for f in files if f.release_id}
    if len(ids) == 1:
        rid = ids.pop()
        if rid.isdigit():
            return int(rid), "tags"
    if len(ids) > 1:
        return None, f"files carry different Discogs release ids ({', '.join(sorted(ids))})"
    rid = journals.release_ids.get(str(album.path))
    if rid:
        return rid, "journal"
    return None, "no Discogs release id in the tags or the journals"


def _fmt(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    s = round(seconds)
    return f"{s // 60}:{s % 60:02d}"


def _judge(number: int, by_disc: dict[int, FileFacts], lengths: dict[tuple[int, int], float | None]
           ) -> NumberEvidence:
    discs = sorted(by_disc)
    timed = [d for d in discs if by_disc[d].length_s is not None and lengths.get((d, number)) is not None]

    def cost(assign: dict[int, int]) -> float:
        return sum(abs(by_disc[d].length_s - lengths[(assign[d], number)]) for d in timed)

    current = best = None
    best_assign: dict[int, int] = {}
    if len(timed) >= 2:
        identity = {d: d for d in timed}
        current = cost(identity)
        if len(timed) <= 6:
            perms = list(itertools.permutations(timed))
        else:  # many discs: single swaps only
            perms = []
            for i, j in itertools.combinations(range(len(timed)), 2):
                perm = list(timed)
                perm[i], perm[j] = perm[j], perm[i]
                perms.append(tuple(perm))
        for perm in perms:
            assign = dict(zip(timed, perm, strict=True))
            if assign == identity:
                continue
            c = cost(assign)
            if best is None or c < best:
                best, best_assign = c, assign
    if current is None or best is None:
        verdict = "unclear"
    elif (current - best) / len(timed) >= SWAP_MARGIN_S and best <= SWAP_KEEP_RATIO * current:
        verdict = "swap"
    elif (best - current) / len(timed) >= SWAP_MARGIN_S:
        verdict = "keep"
    else:
        verdict = "unclear"
    tracks = []
    for d in discs:
        f = by_disc[d]
        other = best_assign.get(d, d) if verdict == "swap" else d
        tracks.append(TrackEvidence(f.path, f"{d}-{number}", f.title, f.length_s, lengths.get((d, number)),
                                    f"{other}-{number}" if other != d else "",
                                    lengths.get((other, number)) if other != d else None))
    return NumberEvidence(number, discs, current, best, verdict, tracks)


def check_album(album: AlbumFolder, source: ReleaseSource, journals: JournalFacts,
                releases: dict[int, DiscogsRelease | Exception] | None = None) -> AlbumCheck:
    """The length check (see _check_lengths), plus folder vs disc tag vs cue/m3u for every file.

    A disagreement makes the status "disagree" unless the lengths already flagged the album.
    """
    result = _check_lengths(album, source, journals, releases)
    result.disagreements = disagreements(album)
    if result.disagreements and not result.flagged:
        if result.status not in ("ok", "inconclusive", "single disc") and result.reason:
            result.notes.append(f"length check {result.status}: {result.reason}")
        result.status = "disagree"
        result.reason = (f"{len(result.disagreements)} file(s) sit in a disc folder that their disc tag or "
                         "the album's cue/m3u files contradict")
    return result


def _check_lengths(album: AlbumFolder, source: ReleaseSource, journals: JournalFacts,
                   releases: dict[int, DiscogsRelease | Exception] | None = None) -> AlbumCheck:
    files = [read_facts(p) for p in album.tracks]
    rid, how = _album_release_id(files, album, journals)
    if rid is None:
        return AlbumCheck(album.path, None, "skipped", how)
    releases = releases if releases is not None else {}
    if rid not in releases:
        try:
            releases[rid] = source.release(rid)
        except (DiscogsError, OSError, ValueError, KeyError) as exc:
            releases[rid] = exc
    release = releases[rid]
    if isinstance(release, Exception):
        return AlbumCheck(album.path, rid, "error", f"release {rid} not available: {release}")
    dtracks = flatten_tracklist(release, release_artists(release))
    if len({dt.disc for dt in dtracks}) < 2:
        return AlbumCheck(album.path, rid, "single disc", "the release has one disc")
    at = {(dt.disc, dt.number): dt for dt in dtracks}
    lengths = {key: dt.duration_s for key, dt in at.items()}
    result = AlbumCheck(album.path, rid, "inconclusive", f"release id from {how}")

    groups: dict[int, dict[int, FileFacts]] = {}
    for f in files:
        name = f.path.relative_to(album.path) if album.path in f.path.parents else f.path.name
        if f.disc is None or f.number is None:
            result.notes.append(f"{name}: no disc/track tags")
            continue
        if f.position:
            try:
                pos = parse_position(f.position)
            except Exception:  # noqa: BLE001
                pos = None
            dt_pos = next((dt for dt in dtracks if dt.position == f.position), None)
            if dt_pos is not None and (dt_pos.disc, dt_pos.number) != (f.disc, f.number):
                result.notes.append(f"{name}: tagged {f.disc}-{f.number} but its discogs_position tag "
                                    f"{f.position} is {dt_pos.disc}-{dt_pos.number}")
            elif dt_pos is None and pos is not None and pos != (f.disc, f.number):
                result.notes.append(f"{name}: tagged {f.disc}-{f.number} but discogs_position is {f.position}")
        dt = at.get((f.disc, f.number))
        if dt is None:
            result.notes.append(f"{name}: tagged {f.disc}-{f.number}, which the release does not have")
            continue
        if f.title and similar(f.title, dt.title) < TITLE_MATCH_MIN:
            others = [o for o in dtracks if o.number == f.number and o.disc != f.disc
                      and similar(f.title, o.title) >= TITLE_MATCH_MIN]
            extra = f" (that is the title of {others[0].disc}-{others[0].number})" if others else ""
            result.notes.append(f"{name}: title tag {f.title!r} is not the release's {f.disc}-{f.number} "
                                f"{dt.title!r}{extra}")
        before = journals.before.get(str(f.path))
        if before is not None:
            jname, old = before
            old_disc = _leading_int(old.get("discnumber", ""))
            old_title = old.get("title", "")
            if old_disc is not None and old_disc != f.disc and _leading_int(old.get("tracknumber", "")) == f.number:
                was = at.get((old_disc, f.number))
                fits = was is not None and old_title and similar(old_title, was.title) >= TITLE_MATCH_MIN
                result.notes.append(
                    f"{name}: the last apply ({jname}) moved it from {old_disc}-{f.number} {old_title!r} to "
                    f"{f.disc}-{f.number} {f.title!r}" + (" (its old tags matched the release: what a swap does)"
                                                          if fits else ""))
        groups.setdefault(f.number, {})
        if f.disc in groups[f.number]:
            result.notes.append(f"{name}: a second file tagged {f.disc}-{f.number}")
            continue
        groups[f.number][f.disc] = f

    for number in sorted(groups):
        if len(groups[number]) >= 2:
            result.numbers.append(_judge(number, groups[number], lengths))
    swaps = sum(1 for n in result.numbers if n.verdict == "swap")
    keeps = sum(1 for n in result.numbers if n.verdict == "keep")
    if not result.numbers:
        result.reason = "no track number is on two discs"
    elif swaps and not keeps:
        result.status = "likely swapped"
    elif swaps and keeps:
        result.status = "mixed"
    elif keeps:
        result.status = "ok"
    else:
        result.reason = "the lengths cannot tell (missing Discogs durations, or too close)"
    return result


def check_disc_swaps(root: Path, source: ReleaseSource, journal_dir: Path | None = None) -> list[AlbumCheck]:
    """Check every album folder under root. Read-only (see the module docstring)."""
    journals = read_journals(journal_dir)
    releases: dict[int, DiscogsRelease | Exception] = {}
    return [check_album(album, source, journals, releases) for album in collect_albums(root)]


class CacheOnlySource:
    """Releases from the Discogs cache only (no token): fresh files are read, nothing is written."""

    def release(self, release_id: int) -> DiscogsRelease:
        import hashlib
        import time

        from .providers.discogs import CACHE_MAX_AGE, cache_dir

        path = cache_dir() / "release" / f"{hashlib.sha1(str(release_id).encode()).hexdigest()}.json"
        try:
            if time.time() - path.stat().st_mtime >= CACHE_MAX_AGE:
                raise DiscogsError("cached copy is older than 6 hours and there is no Discogs token")
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise DiscogsError("not in the Discogs cache and there is no Discogs token") from None


def _disc(value: int | None) -> str:
    return "-" if value is None else str(value)


def _disagreement_line(album_path: Path, e: DiscSources) -> str:
    name = e.path.relative_to(album_path) if album_path in e.path.parents else e.path.name
    folder = f"folder {e.path.parent.name} (disc {e.folder_disc})" if e.folder_disc is not None else "no disc folder"
    if e.sidecar_discs:
        listed = ", ".join(p.name for p in e.sidecars)
        cue = f"cue/m3u disc {'/'.join(map(str, e.sidecar_discs))} ({listed})"
    else:
        cue = "no cue/m3u lists it"
    verdict = {"folder": "the folder is wrong", "tag": "the disc tag is wrong",
               "sidecar": "the cue/m3u is wrong"}.get(e.odd_one_out, "cannot tell which is wrong")
    return f"{name}: {folder}, DISCNUMBER {_disc(e.tag_disc)}, {cue} -> {verdict}"


def format_report(checks: list[AlbumCheck], verbose: bool = False, cache_written: bool = False) -> str:
    lines: list[str] = []
    flagged = [c for c in checks if c.flagged]
    for c in checks:
        if not (verbose or c.flagged or c.status in ("ok", "inconclusive", "error")):
            continue
        lines.append(f"{c.status.upper():15} {c.path}" + (f"   [Discogs {c.release_id}]" if c.release_id else ""))
        if c.reason:
            lines.append(f"    {c.reason}")
        for n in c.numbers:
            lines.append(f"    track {n.number} on discs {', '.join(map(str, n.discs))}: {n.verdict}"
                         f"  (length error now {_fmt(n.current_error_s)}, swapped {_fmt(n.best_other_error_s)})")
            for t in n.tracks:
                name = t.path.name
                line = (f"      {t.tagged:>5} {name}  title {t.title_tag!r}  file {_fmt(t.length_s)}"
                        f"  Discogs {t.tagged} {_fmt(t.discogs_s)}")
                if t.better:
                    line += f"  Discogs {t.better} {_fmt(t.better_s)}  -> the audio is {t.better}"
                lines.append(line)
        for e in c.disagreements:
            lines.append(f"    {_disagreement_line(c.path, e)}")
        for note in c.notes:
            lines.append(f"    note: {note}")
    swapped = sum(1 for c in flagged if c.status == "likely swapped")
    disagree = sum(1 for c in flagged if c.status == "disagree" or c.disagreements)
    detail = ", ".join(x for x in (f"{swapped} likely swapped" if swapped else "",
                                     f"{disagree} with disc folders that disagree" if disagree else "") if x)
    lines.append(f"checked {len(checks)} album(s): {len(flagged)} flagged" + (f" ({detail})" if detail else ""))
    lines.append("read-only: nothing was tagged, moved or journaled"
                 + ("; Discogs responses may have been cached (cache writes allowed)" if cache_written
                    else ", and the Discogs cache was not written"))
    return "\n".join(lines) + "\n"
