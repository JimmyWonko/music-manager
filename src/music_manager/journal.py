"""Journal: the append-only JSON-lines record of every change, and undo from it.
"""

from __future__ import annotations

import errno
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict, cast

from .fs import file_sha256
from .moves import _journal_rename, _mkdirs_logged, _move_across_devices, _rmdir_logged, _trash_logged
from .tags.snapshot import TagSnapshot, capture_tags, restore_tags


class JournalEntry(TypedDict, total=False):
    """One JSON line of a journal. Which keys appear depends on "action"."""
    time: str
    action: str
    command: str
    album: str
    dest: str
    release_id: int  # "start" of an apply: the release, its artist and title (older journals lack them)
    artist: str
    title: str
    root: str
    journal: str
    phase: str
    path: str
    snapshot: TagSnapshot
    sha256: str | None
    kind: str
    old: str
    new: str
    dev: int
    ino: int
    link: str
    trashed: str


# None: $XDG_STATE_HOME/discogs-tag (else ~/.local/state/discogs-tag), read when needed, never at
# import. service.configure(data_dir=...) sets it; tests may too.
STATE_DIR: Path | None = None


def state_dir() -> Path:
    """Where journals live: STATE_DIR when set, else $XDG_STATE_HOME/discogs-tag (~/.local/state)."""
    if STATE_DIR is not None:
        return Path(STATE_DIR)
    base = os.environ.get("XDG_STATE_HOME", "").strip()
    return (Path(base).expanduser() if base else Path.home() / ".local" / "state") / "discogs-tag"


class Journal:
    """Append-only JSON-lines record of renames, flushed and fsynced per line."""

    def __init__(self, kind: str, directory: Path | None = None):
        directory = directory or state_dir() / "journal"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for n in range(1000):
            suffix = f"-{n}" if n else ""
            path = directory / f"{kind}-{stamp}{suffix}.jsonl"
            try:
                self._fh = open(path, "x", encoding="utf-8")  # noqa: SIM115
            except FileExistsError:
                continue
            self.path = path
            break
        else:
            raise OSError(f"cannot create a journal in {directory}")

    def write(self, **entry: Any) -> None:
        line = cast(JournalEntry, {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **entry})
        self._fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def close(self) -> None:
        self._fh.close()


class _UndoView:
    """The file system as undo sees it.

    Live mode looks at the disk. Preview mode layers the effect of every
    step already listed on top, so later steps are checked as they would be
    when they run (a file moved back frees its new name, a recreated folder
    exists, and so on). Nothing on disk is changed in either mode.
    """

    _DIR = "<dir>"

    def __init__(self, simulate: bool):
        self.simulate = simulate
        self.over: dict[str, str | None] = {}
        self._hashes: dict[str, str | None] = {}

    def exists(self, path: Path) -> bool:
        key = str(path)
        if key in self.over:
            return self.over[key] is not None
        return os.path.lexists(path)

    def is_dir(self, path: Path) -> bool:
        key = str(path)
        if key in self.over:
            real = self.over[key]
            return real == self._DIR or (real is not None and os.path.isdir(real))
        return path.is_dir() and not path.is_symlink()

    def is_empty_dir(self, path: Path) -> bool:
        if not self.is_dir(path):
            return False
        names: set[str] = set()
        real = self.over.get(str(path), str(path))
        if real not in (None, self._DIR):
            try:
                names = set(os.listdir(real))
            except OSError:
                names = set()
        for key, value in self.over.items():
            parent, _, name = key.rpartition("/")
            if parent == str(path):
                if value is None:
                    names.discard(name)
                else:
                    names.add(name)
        return not names

    def sha256(self, path: Path) -> str | None:
        real = self.over.get(str(path), str(path))
        if real in (None, self._DIR):
            return None
        if not self.simulate:
            return file_sha256(Path(real))
        if real not in self._hashes:
            self._hashes[real] = file_sha256(Path(real))
        return self._hashes[real]

    def moved(self, src: Path, dest: Path) -> None:
        if self.simulate:
            self.over[str(dest)] = self.over.get(str(src), str(src))
            self.over[str(src)] = None

    def made_dir(self, path: Path) -> None:
        if self.simulate:
            self.over[str(path)] = self._DIR

    def removed(self, path: Path) -> None:
        if self.simulate:
            self.over[str(path)] = None


@dataclass
class UndoStep:
    """One reversing action. check() says why it can't run ("" when it can)."""

    label: str
    check: object
    run: object
    simulate: object
    # Structured description, for previews outside the CLI.
    action: str = ""  # rename, mkdir, rmdir, trashed, create, tags
    current: Path | None = None  # where the item is now
    original: Path | None = None  # where it goes back to (rename, trashed)
    index: int = -1  # entry index in the journal
    snapshot: dict | None = None  # tags: the snapshot restored
    legacy: bool = False  # tags: old pickled snapshot


def _move_back(journal: Journal, current: Path, original: Path) -> None:
    original.parent.mkdir(parents=True, exist_ok=True)
    if current.is_dir() and not current.is_symlink() and original.is_dir() and not original.is_symlink():
        _merge_back(journal, current, original)
        return
    _journal_rename(journal, "undo", current, original)
    try:
        os.rename(current, original)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        _move_across_devices(current, original, journal)


def _merge_back(journal: Journal, current: Path, original: Path) -> None:
    """Move a folder's contents back into a source folder that still exists.

    Never overwrites: a name already taken in original is left in current and
    reported (current is then kept).
    """
    blocked: list[str] = []
    for entry in sorted(current.iterdir()):
        target = original / entry.name
        if os.path.lexists(target):
            if entry.is_dir() and not entry.is_symlink() and target.is_dir() and not target.is_symlink():
                try:
                    _merge_back(journal, entry, target)
                    continue
                except OSError:
                    pass
            blocked.append(entry.name)
            continue
        _move_back(journal, entry, target)
    if blocked:
        raise OSError(errno.EEXIST, f"already exist in {original}, left in {current}: {', '.join(blocked)}")
    _rmdir_logged(journal, current)


def undo_plan(entries: list[JournalEntry], trust_legacy: bool = False) -> list[UndoStep]:
    """Reversing steps for a journal's entries, newest first.

    Every step re-checks the disk before it runs: something that changed
    since the journal was written (a file edited, moved, or replaced, or a
    name that is taken again) is skipped and reported, never overwritten.
    """
    after_hash: dict[int, str | None] = {}
    pending_before: dict[str, int] = {}
    for i, entry in enumerate(entries):
        if entry.get("action") != "tags":
            continue
        if entry.get("phase") == "before":
            pending_before[entry.get("path", "")] = i
        elif entry.get("phase") == "after" and entry.get("path") in pending_before:
            after_hash[pending_before.pop(entry["path"])] = entry.get("sha256")

    steps: list[UndoStep] = []
    for i in range(len(entries) - 1, -1, -1):
        entry = entries[i]
        action = entry.get("action")

        if action == "rename":
            current, original = Path(entry["new"]), Path(entry["old"])

            is_dir = entry.get("kind") == "dir"

            def check(view, e=entry, current=current, original=original, is_dir=is_dir):
                if not view.exists(current):
                    if is_dir and view.is_dir(original):
                        return ""  # moved back child by child (cross-device move)
                    return "no longer exists"
                if e.get("sha256"):
                    if view.sha256(current) != e["sha256"]:
                        return "changed since (contents differ)"
                elif "link" in e and str(current) not in view.over:
                    try:
                        if os.readlink(current) != e["link"]:
                            return "is not the link that was moved (changed since)"
                    except OSError:
                        return "is not the link that was moved (changed since)"
                elif is_dir and str(current) not in view.over:
                    # A cross-device folder move recreates it (new inode); its contents
                    # are journaled one by one, and merging back never overwrites.
                    if not view.is_dir(current):
                        return "is no longer a folder"
                elif str(current) not in view.over:
                    try:
                        st = os.lstat(current)
                    except OSError:
                        return "no longer exists"
                    if st.st_dev == e.get("dev") and st.st_ino != e.get("ino"):
                        return "is not the item that was renamed (changed since)"
                if view.exists(original):
                    if is_dir and view.is_dir(original) and view.is_dir(current):
                        return ""  # partial cross-device move: merge back into it
                    return f"{original.name!r} exists again"
                return ""

            def run(j, c=current, o=original):
                if os.path.lexists(c):
                    _move_back(j, c, o)

            def simulate(view, c=current, o=original):
                if view.exists(c) and not view.exists(o):
                    view.moved(c, o)
                elif view.exists(c):
                    view.removed(c)

            steps.append(UndoStep(f"move back  {current}\n    -> {original}", check, run, simulate,
                                  action="rename", current=current, original=original, index=i))

        elif action == "mkdir":
            folder = Path(entry["path"])
            steps.append(UndoStep(
                f"remove the folder it created  {folder}",
                lambda view, f=folder: "" if view.is_empty_dir(f) else ("no longer exists" if not view.exists(f) else "is not empty"),
                lambda j, f=folder: _rmdir_logged(j, f),
                lambda view, f=folder: view.removed(f),
                action="mkdir", current=folder, index=i,
            ))

        elif action == "rmdir":
            folder = Path(entry["path"])
            steps.append(UndoStep(
                f"recreate empty folder  {folder}",
                lambda view, f=folder: "" if not view.exists(f) else ("" if view.is_dir(f) else "a file now has that name"),
                lambda j, f=folder: _mkdirs_logged(j, f),
                lambda view, f=folder: view.made_dir(f),
                action="rmdir", original=folder, index=i,
            ))

        elif action == "trashed":
            original, trashed = Path(entry["path"]), Path(entry["trashed"])

            def check(view, e=entry, original=original, trashed=trashed):
                if not view.exists(trashed):
                    return "is no longer in the trash"
                if e.get("sha256") and view.sha256(trashed) != e["sha256"]:
                    return "the trashed copy changed"
                if view.exists(original):
                    return f"{original.name!r} exists again"
                return ""

            def run(j, original=original, trashed=trashed):
                _move_back(j, trashed, original)
                info = trashed.parent.parent / "info" / f"{trashed.name}.trashinfo"
                if info.is_file():
                    info.unlink()

            steps.append(UndoStep(
                f"restore from trash  {original}", check, run,
                lambda view, o=original, t=trashed: view.moved(t, o),
                action="trashed", current=trashed, original=original, index=i,
            ))

        elif action == "create":
            created = Path(entry["path"])
            steps.append(UndoStep(
                f"trash the playlist it wrote  {created}",
                lambda view, f=created, h=entry.get("sha256"): (
                    "no longer exists" if not view.exists(f)
                    else "changed since (contents differ)" if view.sha256(f) != h else ""),
                lambda j, f=created: _trash_logged(j, f),
                lambda view, f=created: view.removed(f),
                action="create", current=created, index=i,
            ))

        elif action == "tags" and entry.get("phase") == "before":
            path = Path(entry["path"])
            expected = after_hash.get(i)
            finished = i in after_hash

            snap = entry.get("snapshot") or {"kind": "none"}
            legacy = snap.get("kind") != "none" and "v" not in snap and "data" in snap

            def check(view, path=path, expected=expected, finished=finished, legacy=legacy):
                if legacy and not trust_legacy:
                    return ("old journal format (pickled tag snapshot); not loaded for safety. "
                            "Re-run with --trust-legacy-journal if this journal is your own")
                if not view.exists(path):
                    return "is not back in its old place"
                if finished and view.sha256(path) != expected:
                    return "changed since it was tagged"
                return ""

            def run(j, path=path, snapshot=snap):
                j.write(action="tags", phase="before", path=str(path), snapshot=capture_tags(path))
                restore_tags(path, snapshot, trust_legacy=trust_legacy)
                j.write(action="tags", phase="after", path=str(path), sha256=file_sha256(path))

            label = f"restore the original tags  {path}"
            if not finished:
                label += "  (the tag write never finished)"
            steps.append(UndoStep(label, check, run, lambda view: None, action="tags", current=path, original=path,
                                  index=i, snapshot=snap, legacy=legacy))

    return steps


def read_journal(journal_path: Path, log=print) -> list[JournalEntry]:
    """A journal file's entries; lines that are not valid JSON are reported and ignored."""
    entries = []
    with open(journal_path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                log(f"  warning: line {n} of the journal is not valid JSON; ignored")
    return entries


def undo_journal(journal_path: Path, apply: bool, journal_dir: Path | None = None,
                 trust_legacy: bool = False) -> int:
    """Reverse everything a journal recorded, newest first.

    Without apply this is the undo plan: every step that would run and every
    step that would be skipped, with nothing changed on disk.
    """
    entries = read_journal(journal_path)
    steps = undo_plan(entries, trust_legacy=trust_legacy)
    mode = "APPLY" if apply else "PREVIEW (nothing is changed; add --apply to undo)"
    print(f"undo {journal_path}\n{mode}\n")

    skipped: list[tuple[str, str]] = []
    if not apply:
        view = _UndoView(simulate=True)
        ready = 0
        for step in steps:
            reason = step.check(view)
            if reason:
                skipped.append((step.label, reason))
                continue
            step.simulate(view)
            ready += 1
            print(f"  {step.label}")
        for label, reason in skipped:
            print(f"  would skip  {label}: {reason}")
        print(f"\n{ready} changes would be reversed, {len(skipped)} skipped")
        return 0

    view = _UndoView(simulate=False)
    journal = Journal("undo", journal_dir)
    journal.write(action="start", command="undo-journal", journal=str(journal_path))
    done = 0
    try:
        for step in steps:
            reason = step.check(view)
            if not reason:
                try:
                    step.run(journal)
                    print(f"  {step.label}")
                    done += 1
                    continue
                except (OSError, ValueError, TypeError) as exc:
                    reason = str(exc)
            skipped.append((step.label, reason))
    finally:
        journal.close()
    for label, reason in skipped:
        print(f"  skipped  {label}: {reason}")
    print(f"\nreversed {done} changes, {len(skipped)} skipped")
    print(f"journal of this undo: {journal.path}")
    return 1 if skipped else 0
