"""Contract-shaped facade over FakeService (MOCK ONLY).

Exposes the same namespaces as `music_manager.service` per API-CONTRACT.md v0 (library, match,
provider, plan, journal, templates, settings, jobs, events + types/errors) so the UI runs identical
code on both backends. Everything mock-specific lives here, never in the UI modules:

- 100k synthetic "Mock Artist" albums padding unfiltered / UNMATCHED views (lazy-paging test)
- a small template engine implementing the contract's field rules (for match.destination,
  templates.preview and plan.build), in-memory templates/settings, and a journal with
  kind/undone/undone_by/undo_of
- slow calls (apply, undo, scan, ...) become Jobs finished on the next event-loop turn
"""
from __future__ import annotations

import bisect
import functools
import itertools
import time
import re
from dataclasses import fields as dc_fields, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PySide6.QtCore import QTimer

STALE_AFTER_S = 6 * 3600       # contract: a release fetched longer ago than this counts as not fetched
FETCH_ERROR_HOLD_S = 120       # contract: a failed download is reported (fetch_job None) for 2 minutes
FETCH_STEP_MS = 5              # mock fetch job: one release per event-loop step

from . import fake_service as T
from .fake_service import (Album, AlbumFilter, AlbumPlan, AlbumRow, AlbumSort, Collision, Destination,
                           FakeService, FileChange, Job, JobKind, JobState, LIBRARY_ROOT, MatchStatus, NotFound,
                           Operation, Page, Plan, PlanStale, Problem, ProblemKind, MMError, Settings, TagChange, Template,
                           TemplatePreview, ValidationError, Cluster, ClusterFile, MatchView, LocalTrack, Candidate,
                           ScorePart, DISCOGS, SCANNED_AT, ClusterRow, Pin, ManualPair)


def P_folder_disc(path: Path) -> int | None:
    """The disc a file's CD folder names ("CD2", "Disc 2"), else None."""
    import re
    m = re.fullmatch(r"(?:cd|disc|disk)\s*(\d+)", Path(path).parent.name, re.I)
    return int(m.group(1)) if m else None

SYNTH_ROOT = Path("/mock/synthetic")   # synthetic padding rows (absolute, fake, never touched)
SYNTH_BASE = 10_000_000                # synthetic album ids start here
SYNTH_ADDED = datetime(2025, 1, 1, tzinfo=timezone.utc)   # synthetic rows were "scanned" before the mock albums

# ------------------------------------------------------------------ built-in presets
# Verbatim from API-CONTRACT.md "Built-in presets" (ids, names, texts; in templates.list() order).
# Note the U+2044 FRACTION SLASH in Edition detail: it is text, never a path separator.
BUILTINS = [
    Template("archivist", "Archivist (default)",
             "%albumartist%/[%leadyear%]  %album% [%reissueyear%] [%catno%]/%position%. %title%", True),
    Template("simple", "Simple",
             "%albumartist%/%album% (%originalyear%)/$num(%tracknumber%,2) - %title%", True),
    Template("edition-detail", "Edition detail",
             "%albumartist%/%album% (%originalyear% \u2044 %reissueyear%) [%catno% \u2022 %format%]/"
             "$num(%tracknumber%,2) %title%", True),
    Template("label-archive", "Label archive",
             "%label%/[%catno%] %albumartist% - %album% (%leadyear%)/%position%. %title%", True),
]
CONTRACT_FIELDS = {"albumartist", "album", "title", "artist", "leadyear", "originalyear", "reissueyear", "catno",
                   "catno_raw", "label", "format", "tracknumber", "position", "discnumber", "disctotal"}
MOCK_ONLY_FIELDS: set[str] = set()     # %catno_raw% became a contract field (contract sha256 98f7fe95…)
FIELDS = CONTRACT_FIELDS | MOCK_ONLY_FIELDS

# %catno%: mirrors format_catalog() / CATNO_STRIP in Bassline's discogs-tag.py (read-only reference):
# first catalog number (split on ";"), "none"/"[none]" -> empty, uppercased, U+0020 spaces and
# _ ⋅(U+22C5) / : * ? " < > | removed.  'SST 023' -> 'SST023'.
CATNO_STRIP = re.compile(r"""[_⋅/: *?"<>| ]""")   # ⋅ = U+22C5 DOT OPERATOR
FILENAME_ILLEGAL = '/\\:*?"<>|'


def _first_catalog(catno: str | None) -> str:
    """First catalog number (Discogs separates several with ';'); 'none'/'[none]' mean no number."""
    if not catno:
        return ""
    first = re.split(r";\s*", catno.strip())[0].strip()
    return "" if first.casefold() in {"", "none", "[none]"} else first


def format_catalog(catno: str | None) -> str:
    """%catno% (contract default; mirrors discogs-tag.py format_catalog): 'SST 023' -> 'SST023'."""
    return CATNO_STRIP.sub("", _first_catalog(catno)).upper()


def catno_raw(catno: str | None) -> str:
    """%catno_raw% (contract): the first catalog number exactly as Discogs lists it; only characters
    not allowed in file names are replaced with '_'. The contract doesn't list that set; the mock uses
    discogs-tag.py sanitize_filename's: / \\ : * ? " < > |"""
    text = _first_catalog(catno)
    for ch in FILENAME_ILLEGAL:
        text = text.replace(ch, "_")
    return text
FUNCTIONS = {"if", "if2", "left", "num", "lower", "upper"}
UNKNOWN_LABEL = "Unknown label"
STATUS_ORDER = [MatchStatus.NEEDS_REVIEW, MatchStatus.HELD, MatchStatus.UNMATCHED, MatchStatus.QUEUED, MatchStatus.MATCHED,
                MatchStatus.ACCEPTED, MatchStatus.SKIPPED, MatchStatus.NO_MATCH, MatchStatus.APPLIED]

# ------------------------------------------------------------------ template engine (mock)
# AST nodes: ("lit", str) | ("field", name) | ("func", name, [args: list[node]])
_EMPTY = "\x00"          # marks an empty field/function result until bracket/spacing cleanup
_DROP = "\x01"           # a bracket group that collapsed because its fields were empty
_CONNECTORS = "•\u2044,–-"   # separators inside brackets that go with an empty neighbour


def parse(text: str) -> list:
    nodes, i = _parse(text, 0, top=True)
    return nodes


def _parse(text: str, i: int, top: bool = False, in_args: bool = False):
    nodes, buf = [], []

    def flush():
        if buf:
            nodes.append(("lit", "".join(buf))); buf.clear()
    while i < len(text):
        ch = text[i]
        if ch == "%":
            m = re.compile(r"%(\w*)%").match(text, i)
            if not m:
                raise ValidationError(f"Unclosed % at position {i}", field="%")
            if m.group(1) not in FIELDS:
                raise ValidationError(f"Unknown field %{m.group(1)}%", field=m.group(1))
            flush(); nodes.append(("field", m.group(1))); i = m.end(); continue
        if ch == "$":
            m = re.compile(r"\$(\w+)\(").match(text, i)
            if not m:
                raise ValidationError(f"Malformed function at position {i}", field="$")
            name = m.group(1)
            if name not in FUNCTIONS:
                raise ValidationError(f"unknown function ${name}", field=name)   # real: bare name
            flush(); i = m.end(); args = []
            while True:
                arg, i = _parse(text, i, in_args=True)
                args.append(arg)
                if i >= len(text):
                    raise ValidationError(f"unclosed ${name}(", field=name)
                if text[i] == ")":
                    i += 1; break
                i += 1                     # ","
            nodes.append(("func", name, args)); continue
        if in_args and ch in ",)":
            break
        buf.append(ch); i += 1
    flush()
    return nodes, i


def validate(text: str) -> None:
    if not text or not text.strip():
        raise ValidationError("Template is empty", field="text")
    parse(text)
    if text.startswith("/") or any(seg.strip() in ("..", ".") for seg in text.split("/")):
        raise ValidationError("Template must stay inside the library root", field="text")


def _plain(s: str) -> str:
    return s.replace(_EMPTY, "").replace(_DROP, "")


def _eval(nodes: list, vals: dict) -> str:
    out = []
    for n in nodes:
        if n[0] == "lit":
            out.append(n[1])
        elif n[0] == "field":
            v = vals.get(n[1], "")
            out.append(str(v).replace("/", "-") if v else _EMPTY)   # values never create folders
        else:
            name, args = n[1], [_eval(a, vals) for a in n[2]]
            out.append(_call(name, args))
    return "".join(out)


def _call(name: str, a: list[str]) -> str:
    p = [_plain(x).strip() for x in a]
    if name == "if":
        r = a[1] if p[0] else (a[2] if len(a) > 2 else "")
    elif name == "if2":
        r = next((x for x, q in zip(a, p) if q), "")
    elif name == "left":
        r = p[0][:int(p[1] or 0)]
    elif name == "num":
        r = p[0].zfill(int(p[1] or 0)) if p[0].isdigit() else p[0]
    elif name == "lower":
        r = p[0].lower()
    else:  # upper
        r = p[0].upper()
    return r if _plain(r).strip() else _EMPTY


def _cleanup(seg: str) -> str:
    """An empty field drops along with its surrounding brackets and spacing."""
    def group(m):
        inner = m.group(2)
        if _EMPTY not in inner:
            return m.group(0)
        if not re.sub(rf"[\s{_CONNECTORS}]+", "", _plain(inner)):
            return _DROP
        inner = re.sub(rf"\s*[{_CONNECTORS}]\s*{_EMPTY}|{_EMPTY}\s*[{_CONNECTORS}]\s*", "", inner)
        return m.group(1) + inner.replace(_EMPTY, "").strip() + m.group(3)
    seg = re.sub(r"([\[(])([^\[\]()]*)([\])])", group, seg)
    seg = re.sub(rf"(?<=\S)\s+[{_EMPTY}{_DROP}]+", "", seg)     # drop with the spacing before it
    seg = re.sub(rf"[{_EMPTY}{_DROP}]+\s*", "", seg)             # ...or after it at the start
    return seg.strip(" -")


def render_segments(text: str, f: dict) -> list[str]:
    """Split on ASCII '/' only (U+2044 and other look-alikes are ordinary text), evaluate each
    segment; an empty %label% in a folder segment becomes 'Unknown label'."""
    raw = text.split("/")
    out = []
    for i, seg in enumerate(raw):
        vals = dict(f)
        if i < len(raw) - 1 and not vals.get("label"):
            vals["label"] = UNKNOWN_LABEL
        out.append(_cleanup(_eval(parse(seg), vals)))
    return [s for s in out if s]


@functools.total_ordering
class _Rev:
    """Reverses comparison for one key component (descending primary key)."""
    __slots__ = ("v",)

    def __init__(self, v): self.v = v
    def __eq__(self, o): return self.v == o.v
    def __lt__(self, o): return self.v > o.v


def _is_sided(cand) -> bool:
    return bool(cand) and any("Vinyl" in x or "Cassette" in x for x in cand.formats)


class _NS:
    def __init__(self, **fns):
        self.__dict__.update(fns)


# MOCK: alias keys the engine renames to their Picard name (contract "Picard tag names (2026-10-21)")
TAG_ALIASES = {"TOTALTRACKS": "TRACKTOTAL", "TOTALDISCS": "DISCTOTAL", "CATALOG": "CATALOGNUMBER"}


class FakeContract:
    """Contract facade over a FakeService instance."""

    def __init__(self, fake: FakeService | None = None, synthetic: int = 100_000):
        self.fake = fake if fake is not None else FakeService()
        self.synthetic = synthetic
        self.events = self.fake.events
        # types + errors, as the real module exposes them at top level
        self.types = _NS(**{n: getattr(T, n) for n in (
            "AlbumFilter", "AlbumSort", "MatchStatus", "JobState", "JobKind", "Job", "Page", "Template",
            "TemplatePreview", "Collision", "Destination", "Settings", "Operation", "AutoRules", "Problem", "ProblemKind", "AlbumPlan", "Plan", "MMError", "NotFound",
            "PlanStale", "ValidationError", "ProviderError", "ConfigError", "Cluster", "ClusterFile", "MatchView",
            "ClusterRow", "Pin", "ManualPair", "PinConflict", "SubtrackGroup", "TrackPair", "RemoteTrack", "TagChange", "UnwrittenField")})
        self._ids = itertools.count(100_000)
        self._jobs: dict[int, Job] = {}
        self._plans: dict[int, Plan] = {}
        self._undo_preview: dict[int, int] = {}       # operation_id -> plan_id of the latest preview
        self._ops: dict[int, Operation] = {}
        self._op_plans: dict[int, Plan] = {}
        self._user_templates: dict[str, Template] = {}
        self._settings = Settings(library_roots=[LIBRARY_ROOT, SYNTH_ROOT], active_template="archivist")
        self._syn_cache: dict[tuple, tuple[list, list[int]]] = {}
        self.library = _NS(albums=self._albums, status_counts=self._status_counts, album=self._album,
                           search=self._search, scan=self._scan, clusters=self._clusters_list,
                           cluster=self._cluster, adopt_cluster=self._adopt_cluster,
                           unadopt_cluster=self._unadopt_cluster, move_to_cluster=self._move_to_cluster,
                           scan_clusters=self._scan_clusters)
        self._moved: dict[Path, str] = {}               # MOCK: move_to_cluster targets (group key)
        self._pins: dict[int, dict[Path, Pin]] = {}     # album -> file -> pin
        self._manual_ops: dict[int, list] = {}          # apply operation -> ManualPair (journal.manual_pairs)
        self._clusters: dict[str, Cluster] = {}         # the last clusters() call + adopted ones
        self._pending_cands: dict[int, list] = {}       # adopted cluster -> candidates match.find will "find"
        self.match = _NS(view=self._view, clusters=self._match_clusters, pin=self._pin, unpin=self._unpin,
                         pins=self._pins_of, find=self._find, candidates=self._candidates, choose=self.fake.choose,
                         destination=self._destination, skip=self.fake.skip, manual=self._manual,
                         auto_rules=lambda: T.AutoRules(), auto_accept=self._auto_accept,
                         split_subtracks=self._split_subtracks, unsplit_subtracks=self._unsplit_subtracks,
                         subtrack_splits=self._subtrack_splits)
        self._splits: dict[int, set[tuple[int, str]]] = {}   # album -> {(release id, base)} (2026-10-21)
        self.provider = _NS(submit_link=self.fake.submit_link)
        self.plan = _NS(build=self._build, apply=self._apply)
        self.journal = _NS(manual_pairs=self._manual_pairs, list=self._journal_list, undo_plan=self._undo_plan, undo=self._undo)
        self.templates = _NS(list=self._tpl_list, save=self._tpl_save, delete=self._tpl_delete,
                             preview=self._tpl_preview)
        self.settings = _NS(get=lambda: self._settings, set=self._settings_set,
                            verify_hashes_available=lambda: ["sha256"])  # MOCK: no optional xxhash
        self.jobs = _NS(get=self._job_get, cancel=lambda job_id: None, pause=lambda job_id: None,
                         resume=lambda job_id: None, wait=self._job_wait, idle=self._jobs_idle)
        self._announced: set[int] = set()     # finished jobs whose job_finished has been emitted
        # MOCK: extra problems per album id, added by plan.build and match.destination (UI work only)
        self.mock_problems: dict[int, list[Problem]] = {}
        # MOCK (contract "Picard tag names (2026-10-21)"): keys an album's files already carry ({"TOTALTRACKS": "3"});
        # aliases are renamed to the Picard key, a Vorbis YEAR is removed only with Settings.migrate_tag_keys
        self.mock_tag_keys: dict[int, dict[str, str]] = {}
        # MOCK: AlbumPlan.unwritten per album (also a Plan.warnings line each); empty for held albums
        self.mock_unwritten: dict[int, list] = {}
        # MOCK: files edited since their apply -> FILE_CHANGED on undo, that step is skipped
        self.mock_changed: dict[Path, str] = {}
        # Contract "Release fetching in plan.build": build never fetches. An unfetched or stale (> 6 h) chosen
        # release gives RELEASE_NOT_FETCHED with files == [] and a FETCH_CANDIDATES job in Plan.fetch_job.
        self.simulate_fetch = True             # False: mock releases count as stored (old mock behaviour)
        self.clock = time.time                 # tests move it to make stored releases stale
        self.fetched_at: dict[tuple[int, int], float] = {}   # set by fetch jobs; untracked releases are fresh
        self.mock_fetch_fail: set[int] = set()  # MOCK: release ids whose download fails
        self._fetch_errors: dict[tuple[int, int], tuple[float, str]] = {}   # reported for 2 minutes
        self._fetching: dict[tuple[int, int], int] = {}
        # Bassline's coming changes (handled by the UI either way; mock knobs so tests cover both):
        # - plan.apply raises MMError right away when nothing in the plan is applicable, holding the albums
        #   with problems (a06a7c9; 0666aad ran a Job that ended FAILED with "Applied 0 album(s); refused N …")
        self.apply_raises_when_nothing_applicable = False
        # - MOCK: album ids whose files change on disk after plan.build: apply refuses them during the run
        #   (FILE_CHANGED, status HELD); if that refuses every album the job ends FAILED
        self.mock_changed_before_apply: set[int] = set()
        # - undo FileChange.kind for track files: "extra" at 0666aad, "track" after the fix
        self.undo_track_kind = "extra"
        self._seed_journal()
        self._seed_problems()

    # ================================================================ library
    @staticmethod
    def _synth_fields(i: int) -> dict:
        artist = "Mock Artist %05d" % (i // 10)
        return dict(id=SYNTH_BASE + i, folder=SYNTH_ROOT / str(i), artist=artist, title=f"Mock Album {i}",
                    year=str(1960 + i % 60), catno="", track_count=8 + i % 9, status=MatchStatus.UNMATCHED,
                    confidence=None, artwork=None, artist_sort=artist,
                    added_at=SYNTH_ADDED + timedelta(minutes=i), held_for=[])

    def _synth_row(self, i: int) -> AlbumRow:
        return AlbumRow(**self._synth_fields(i))

    def _pad(self, f: AlbumFilter) -> int:
        """Synthetic rows appear in unfiltered / 'not matched yet' views without search text."""
        return self.synthetic if (f.status is None or MatchStatus.UNMATCHED in f.status) and not f.text else 0

    @staticmethod
    def _key(sort: AlbumSort, desc: bool, r: dict):
        """AlbumSort semantics; descending reverses the primary key only, folder tiebreak stays ascending."""
        rev = (lambda v: _Rev(v)) if desc else (lambda v: v)
        folder, year = str(r["folder"]), r["year"] or "9999"
        if sort is AlbumSort.ARTIST:        # artist_sort, then lead year, then title
            return (rev(r["artist_sort"].casefold()), year, r["title"].casefold(), folder)
        if sort is AlbumSort.TITLE:
            return (rev(r["title"].casefold()), folder)
        if sort is AlbumSort.YEAR:          # lead year, then artist
            return (rev(year), r["artist_sort"].casefold(), folder)
        if sort is AlbumSort.ADDED:         # newest scan first
            return (_Rev(r["added_at"]) if not desc else r["added_at"], folder)
        if sort is AlbumSort.CONFIDENCE:    # lowest first; unmatched (None) last in both directions
            c = r["confidence"]
            return (c is None, rev(c or 0.0), folder)
        if sort is AlbumSort.STATUS:
            return (rev(STATUS_ORDER.index(r["status"])), folder)
        return (rev(folder), folder)         # FOLDER

    @staticmethod
    def _parse_sort(sort) -> AlbumSort:
        try:
            return sort if isinstance(sort, AlbumSort) else AlbumSort(str(sort))
        except ValueError:
            raise ValidationError(f"Unknown sort key {sort!r}", field="sort") from None

    def _row_key(self, a: AlbumRow, sort, desc):
        return self._key(sort, desc, a.__dict__)

    def _synthetic_order(self, sort: AlbumSort, desc: bool):
        ck = (sort, desc, self.synthetic)
        if ck not in self._syn_cache:
            keyed = sorted((self._key(sort, desc, self._synth_fields(i)), i) for i in range(self.synthetic))
            self._syn_cache[ck] = ([k for k, _ in keyed], [i for _, i in keyed])
        return self._syn_cache[ck]

    def _albums(self, filter: AlbumFilter, sort: AlbumSort, offset: int, limit: int,
                descending: bool = False) -> Page:
        sort_key = self._parse_sort(sort)
        real = sorted(self.fake.albums(filter), key=lambda a: self._row_key(a, sort_key, descending))
        n_syn = self._pad(filter)
        syn_keys, syn_idx = self._synthetic_order(sort_key, descending) if n_syn else ([], [])
        # merged position of each real row among the synthetic ones
        pos = {bisect.bisect_left(syn_keys, self._row_key(a, sort_key, descending)) + j: a
               for j, a in enumerate(real)}
        real_positions = sorted(pos)
        total = len(real) + n_syn
        items = []
        for m in range(max(0, offset), min(total, offset + max(0, limit))):
            if m in pos:
                items.append(pos[m])
            else:
                items.append(self._synth_row(syn_idx[m - bisect.bisect_left(real_positions, m)]))
        return Page(items, total, offset)

    def _status_counts(self, filter: AlbumFilter | None = None) -> dict[MatchStatus, int]:
        f = replace(filter, status=None) if filter is not None else AlbumFilter()
        out = {s: 0 for s in MatchStatus}
        for a in self.fake.albums(f):
            out[a.status] += 1
        out[MatchStatus.UNMATCHED] += self._pad(f)
        return out

    def _album(self, album_id: int) -> Album:
        if album_id >= SYNTH_BASE:
            if album_id - SYNTH_BASE >= self.synthetic:
                raise NotFound(f"album {album_id}")
            return Album(**self._synth_fields(album_id - SYNTH_BASE), tracks=[], extras=[])
        try:
            return self.fake.album(album_id)
        except KeyError:
            raise NotFound(f"album {album_id}") from None

    def _candidates(self, album_id: int):
        return [] if album_id >= SYNTH_BASE else self.fake.candidates(album_id)

    def _search(self, query: str, limit: int = 200):
        return list(self._albums(AlbumFilter(text=query), AlbumSort.ARTIST, 0, limit).items)

    # ================================================================ jobs
    def _finish(self, kind: JobKind, work, message: str = "") -> Job:
        """Return a QUEUED job now, run `work` on the next event-loop turn, then emit job_finished."""
        job = Job(next(self._ids), kind, JobState.QUEUED, 0, 1, message)
        self._jobs[job.id] = job

        def run():
            try:
                msg = work() or message
                done = replace(job, state=JobState.DONE, done=1, message=msg)
            except Exception as e:  # contract: job failures carry the message in Job.message
                done = replace(job, state=JobState.FAILED, message=f"{type(e).__name__}: {e}")
            self._jobs[job.id] = done
            self.events.job_progress.emit(done)
            self.events.job_finished.emit(done)
            self._announced.add(job.id)        # jobs.wait returns only after this point
        QTimer.singleShot(0, self.fake, run)   # on the fake's (UI) thread, even from a worker
        return job

    def _job_wait(self, job_id: int, timeout: float | None = 30.0) -> Job:
        """Real service extra (deviation 12; 0666aad): block until the job is final AND its job_finished
        was emitted. The fake runs on the Qt event loop, so this pumps events while it waits."""
        from PySide6.QtCore import QCoreApplication, QEventLoop
        self._job_get(job_id)
        deadline = None if timeout is None else time.monotonic() + timeout
        while job_id not in self._announced:
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError(f"job {job_id} still {self._jobs[job_id].state.name}")
            QCoreApplication.processEvents(QEventLoop.AllEvents, 20)
            time.sleep(0.001)
        return self._jobs[job_id]

    def _jobs_idle(self, timeout: float = 30.0) -> None:
        for jid in list(self._jobs):
            self._job_wait(jid, timeout)

    def _job_get(self, job_id: int) -> Job:
        try:
            return self._jobs[job_id]
        except KeyError:
            raise NotFound(f"job {job_id}") from None

    def _scan(self, roots=None) -> Job:
        return self._finish(JobKind.SCAN, lambda: "mock: scan is a no-op")

    def _find(self, album_ids) -> Job:
        for aid in [a for a in album_ids if a in self._pending_cands]:     # an adopted cluster: search now
            self.fake._cands[aid] = self._pending_cands.pop(aid)
            self.fake._fetch_queue += [(aid, c.release_id) for c in self.fake._cands[aid][:1]]
            self.fake._set(aid, status=MatchStatus.NEEDS_REVIEW, confidence=self.fake._cands[aid][0].score)
            self.fake._timer.start()
        return self._finish(JobKind.MATCH, lambda: "mock: candidates are pre-seeded")

    # ================================================================ clustering (c334bbf, c2fcf25)
    @staticmethod
    def _cluster_key(paths, prefix: str = "cl-") -> str:
        import hashlib
        return prefix + hashlib.sha1("\n".join(sorted(map(str, paths))).encode()).hexdigest()[:12]

    def _group_of(self, path: Path) -> str:
        """Clustering key of a loose file: its album tag, or where move_to_cluster put it ("\0un" = out)."""
        return self._moved.get(path, self.fake._file_tags.get(path, ("", ""))[1].casefold())

    def _cluster_of(self, tracks, folder: Path, album_id=None, kind: str = "tag_cluster") -> Cluster:
        tags = self.fake._file_tags
        files = [ClusterFile(t.path, t.disc or 1, t.number, t.title, t.duration_s, *reversed(tags.get(t.path, ("", ""))))
                 for t in sorted(tracks, key=lambda t: str(t.path))]
        if kind == "unclustered":
            return Cluster(self._cluster_key([f.path for f in files], "un-"), "", "", folder, files,
                           len({f.disc for f in files}), None, kind)
        common = lambda xs, d: max(set(xs), key=xs.count) if any(xs) else d   # noqa: E731
        return Cluster(self._cluster_key([f.path for f in files]), common([f.artist for f in files if f.artist], folder.name),
                       common([f.album for f in files if f.album], folder.name), folder, files,
                       len({f.disc for f in files}), album_id, kind)

    def _adopted_cluster(self, a) -> Cluster:
        c = self._clusters.get(a.cluster_id)
        made = self._cluster_of(a.tracks, a.folder, a.id)
        # the files may have changed (move_to_cluster): the id follows them, the name stays (Picard behaviour)
        c = replace(made, artist=c.artist, title=c.title) if c is not None else made
        if c.id != a.cluster_id:
            self.fake._albums[a.id] = replace(a, cluster_id=c.id)
        return c

    def _clusters_list(self, paths=None) -> list[Cluster]:
        """MOCK of library.clusters: loose-tracks albums' files grouped by album tag (or a move_to_cluster),
        folder by folder, files moved out of every cluster as kind "unclustered", plus every adopted cluster."""
        albums = list(self.fake._albums.values())
        if paths is not None:
            by_path = {t.path: t for a in albums for t in a.tracks}
            if not paths or any(Path(str(x)) not in by_path for x in paths):
                raise ValidationError("paths: audio files inside a library root", field="paths")
            tracks = [by_path[Path(str(x))] for x in paths]
            parents = {t.path.parent for t in tracks}
            out = [self._cluster_of(tracks, parents.pop() if len(parents) == 1 else LIBRARY_ROOT)]
        else:
            out = []
            for a in albums:
                if getattr(a, "loose_tracks", False):
                    groups: dict[str, list] = {}
                    for t in a.tracks:
                        groups.setdefault(self._group_of(t.path), []).append(t)
                    out += [self._cluster_of(g, a.folder, kind="unclustered" if k == "\0un" else "tag_cluster")
                            for k, g in groups.items()]
                elif getattr(a, "cluster_id", None):
                    out.append(self._adopted_cluster(self.fake._albums[a.id]))
        self._clusters.update({c.id: c for c in out})
        return sorted(out, key=lambda c: (str(c.folder), str(c.files[0].path) if c.files else ""))

    def _cluster(self, cluster_id: str) -> Cluster:
        if cluster_id not in self._clusters:
            self._clusters_list()
        try:
            return self._clusters[cluster_id]
        except KeyError:
            raise NotFound(f"cluster {cluster_id}") from None

    def _scan_clusters(self) -> Job:
        def work():
            cs = self._clusters_list()
            self.fake.events.clusters_changed.emit([c.id for c in cs])
            return f"{len(cs)} cluster(s), {sum(len(c.files) for c in cs)} file(s)"
        return self._finish(JobKind.CLUSTER, work)

    def _loose_row(self, folder: Path):
        return next((a for a in self.fake._albums.values() if getattr(a, "loose_tracks", False) and a.folder == folder), None)

    def _adopt_cluster(self, cluster_id: str) -> AlbumRow:
        """MOCK of library.adopt_cluster: index only. The cluster becomes an UNMATCHED album holding exactly its
        files (no sidecars or cover art); they leave their loose-tracks row. Idempotent."""
        c = self._cluster(cluster_id)
        if c.kind == "unclustered":
            raise ValidationError("unclustered files can't be adopted: move them into a cluster", field="cluster_id")
        if c.album_id is not None:
            return self.fake.album(c.album_id)
        fk = self.fake
        mine = {f.path for f in c.files}
        owner = next((a for a in fk._albums.values() if mine & {t.path for t in a.tracks}), None)
        tracks = [t for t in (owner.tracks if owner else []) if t.path in mine]
        aid = next(fk._ids)
        fk._albums[aid] = Album(aid, c.folder, c.artist, c.title, "", "", len(tracks), MatchStatus.UNMATCHED, None,
                                None, c.artist, SCANNED_AT + timedelta(minutes=aid), [], cluster_id=c.id,
                                tracks=tracks, extras=[])
        parts = [ScorePart("album", .40, 1.0, c.title, c.title, True), ScorePart("artist", .35, 1.0, c.artist, c.artist, True)]
        fk._cands[aid] = []
        self._pending_cands[aid] = [Candidate(next(fk._rid), None, c.artist, c.title, "1987", "US", "Mock Label", "X-1",
                                              ["CD"], 0.91, None, parts, None, None, None, None, False, DISCOGS,
                                              auto_score=0.91)]
        self._clusters[c.id] = replace(c, album_id=aid)
        if owner is not None:
            rest = [t for t in owner.tracks if t.path not in mine]
            fk._set(owner.id, tracks=rest, track_count=len(rest))
        fk.events.album_changed.emit(aid)
        fk.events.clusters_changed.emit([c.id])
        return fk._albums[aid]

    def _unadopt_cluster(self, album_id: int) -> Cluster:
        fk = self.fake
        a = self._album(album_id)
        if not getattr(a, "cluster_id", None):
            raise ValidationError(f"album {album_id} is not an adopted cluster", field="album_id")
        if any(album_id in [ap.album_id for ap in pl.albums] for o, pl in self._op_plans.items()
               if self._ops[o].kind == "apply"):
            op = next(o for o, pl in self._op_plans.items() if album_id in [ap.album_id for ap in pl.albums])
            raise MMError(f"{a.artist} – {a.title} was applied (operation {op}): undo that in History")
        owner = self._loose_row(a.folder)
        del fk._albums[album_id]
        for d in (fk._cands, self._pending_cands, fk._chosen, self._pins):
            d.pop(album_id, None)
        self._clusters.pop(a.cluster_id, None)
        fk.events.album_removed.emit(album_id)                 # 1f7b340: removed, then the loose row changed
        if owner is not None:
            fk._set(owner.id, tracks=owner.tracks + list(a.tracks), track_count=owner.track_count + len(a.tracks))
        c = self._cluster_of(a.tracks, a.folder)
        self._clusters[c.id] = c
        fk.events.clusters_changed.emit([c.id])
        return c

    def _move_to_cluster(self, file, cluster_id) -> Cluster:
        """MOCK of library.move_to_cluster: index only; both clusters get new ids (they hash their files)."""
        fk, path = self.fake, Path(file)
        self._clusters_list()
        src = next((c for c in self._clusters.values() if path in [f.path for f in c.files]
                    and (c.album_id is None or c.album_id in fk._albums)), None)
        if src is None:
            raise ValidationError(f"{path} is not a loose file", field="file")
        if cluster_id is None:
            dst_key, dst = "\0un", None
        else:
            dst = self._cluster(cluster_id)
            if dst.folder != src.folder:
                raise ValidationError("the target cluster is in another folder", field="cluster_id")
            dst_key = ("\0un" if dst.kind == "unclustered" else
                       self._group_of(dst.files[0].path) if dst.files else dst.title.casefold())
        # leaving an adopted album: back to the loose row
        if src.album_id is not None:
            a = fk._albums[src.album_id]
            t = next(t for t in a.tracks if t.path == path)
            rest = [x for x in a.tracks if x.path != path]
            self._pins.get(a.id, {}).pop(path, None)
            if rest:
                fk._albums[a.id] = replace(a, tracks=rest, track_count=len(rest))
            else:                                             # an adopted cluster left empty is deleted
                del fk._albums[a.id]
                fk.events.album_removed.emit(a.id)
            owner = self._loose_row(src.folder)
            if owner is not None:
                fk._albums[owner.id] = replace(owner, tracks=owner.tracks + [t], track_count=owner.track_count + 1)
        self._moved[path] = dst_key
        if dst is not None and dst.album_id is not None:          # into an adopted album
            owner = self._loose_row(src.folder)
            t = next(t for t in owner.tracks if t.path == path)
            fk._albums[owner.id] = replace(owner, tracks=[x for x in owner.tracks if x.path != path],
                                           track_count=owner.track_count - 1)
            a = fk._albums[dst.album_id]
            fk._albums[a.id] = replace(a, tracks=a.tracks + [t], track_count=a.track_count + 1)
        before = {c.id for c in self._clusters.values()}
        now = self._clusters_list()
        new_dst = next(c for c in now if path in [f.path for f in c.files])
        left = next((c for c in now if c.id not in before and c.id != new_dst.id), None)
        for aid in {x for x in (src.album_id, dst.album_id if dst else None) if x is not None and x in fk._albums}:
            fk.events.album_changed.emit(aid)
        fk.events.cluster_changed.emit(cluster_id or "", new_dst.id)
        fk.events.cluster_changed.emit(src.id, left.id if left else "")
        return new_dst

    def _match_clusters(self, filter=None, offset: int = 0, limit: int = 200) -> Page:
        """MOCK of match.clusters: folder albums (the mock's own, not the synthetic padding), tag clusters
        and unclustered files in one list; filter.status / max_confidence leave out clusters not adopted."""
        f = filter or AlbumFilter()
        text = (f.text or "").strip().casefold()
        rows = []
        for a in self.fake._albums.values():
            if getattr(a, "loose_tracks", False):
                continue
            if f.status is not None and a.status not in set(f.status):
                continue
            if f.max_confidence is not None and (a.confidence is None or a.confidence >= f.max_confidence):
                continue
            if text and text not in f"{a.artist} {a.title} {' '.join(t.title for t in a.tracks)}".casefold():
                continue
            cid = getattr(a, "cluster_id", None)
            rows.append(ClusterRow(cid or f"album-{a.id}", "tag_cluster" if cid else "folder", a.artist, a.title,
                                   a.track_count, a.status, a.id, bool(cid), a.folder))
        if f.status is None and f.max_confidence is None:
            for c in self._clusters_list():
                if c.album_id is not None:
                    continue
                if text and text not in f"{c.artist} {c.title} {' '.join(x.title for x in c.files)}".casefold():
                    continue
                rows.append(ClusterRow(c.id, c.kind, c.artist, c.title, len(c.files), None, None, True, c.folder))
        rows.sort(key=lambda r: (r.kind == "unclustered", r.artist.casefold(), r.title.casefold(),
                                 str(r.folder).casefold(), r.id))
        return Page(rows[offset:offset + limit], len(rows), offset)

    # ---------------------------------------------------------------- pins (c2fcf25)
    def _release_for(self, album_id: int, release_id=None):
        cands = self._candidates(album_id)
        if release_id is not None:
            if release_id not in [c.release_id for c in cands]:
                raise NotFound(f"release {release_id} is not a candidate of album {album_id}")
            return release_id
        chosen = self.fake._chosen.get(album_id)
        return chosen if chosen in [c.release_id for c in cands] else (cands[0].release_id if cands else None)

    def _pin(self, album_id: int, file, position: str = "", override_hold: bool = False, release_id=None,
             positions=None) -> Pin:
        a = self._album(album_id)
        path = Path(file)
        if path not in {t.path for t in a.tracks}:
            raise ValidationError(f"{path} is not a file of album {album_id}", field="file")
        group: list[str] = []
        if positions is not None:                     # 2026-10-21: a subtrack group pin
            if str(position or "").strip():
                raise ValidationError("give position or positions, not both", field="positions")
            group = [str(x).strip() for x in positions]
            if len(group) < 2 or not all(group) or any("+" in x for x in group):
                raise ValidationError("positions needs two or more release positions", field="positions")
            position = "+".join(group)
        if not str(position or "").strip():
            raise ValidationError("position is empty", field="position")
        rid = self._release_for(album_id, release_id)
        if rid is None:
            raise MMError(f"album {album_id} has no candidates: run match.find first")
        pin = Pin(path, str(position).strip(), rid, bool(override_hold), False, group or [str(position).strip()])
        self._pins.setdefault(album_id, {})[path] = pin
        self.fake.events.match_view_changed.emit(album_id)
        return pin

    def _unpin(self, album_id: int, file=None) -> None:
        self._album(album_id)
        if file is None:
            self._pins.pop(album_id, None)
        else:
            self._pins.get(album_id, {}).pop(Path(file), None)
        self.fake.events.match_view_changed.emit(album_id)

    def _pins_of(self, album_id: int) -> list:
        self._album(album_id)
        return sorted(self._pins.get(album_id, {}).values(), key=lambda p: str(p.path))

    def _held_files(self, album, cand) -> set:
        return {x for p in self._disc_ambiguous(album, cand) + self._disc_track_count(album, cand) for x in p.paths}

    def _apply_pins(self, album, cand, pairs):
        """Engine rule (c2fcf25 _pins.resolve) in small: pins go first, the rest keep their pairs; unusable
        pins are PIN_CONFLICT problems. Returns (pairs, problems, usable {path: position})."""
        pins = [p for p in self._pins.get(album.id, {}).values() if p.release_id == cand.release_id]
        if not pins:
            return pairs, [], {}
        remotes = {pr.remote.position: pr.remote for pr in pairs if pr.remote is not None}
        held = self._held_files(album, cand)
        files = {t.path for t in album.tracks}
        problems, wanted = [], {}
        refuse = lambda pin, reason, why: problems.append(Problem(   # noqa: E731
            ProblemKind.PIN_CONFLICT, f"Manual pair {pin.path.name} -> {pin.position}: {why}", [pin.path],
            pin_conflicts=[T.PinConflict(pin.path, pin.position, reason)]))
        for pin in pins:
            if pin.stale or pin.path not in files:
                refuse(pin, "stale", "the file changed since it was paired; pair it again")
            elif pin.position not in remotes:
                refuse(pin, "unknown_position", f"release {cand.release_id} has no track {pin.position}")
            else:
                wanted.setdefault(pin.position, []).append(pin)
        usable = {}
        for pos, group in wanted.items():
            if len(group) > 1:
                for pin in group:
                    refuse(pin, "duplicate_position", f"{len(group)} files are paired with track {pos}")
                continue
            pin = group[0]
            fd = P_folder_disc(pin.path)
            if not pin.override_hold and fd is not None and fd != remotes[pos].disc:
                refuse(pin, "disc_folder_mismatch", "the file sits in another disc's folder")
                continue
            if not pin.override_hold and pin.path in held:
                refuse(pin, "override_hold_required", "this overrides a disc hold; pair it again with override_hold=True")
                continue
            usable[pin.path] = pos
        if not usable:
            return pairs, problems, {}
        locals_ = {t.path: t for t in album.tracks}
        taken = set(usable.values())
        out = []
        for pr in pairs:
            lp = pr.local.path if pr.local is not None else None
            if lp in usable:
                continue
            if pr.remote is not None and pr.remote.position in taken:
                if pr.local is not None:
                    out.append(T.TrackPair("unmatched", pr.local, None, 0.0, None, False))
                continue
            out.append(pr)
        for path, pos in usable.items():
            rt = remotes[pos]
            sim = 1.0 if locals_[path].title.casefold() == rt.title.casefold() else 0.5
            delta = (None if locals_[path].duration_s is None or rt.duration_s is None
                     else locals_[path].duration_s - rt.duration_s)
            out.append(T.TrackPair("manual", locals_[path], rt, sim, delta, True))
        for pos, rt in remotes.items():          # tracks a pin took a file away from stay listed
            if not any(x.remote is not None and x.remote.position == pos for x in out):
                out.append(T.TrackPair("unmatched", None, rt, 0.0, None, False))
        order = list(remotes)
        out.sort(key=lambda x: (x.remote is None, order.index(x.remote.position) if x.remote else 0))
        return out, problems, usable

    def _view(self, album_or_cluster_id, release_id=None) -> MatchView:
        """MOCK of match.view: read-only, never fetches; takes an album id, "album-<id>" or a cluster id."""
        cid = None
        if isinstance(album_or_cluster_id, str) and album_or_cluster_id.startswith("album-"):
            album_or_cluster_id = int(album_or_cluster_id[6:])
        if isinstance(album_or_cluster_id, str):
            c = self._cluster(album_or_cluster_id)
            cid = c.id
            if c.album_id is None:
                files = [LocalTrack(0, f.path, f.disc, f.number, f.title, f.duration_s) for f in c.files]
                return MatchView(None, cid, None, False, [], [], files, [], [])
            album_or_cluster_id = c.album_id
        aid = int(album_or_cluster_id)
        a = self._album(aid)
        cid = cid or getattr(a, "cluster_id", None)
        cands = self._candidates(aid)
        rid = self._release_for(aid, release_id)
        cand = next((x for x in cands if x.release_id == rid), None)
        if cand is None:
            return MatchView(aid, cid, None, False, cands, [], list(a.tracks), [], [])
        pins = [p for p in self._pins_of(aid) if p.release_id == rid]
        if rid in self.fake._subtrack_releases:
            return self._subtrack_view(a, cid, cands, cand, pins)
        if not cand.fetched or not cand.alignment:
            return MatchView(aid, cid, rid, False, cands, [], list(a.tracks), [], [], pins)
        pairs, pin_problems, _ = self._apply_pins(a, cand, list(cand.alignment))
        try:
            problems = list(self._destination(aid, rid, None).problems)
        except MMError:
            problems = []
        problems += [p for p in pin_problems if p not in problems]
        return MatchView(aid, cid, rid, True, cands, pairs,
                         [x.local for x in pairs if x.local is not None and x.remote is None],
                         [x.remote for x in pairs if x.remote is not None and x.local is None], problems, pins)

    # ---------------------------------------------------------------- subtracks (2026-10-21)
    @staticmethod
    def _split_base(base_position) -> str:
        base = str(base_position if base_position is not None else "").strip()
        if not base:
            raise ValidationError("base_position is empty", field="base_position")
        return base

    def _group_bases(self, release_id: int) -> list[str]:
        """The release's subtrack group bases, as the release spells them ("2-9")."""
        out: list[str] = []
        for pos, _t, _s in self.fake._subtrack_releases.get(release_id, []):
            d, n, suf = self._sub_key(pos)
            if suf and f"{d}-{n}" not in out:
                out.append(f"{d}-{n}")
        return out

    def _split_subtracks(self, album_id: int, base_position: str, release_id=None) -> None:
        """MOCK of match.split_subtracks (3d31a39): base_position must name one of the release's subtrack groups
        (case-insensitive, trimmed; stored as the release spells it), else ValidationError("base_position"),
        also when empty or the release isn't fetched. NotFound: the album or a release that isn't its candidate."""
        self._album(album_id)
        want = self._split_base(base_position)
        rid = self._release_for(album_id, release_id)        # NotFound: not a candidate
        if rid is None:
            raise MMError(f"album {album_id} has no candidates: run match.find first")
        cand = next(c for c in self._candidates(album_id) if c.release_id == rid)
        if not cand.fetched:
            raise ValidationError(f"release {rid} is not fetched yet: its subtrack groups are unknown",
                                  field="base_position")
        groups = self._group_bases(rid)
        base = next((g for g in groups if g.casefold() == want.casefold()), None)
        if base is None:
            raise ValidationError(f"release {rid} has no subtrack group {want!r} (groups: {', '.join(groups) or 'none'})",
                                  field="base_position")
        self._splits.setdefault(album_id, set()).add((rid, base))
        self.fake.events.match_view_changed.emit(album_id)

    def _unsplit_subtracks(self, album_id: int, base_position=None, release_id=None) -> None:
        """MOCK of match.unsplit_subtracks: one group (case-insensitive) or all (None); an empty base is a
        ValidationError."""
        self._album(album_id)
        want = None if base_position is None else self._split_base(base_position).casefold()
        keep = {(r, b) for r, b in self._splits.get(album_id, set())
                if not ((want is None or b.casefold() == want)
                        and (release_id is None or r == int(release_id)))}
        self._splits[album_id] = keep
        self.fake.events.match_view_changed.emit(album_id)

    def _subtrack_splits(self, album_id: int, release_id=None) -> list[str]:
        self._album(album_id)
        return sorted({b for r, b in self._splits.get(album_id, set()) if release_id is None or r == int(release_id)})

    def _albums_with_subtracks(self) -> list[int]:
        """Albums whose view merge_subtracks can change: a fetched candidate release with subtrack groups."""
        return sorted(aid for aid, cands in self.fake._cands.items()
                      if any(c.fetched and self._group_bases(c.release_id) for c in cands))

    @staticmethod
    def _sub_key(position: str) -> tuple[int, int, str]:
        d, _, rest = position.partition("-")
        m = re.match(r"(\d+)([a-z]*)$", rest)
        return int(d), int(m.group(1)), m.group(2)

    def _subtrack_view(self, album, cid, cands, cand, pins) -> MatchView:
        """MOCK of match.view with match.collapse_subtracks for a release with subtracks: groups are runs of
        lettered positions sharing a base; files pair on (disc, number); a disc whose file and track counts
        still differ is held (DISC_TRACK_COUNT) and left unpaired."""
        rid, artist = cand.release_id, cand.artist
        raw = self.fake._subtrack_releases[rid]
        groups: dict[str, list] = {}
        for pos, title, secs in raw:
            d, n, suf = self._sub_key(pos)
            if suf:
                groups.setdefault(f"{d}-{n}", []).append((pos, title, secs))
        files = sorted(album.tracks, key=lambda t: (t.disc, t.number or 0))
        problems, group_pin = [], {}
        def conflict(pin, reason, why):
            problems.append(Problem(ProblemKind.PIN_CONFLICT, f"Manual pair {pin.path.name} -> {pin.position}: {why}",
                                    [pin.path], pin_conflicts=[T.PinConflict(pin.path, pin.position, reason)]))
        single = []
        for pin in pins:
            if len(pin.positions) > 1:
                base = next((b for b, g in groups.items() if [x[0] for x in g] == pin.positions), None)
                if base is None:
                    conflict(pin, "not_subtrack_group", "these positions are not one subtrack group of the release")
                else:
                    group_pin[base] = pin
            else:
                single.append(pin)
        splits = {b for r, b in self._splits.get(album.id, set()) if r == rid}
        n_files = {d: sum(1 for t in files if t.disc == d) for d in {t.disc for t in files}}
        n_tracks: dict[int, int] = {}
        for pos, _t, _s in raw:
            n_tracks[self._sub_key(pos)[0]] = n_tracks.get(self._sub_key(pos)[0], 0) + 1
        decided: dict[str, tuple[bool, str]] = {}
        for base, g in groups.items():
            d = self._sub_key(g[0][0])[0]
            if base in group_pin:
                decided[base] = (True, "pinned")
            elif not self._settings.merge_subtracks:
                decided[base] = (False, "merge_off")
            elif base in splits:
                decided[base] = (False, "split")
            elif n_files.get(d, 0) == n_tracks.get(d, 0):
                decided[base] = (False, "not_needed")
            elif n_tracks[d] - sum(len(x) - 1 for x in groups.values() if self._sub_key(x[0][0])[0] == d) \
                    != n_files.get(d, 0):
                decided[base] = (False, "ambiguous")
            elif any(s is None for _p, _t, s in g):
                decided[base] = (False, "durations_unknown")
            else:
                _d, num, _ = self._sub_key(g[0][0])
                f = next((t for t in files if t.disc == d and t.number == num), None)
                total = sum(s for _p, _t, s in g)
                fits = f is not None and f.duration_s is not None and abs(f.duration_s - total) <= max(5.0, .03 * total)
                decided[base] = (True, "durations_fit") if fits else (False, "durations_differ")
        remotes: list = []
        seen = set()
        for pos, title, secs in raw:
            d, n, suf = self._sub_key(pos)
            base = f"{d}-{n}"
            if suf and decided.get(base, (False, ""))[0]:
                if base in seen:
                    continue
                seen.add(base)
                g = groups[base]
                dur = None if any(s is None for _p, _t, s in g) else sum(s for _p, _t, s in g)
                remotes.append(T.RemoteTrack(base, d, n, " / ".join(t for _p, t, _s in g), artist, dur,
                                             [p for p, _t, _s in g]))
            else:
                remotes.append(T.RemoteTrack(pos, d, n, title, artist, secs))
        by_pos = {rt.position: rt for rt in remotes}
        held_discs = set()
        for d in sorted(n_tracks):
            disc_files = [t.path for t in files if t.disc == d]
            m = sum(1 for rt in remotes if rt.disc == d)
            if len(disc_files) != m:
                held_discs.add(d)
                msg = T.disc_count_message(album.folder, d, disc_files, m)
                hint = [g for b, g in groups.items() if self._sub_key(g[0][0])[0] == d and not decided[b][0]]
                for g in hint:
                    msg += (f". Subtracks {' + '.join(p for p, _t, _s in g)} may be one file "
                            f"({' / '.join(t for _p, t, _s in g)}): pin it to the group")
                problems.append(Problem(ProblemKind.DISC_TRACK_COUNT, msg, disc_files))
        pinned_to: dict[Path, tuple] = {}
        for base, pin in group_pin.items():
            pinned_to[pin.path] = ("group", by_pos[base])
        for pin in single:
            rt = by_pos.get(pin.position)
            if rt is None:
                conflict(pin, "unknown_position", f"release {rid} has no track {pin.position}")
            elif rt.disc in held_discs and not pin.override_hold:
                conflict(pin, "override_hold_required",
                         "this overrides a disc hold; pair it again with override_hold=True")
            else:
                pinned_to[pin.path] = ("manual", rt)
        taken = {id(v[1]) for v in pinned_to.values()}
        pairs = []
        used_files = set(pinned_to)
        for rt in remotes:
            hit = next((p for p, v in pinned_to.items() if v[1] is rt), None)
            if hit is not None:
                lt = next(t for t in files if t.path == hit)
                how = "subtracks" if rt.subtracks else "manual"
                pairs.append(T.TrackPair(how, lt, rt, 1.0 if how == "subtracks" else 0.5,
                                         None if rt.duration_s is None else lt.duration_s - rt.duration_s, True,
                                         list(rt.subtracks)))
                continue
            lt = None
            if rt.disc not in held_discs and id(rt) not in taken:
                lt = next((t for t in files if t.disc == rt.disc and t.number == rt.number
                           and t.path not in used_files), None)
            if lt is None:
                pairs.append(T.TrackPair("unmatched", None, rt, 0.0, None, False))
                continue
            used_files.add(lt.path)
            delta = None if rt.duration_s is None else lt.duration_s - rt.duration_s
            if rt.subtracks:
                pairs.append(T.TrackPair("subtracks", lt, rt, 1.0, delta, True, list(rt.subtracks)))
            else:
                sim = T._tsim(lt.title, rt.title)
                pairs.append(T.TrackPair("number", lt, rt, sim, delta, sim >= T.TITLE_MATCH_MIN))
        for t in files:
            if t.path not in {p.local.path for p in pairs if p.local is not None}:
                pairs.append(T.TrackPair("unmatched", t, None, 0.0, None, False))
        paired = {tuple(p.positions): p.local.path for p in pairs if p.positions and p.local is not None}
        sg = []
        for base, g in groups.items():
            merged, reason = decided[base]
            pos = [p for p, _t, _s in g]
            dur = None if any(s is None for _p, _t, s in g) else sum(s for _p, _t, s in g)
            sg.append(T.SubtrackGroup(self._sub_key(pos[0])[0], base, pos, [t for _p, t, _s in g], merged, reason,
                                      paired.get(tuple(pos)) if merged else None, dur))
        return MatchView(album.id, cid, rid, True, cands, pairs,
                         [x.local for x in pairs if x.local is not None and x.remote is None],
                         [x.remote for x in pairs if x.remote is not None and x.local is None], problems, pins, sg)

    def _manual_pairs(self, operation_id: int) -> list:
        return list(self._manual_ops.get(operation_id, []))

    def _manual(self, album_id: int, release_id_or_url: str) -> Job:
        if album_id < SYNTH_BASE and self.fake.album(album_id).status == MatchStatus.HELD:
            self.fake._set(album_id, status=MatchStatus.NEEDS_REVIEW, held_for=[])   # contract: manual clears HELD
        def work():
            raise MMError("Other release… is not implemented in the mock")
        return self._finish(JobKind.FETCH_CANDIDATES, work)

    # ================================================================ naming engine (mock)
    def _template_text(self, template: str | None) -> str:
        """Contract: every `template` parameter is a Template.id; None = Settings.active_template;
        an unknown id raises NotFound (raw text only goes to templates.preview/save)."""
        tid = self._settings.active_template if template is None else template
        for t in self._tpl_list():
            if t.id == tid:
                return t.text
        raise NotFound(f"template {tid!r}")

    def _root_for(self, folder: Path) -> Path:
        for r in self._settings.library_roots:
            if folder == r or r in folder.parents:
                return r
        return self._settings.library_roots[0] if self._settings.library_roots else folder.parent

    def _release(self, album_id: int, release_id: int | None = None):
        cands = self._candidates(album_id)
        rid = release_id if release_id is not None else self.fake._chosen.get(album_id)
        if rid is None:
            return cands[0] if cands else None
        for c in cands:
            if c.release_id == rid:
                return c
        raise NotFound(f"release {rid} is not a candidate for album {album_id}")

    def _album_fields(self, album: Album, cand) -> dict:
        years = [y for y in (album.year, cand.year if cand else "") if y]
        lead = min(years) if years else ""
        rel_year = cand.year if cand else album.year
        return dict(albumartist=album.artist, artist=album.artist, album=cand.title if cand else album.title,
                    leadyear=lead, originalyear=lead, reissueyear=rel_year if rel_year and rel_year != lead else "",
                    catno=format_catalog(cand.catno if cand else album.catno),
                    catno_raw=catno_raw(cand.catno if cand else album.catno),
                    label=(cand.label if cand else "") or "",
                    format=(cand.formats[0].split(",")[0].strip() if cand and cand.formats else ""),
                    disctotal="1")

    def _positions(self, album: Album, cand) -> list[str]:
        n = len(album.tracks)
        if self._settings.sequential_sides or not _is_sided(cand):
            return [str(t.number or i + 1) for i, t in enumerate(album.tracks)]
        if cand.alignment and all(p.remote and p.remote.position[:1].isalpha() for p in cand.alignment[:n]):
            return [p.remote.position for p in cand.alignment[:n]]
        half = (n + 1) // 2
        return [f"A{i + 1}" if i < half else f"B{i + 1 - half}" for i in range(n)]

    def _disc_ambiguous(self, album: Album, cand) -> list[Problem]:
        """Contract "Multi-disc pairing": files whose disc can't be told -> one DISC_AMBIGUOUS problem (paths =
        those files). Not overridable: allow_track_mismatch never removes it."""
        multi = self.fake.disc_pairing_explained(album.id, cand.release_id) if cand is not None else None
        if not multi or not multi[1]:
            return []
        _pairs, amb, conflicts = multi       # ccfa4a8: one DiscConflict per refused file, in file order
        return [Problem(ProblemKind.DISC_AMBIGUOUS, T.disc_warning(album.folder, amb, conflicts), list(amb),
                        disc_conflicts=list(conflicts))]

    def _disc_track_count(self, album: Album, cand) -> list[Problem]:
        """1566b36: one DISC_TRACK_COUNT problem (paths = that disc's files) per disc whose file and track counts
        differ and whose titles don't settle it. Not overridable: allow_track_mismatch never removes it."""
        if cand is None:
            return []
        return [Problem(ProblemKind.DISC_TRACK_COUNT, T.disc_count_message(album.folder, d, paths, m), list(paths))
                for d, _n, m, paths in self.fake.disc_count_holds(album.id, cand.release_id)]

    def _layout_ambiguous(self, album: Album) -> list[Problem]:
        """Contract "Album folders with loose audio and subfolders": one LAYOUT_AMBIGUOUS problem, paths =
        [album folder], first like the real album_target. Not overridable; never applied, moved or renamed."""
        msg = self.fake.layout_problem(album.id)
        if not msg:
            return []
        reason, subdirs, loose = self.fake.layout_conflict(album.id)
        return [Problem(ProblemKind.LAYOUT_AMBIGUOUS, msg, [album.folder], layout_reason=reason,
                        blocking_subdirs=subdirs, loose_files=loose)]

    def _disc_folder_mismatch(self, album: Album, cand, files) -> list[Problem]:
        """Contract "Behaviour changes of 2026-10-18" item 3: a destination that puts a track into a CDn/Disc n
        folder of another disc -> one DISC_FOLDER_MISMATCH (paths = those files). Fetched releases only (the
        disc tags come from the tracklist); not overridable."""
        if cand is None or not cand.fetched or not files:
            return []
        multi = self.fake.disc_pairing(album.id, cand.release_id)
        discs = ({t.path: rt.disc for t, rt in zip(album.tracks, multi[0]) if rt is not None} if multi is not None
                 else {t.path: 1 for t in album.tracks})
        wrong = T.folder_mismatches(self._root_for(album.folder),
                                    [(b, a, discs.get(b, 0)) for b, a in files])
        if not wrong:
            return []
        return [Problem(ProblemKind.DISC_FOLDER_MISMATCH, T.disc_folder_message(album.folder, wrong),
                        [m.path for m in wrong], folder_mismatches=wrong)]

    def _layout(self, album: Album, cand, text: str) -> tuple[Path, list[tuple[Path, Path]]]:
        root = self._root_for(album.folder)
        base = self._album_fields(album, cand)
        files = []
        multi = self.fake.disc_pairing(album.id, cand.release_id) if cand is not None else None
        if multi is not None:      # multi-disc: paired on (disc, track); unpaired/refused files stay in place
            remotes, _amb = multi
            discs = len({rt.disc for rt in self.fake._tracklists[cand.release_id]})
            for t, rt in zip(album.tracks, remotes):
                if rt is None:
                    continue
                f = dict(base, title=rt.title, tracknumber=str(rt.number), position=rt.position,
                         discnumber=str(rt.disc), disctotal=str(discs))
                segs = render_segments(text, f)
                files.append((t.path, root.joinpath(*segs[:-1], segs[-1] + t.path.suffix)))
        positions = self._positions(album, cand) if multi is None else []
        for t, pos in zip(album.tracks, positions):
            f = dict(base, title=t.title, tracknumber=str(t.number or ""), position=pos, discnumber=str(t.disc))
            segs = render_segments(text, f)
            files.append((t.path, root.joinpath(*segs[:-1], segs[-1] + t.path.suffix)))
        if files:
            folder = files[0][1].parent
        else:
            folder_part = text.rsplit("/", 1)[0] if "/" in text else ""
            segs = render_segments(folder_part + "/x", dict(base, title="", tracknumber="", position="",
                                                            discnumber="1")) if folder_part else ["x"]
            folder = root.joinpath(*segs[:-1])
        return folder, files

    def _album_label(self, album_id: int) -> str:
        a = self._album(album_id)
        return f"{a.artist} – {a.title}" if a.title else a.artist

    @staticmethod
    def _plan_names(album, cand) -> tuple[str, str]:
        """AlbumPlan.artist / title: the release's; loose tracks (d6ac2f3) get the folder's name and "" (held,
        never tagged as that release)."""
        if getattr(album, "loose_tracks", False):
            return album.folder.name, ""
        return cand.artist, cand.title

    def _collisions(self, layouts: dict[int, tuple[Path, list]]) -> list[Collision]:
        out, by_folder = [], {}
        for aid, (folder, _files) in layouts.items():
            by_folder.setdefault(folder, []).append(aid)
        for folder, ids in by_folder.items():
            if len(ids) > 1:
                out.append(Collision(folder, sorted(ids)))
        for aid, (_folder, files) in layouts.items():
            seen = {}
            for _before, after in files:
                seen[after] = seen.get(after, 0) + 1
            out += [Collision(p, [aid]) for p, n in seen.items() if n > 1]   # two files in one album
        return out

    @staticmethod
    def _auto_accept(candidates: list, rules=None, local_track_count: int | None = None):
        """Mirror of the real --auto check (service/match.py): best by auto_score; a lone candidate leads by its
        whole score; with require_track_count and a local count, an unfetched release (alignment None) can't be
        checked and is NOT accepted (API-DEVIATIONS #3)."""
        rules = rules or T.AutoRules()
        if not candidates:
            return None
        def auto(c):        # 4dc5e69: ranked by Candidate.auto_score (the --auto score), else score
            return c.auto_score if getattr(c, "auto_score", None) is not None else c.score
        ranked = sorted(candidates, key=lambda c: -auto(c))
        top = ranked[0]
        second = auto(ranked[1]) if len(ranked) > 1 else 0.0
        if auto(top) < rules.min_score or auto(top) - second < rules.min_margin:
            return None
        if rules.require_track_count and local_track_count is not None:
            if top.alignment is None:
                return None
            if sum(1 for p in top.alignment if p.remote is not None) != local_track_count:
                return None
        return top

    def _seed_problems(self):
        """MOCK: Slip It In has a stray symlink and a track-count mismatch."""
        ewb = next((a for a in self.fake._albums.values() if a.title == "Slip It In"), None)
        if ewb is not None:
            self.mock_problems[ewb.id] = [
                Problem(ProblemKind.SYMLINK_OUTSIDE, "This folder contains a link that points outside the album.",
                        [ewb.folder / "scans"]),
                Problem(ProblemKind.TRACK_MISMATCH,
                        f"Discogs lists {ewb.track_count + 1} tracks, folder has {ewb.track_count}", [ewb.folder])]

    def _nesting(self, album, dst: Path, root: Path) -> list[Problem]:
        # An adopted cluster moves only its own files (Bassline, after c334bbf): its folder is just their
        # common parent, so a destination below it is not nesting the album inside itself.
        if getattr(album, "cluster_id", None):
            return []
        return self._nesting_problems(album.folder, dst, root)

    @staticmethod
    def _nesting_problems(src: Path, dst: Path, root: Path) -> list[Problem]:
        out = []
        if dst != src and (src in dst.parents or src in root.parents or root == src):
            out.append(Problem(ProblemKind.DEST_INSIDE_SOURCE,
                               "The new folder would be inside the album's current folder.", [src, dst]))
        if dst != src and dst in src.parents:
            out.append(Problem(ProblemKind.SOURCE_INSIDE_TARGET,
                               "The album's current folder is inside the new folder.", [src, dst]))
        return out

    def _occupied_problems(self, album_id: int, folder: Path, extra: dict[int, Path] | None = None) -> list[Problem]:
        """Collision with another album already in (or queued for) that folder."""
        probs = []
        for other in self.fake._albums.values():
            if other.id == album_id:
                continue
            there = other.folder
            if (extra or {}).get(other.id) is not None:
                there = extra[other.id]
            if there == folder:
                probs.append(Problem(ProblemKind.COLLISION,
                                     f"Collision with another album: {other.artist} – {other.title} ({folder})",
                                     [folder]))
        return probs

    def _destination(self, album_id: int, release_id: int, template: str | None = None) -> Destination:
        album = self._album(album_id)
        cand = self._release(album_id, release_id)
        text = self._template_text(template)
        folder, files = self._layout(album, cand, text)
        problems = self._layout_ambiguous(album)
        problems += self._occupied_problems(album_id, folder, self._queued_folders(text, exclude=album_id))
        problems += [Problem(ProblemKind.COLLISION, f"Two files would get the same name: {c.target.name}", [c.target])
                     for c in self._collisions({album_id: (folder, files)}) if c.album_ids == [album_id]]
        root = self._root_for(album.folder)
        problems += self._nesting(album, folder, root) + self.mock_problems.get(album_id, [])
        problems += (self._disc_ambiguous(album, cand) + self._disc_track_count(album, cand)
                     + self._disc_folder_mismatch(album, cand, files))
        return Destination(root, folder, files if cand.fetched else [], problems)

    def _queued_folders(self, text: str, exclude: int) -> dict[int, Path]:
        """Where other ACCEPTED albums will land (their chosen release, same template)."""
        out = {}
        for aid, rid in self.fake._chosen.items():
            a = self.fake.album(aid)
            if aid != exclude and a.status is MatchStatus.ACCEPTED:
                out[aid] = self._layout(a, self._release(aid, rid), text)[0]
        return out

    # ================================================================ templates / settings
    def _tpl_list(self) -> list[Template]:
        return BUILTINS + list(self._user_templates.values())

    def _tpl_save(self, name: str, text: str) -> Template:
        if not name or not name.strip():
            raise ValidationError("Template name is empty", field="name")
        validate(text)
        slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-") or "template"
        t = Template(f"user:{slug}", name.strip(), text, False)
        self._user_templates[t.id] = t
        return t

    def _tpl_delete(self, template_id: str) -> None:
        if any(t.id == template_id for t in BUILTINS):
            raise ValidationError("Built-in templates can't be deleted", field="template_id")
        if template_id not in self._user_templates:
            raise NotFound(f"template {template_id}")
        del self._user_templates[template_id]
        if self._settings.active_template == template_id:      # contract doesn't say; mock falls back
            self._settings = replace(self._settings, active_template="archivist")

    def _tpl_preview(self, text: str, album_ids: list[int]) -> TemplatePreview:
        validate(text)
        layouts, moves, no_label = {}, [], 0
        label_in_folder = any("%label%" in seg for seg in text.split("/")[:-1])
        for aid in album_ids:
            album = self._album(aid)
            cand = self._release(aid)
            folder, files = self._layout(album, cand, text)
            layouts[aid] = (folder, files)
            moves += files if files else [(album.folder, folder)]
            if label_in_folder and not (cand and cand.label):
                no_label += 1
        warnings = []
        if no_label:
            warnings.append(f"{no_label} albums have no label: {UNKNOWN_LABEL}/" if no_label != 1
                            else f"1 album has no label: {UNKNOWN_LABEL}/")
        if "/" not in text:
            warnings.append("Every file would land directly in the library root")
        return TemplatePreview(moves, self._collisions(layouts), warnings)

    def _settings_set(self, **changes) -> Settings:
        known = {f.name for f in dc_fields(Settings)}
        for k, v in changes.items():
            if k not in known:
                raise ValidationError(f"Unknown setting {k}", field=k)
        if "merge_subtracks" in changes and not isinstance(changes["merge_subtracks"], bool):
            raise ValidationError("merge_subtracks must be true or false", field="merge_subtracks")
        if "verify_hash" in changes and changes["verify_hash"] not in ("sha256", "xxhash"):
            raise ValidationError("verify_hash must be sha256 or xxhash", field="verify_hash")
        for key in ("migrate_tag_keys", "write_styles", "styles_in_genre", "id3v23_mp3", "id3_latin1"):
            if key in changes and not isinstance(changes[key], bool):
                raise ValidationError(f"{key} must be true or false", field=key)
        if "active_template" in changes and not any(t.id == changes["active_template"] for t in self._tpl_list()):
            raise ValidationError(f"No template {changes['active_template']!r}", field="active_template")
        before = self._settings.merge_subtracks
        self._settings = replace(self._settings, **changes)
        if "merge_subtracks" in changes and self._settings.merge_subtracks != before:   # 3d31a39
            for album_id in self._albums_with_subtracks():
                self.fake.events.match_view_changed.emit(album_id)
        return self._settings

    # ================================================================ plans
    def _build(self, album_ids: list[int], template: str | None = None,
               allow_track_mismatch: set[int] = frozenset(), allow_encoding_drop: set[int] = frozenset()) -> Plan:
        text = self._template_text(template)
        layouts, aps, moves, tags = {}, [], 0, 0
        not_fetched = []
        for aid in album_ids:
            album = self._album(aid)
            cand = self._release(aid)
            if cand is None:
                raise MMError(f"album {aid} has no chosen release")
            key = (aid, cand.release_id)
            stale = key in self.fetched_at and self.clock() - self.fetched_at[key] > STALE_AFTER_S
            if self.simulate_fetch and (not cand.fetched or stale):
                folder, _files = self._layout(album, cand, text)
                err = self._fetch_errors.get(key)
                if not (err and self.clock() - err[0] < FETCH_ERROR_HOLD_S):
                    self._fetch_errors.pop(key, None); err = None
                not_fetched.append((AlbumPlan(aid, cand.release_id, album.folder, folder, [], [], *self._plan_names(album, cand)), err))
                continue
            layouts[aid] = (album, cand) + self._layout(album, cand, text)
        targets = {aid: v[2] for aid, v in layouts.items()}
        collided = {}
        for c in self._collisions({aid: (v[2], v[3]) for aid, v in layouts.items()}):
            for aid in c.album_ids:
                collided.setdefault(aid, []).append(Problem(
                    ProblemKind.COLLISION, f"Collision: {c.target} is also the target of " +
                    (", ".join(self._album_label(o) for o in c.album_ids if o != aid) or "another file in this album"),
                    [c.target]))
        for aid, (album, cand, folder, files) in layouts.items():
            problems = (self._layout_ambiguous(album)                       # never overridden
                        + collided.get(aid, []) + self._occupied_problems(aid, folder, targets)
                        + self._nesting(album, folder, self._root_for(album.folder))
                        + [p for p in self.mock_problems.get(aid, [])
                           if not (p.kind == ProblemKind.TRACK_MISMATCH and aid in allow_track_mismatch)]
                        + self._disc_ambiguous(album, cand)            # never overridden
                        + self._disc_track_count(album, cand)          # never overridden
                        + self._disc_folder_mismatch(album, cand, files))     # never overridden
            _pairs, pin_problems, pinned = (self._apply_pins(album, cand, list(cand.alignment))
                                            if cand.alignment else ([], [], {}))
            problems += pin_problems
            multi = self.fake.disc_pairing(aid, cand.release_id)
            if multi is None:
                positions = dict(zip((t.path for t in album.tracks), self._positions(album, cand)))
                numbers = {t.path: (str(t.number), None) for t in album.tracks}
            else:                                   # multi-disc: the paired track's disc and number
                positions = {t.path: rt.position for t, rt in zip(album.tracks, multi[0]) if rt is not None}
                numbers = {t.path: (str(rt.number), str(rt.disc)) for t, rt in zip(album.tracks, multi[0])
                           if rt is not None}
            fcs = []
            keys_now = self.mock_tag_keys.get(aid, {})
            for before, after in files:
                t = next(t for t in album.tracks if t.path == before)
                num, disc = numbers[before]
                tc = ([TagChange("TRACKNUMBER", str(t.number), num)]
                      + ([TagChange("DISCNUMBER", str(t.disc) if t.disc else None, disc)] if disc else []) + [
                      TagChange("DISCOGS_POSITION", None, positions[before]),
                      TagChange("CATALOGNUMBER", album.catno or None, cand.catno),
                      TagChange("LABEL", None, cand.label),
                      TagChange("DISCOGS_RELEASE_ID", None, str(cand.release_id))])
                if before in pinned:
                    tc = [replace(x, after=pinned[before]) if x.key == "DISCOGS_POSITION" else x for x in tc]
                tc += self._key_changes(keys_now)
                fcs.append(FileChange("track", before, after, tc, pinned=before in pinned))
                tags += len(tc)
            moves += len(fcs)
            unwritten = [] if problems else [replace(u, folder=album.folder) for u in self.mock_unwritten.get(aid, [])]
            aps.append(AlbumPlan(aid, cand.release_id, album.folder, folder, fcs, problems, *self._plan_names(album, cand),
                                 unwritten=unwritten))
        wanted = [(ap.album_id, ap.release_id) for ap, err in not_fetched if err is None]
        fetch_job = self._fetch_job(wanted) if wanted else None
        pending = []
        for ap, err in not_fetched:      # same wording as the real service (plan.py @ 0666aad)
            msg = (f"Release {ap.release_id} could not be fetched from Discogs: {err[1]}" if err and fetch_job is None
                   else f"Release {ap.release_id} is being fetched from Discogs: build the plan again when it arrives")
            pending.append(replace(ap, problems=[Problem(ProblemKind.RELEASE_NOT_FETCHED, msg, [ap.folder_before])]))
        not_fetched = pending
        order = {aid: i for i, aid in enumerate(album_ids)}
        aps = sorted(aps + not_fetched, key=lambda ap: order[ap.album_id])
        warnings = [f"{u.folder or ap.folder_before}: {u.field}: {u.detail}" for ap in aps for u in ap.unwritten]
        if not_fetched:
            warnings.append(f"{len(not_fetched)} release(s) are being fetched from Discogs (job {fetch_job}): build "
                            "the plan again when it finishes" if fetch_job is not None
                            else f"{len(not_fetched)} release(s) could not be fetched from Discogs")
        refused = sum(1 for ap in aps if ap.problems and ap not in not_fetched)
        if refused:
            warnings.append(f"{refused} album(s) have problems and will be refused (held) by apply")
        # like the real service (plan.py @ 0666aad): every album with a built target counts, held ones too
        covers = sum(1 for ap in aps if ap not in not_fetched) if self._settings.save_cover_art else 0
        fetch_kw = {"fetch_job": fetch_job}
        if covers:   # real service: folder.jpg is a plan warning, not a FileChange (API-DEVIATIONS #7)
            warnings.append(f"folder.jpg will be saved from Discogs for {covers} album(s)")
        p = Plan(next(self._ids), aps, warnings, moves, tags, 0, 0, **fetch_kw)   # read-only: never writes a status
        self._plans[p.id] = p
        return p

    def _fetch_job(self, wanted: list[tuple[int, int]]) -> int:
        """MOCK of Plan.fetch_job: one FETCH_CANDIDATES job per batch; a build while it is queued or running
        returns the same id and queues nothing new. Reports job_progress, candidate_ready and album_changed
        per album; a failed download is reported by builds for 2 minutes with fetch_job = None."""
        live = {self._fetching[w] for w in wanted if w in self._fetching
                and self._jobs[self._fetching[w]].state in (JobState.QUEUED, JobState.RUNNING)}
        if live:
            return max(live)
        job = Job(next(self._ids), JobKind.FETCH_CANDIDATES, JobState.QUEUED, 0, len(wanted),
                  f"Downloading {len(wanted)} release(s) from Discogs")
        self._jobs[job.id] = job
        for w in wanted:
            self._fetching[w] = job.id
        state = {"i": 0, "failed": []}

        def step():
            i = state["i"]
            if i < len(wanted):
                aid, rid = wanted[i]
                if rid in self.mock_fetch_fail:
                    state["failed"].append(rid)
                    self._fetch_errors[(aid, rid)] = (self.clock(), f"release {rid}: HTTP 404")
                else:
                    self.fake._fetch_queue.insert(0, (aid, rid)); self.fake._fetch_one(pace=False)
                    self.fetched_at[(aid, rid)] = self.clock()
                    self._fetch_errors.pop((aid, rid), None)
                self.fake.events.album_changed.emit(aid)
                state["i"] = i + 1
                cur = replace(self._jobs[job.id], state=JobState.RUNNING, done=i + 1,
                              message=f"{i + 1} of {len(wanted)}: {self._album_label(aid)}")
                self._jobs[job.id] = cur
                self.events.job_progress.emit(cur)
                QTimer.singleShot(FETCH_STEP_MS, self.fake, step)
                return
            ok = len(wanted) - len(state["failed"])
            msg = f"Fetched {ok} release(s): rebuild the plan"
            if state["failed"]:
                msg += f"; {len(state['failed'])} failed: " + " | ".join(f"release {r}: HTTP 404" for r in state["failed"])
            done = replace(self._jobs[job.id], state=JobState.FAILED if not ok else JobState.DONE,
                           message=f"MMError: {msg}" if not ok else msg)
            self._jobs[job.id] = done
            self.events.job_progress.emit(done)
            self.events.job_finished.emit(done)
            self._announced.add(job.id)
        QTimer.singleShot(0, self.fake, step)     # build may run on a worker thread
        return job.id

    def _apply(self, plan_id: int) -> Job:
        if self.apply_raises_when_nothing_applicable:     # Bassline's coming change: checked before any Job
            plan = self._plans.get(plan_id)
            if plan is None:
                raise NotFound(f"plan {plan_id}")
            if not any(not ap.problems for ap in plan.albums):   # same wording as plan.py @ a06a7c9
                waiting = [ap for ap in plan.albums
                           if any(p.kind == ProblemKind.RELEASE_NOT_FETCHED for p in ap.problems)]
                held = [ap for ap in plan.albums if ap not in waiting]
                for ap in held:                                       # albums with problems are held as usual
                    self.fake._set(ap.album_id, status=MatchStatus.HELD, held_for=list(ap.problems))
                parts = []
                if held:
                    parts.append(f"{len(held)} album(s) have problems (held; rebuild the plan)")
                if waiting:
                    parts.append(f"{len(waiting)} album(s) wait for their release from Discogs (rebuild the plan)")
                raise MMError("Nothing to apply: " + ("; ".join(parts) or "the plan is empty"))

        def work():
            plan = self._plans.get(plan_id)
            if plan is None:
                raise NotFound(f"plan {plan_id}")
            waiting = [ap for ap in plan.albums if any(p.kind == ProblemKind.RELEASE_NOT_FETCHED for p in ap.problems)]
            changed = {ap.album_id: Problem(ProblemKind.FILE_CHANGED,
                                            "Files changed on disk since the plan was built: rebuild the plan",
                                            [ap.folder_before])
                       for ap in plan.albums if not ap.problems and ap.album_id in self.mock_changed_before_apply}
            ok = [ap for ap in plan.albums if not ap.problems and ap.album_id not in changed]
            refused = len(plan.albums) - len(ok) - len(waiting)
            for ap in plan.albums:   # contract: refused albums become HELD with held_for = their problems
                if ap.album_id in changed:                 # refused during the run (real: _changed(fingerprint))
                    self.fake._set(ap.album_id, status=MatchStatus.HELD, held_for=[changed[ap.album_id]])
                elif ap.problems and ap not in waiting:    # RELEASE_NOT_FETCHED: left out, status unchanged
                    self.fake._set(ap.album_id, status=MatchStatus.HELD, held_for=list(ap.problems))
            if ok:
                done = replace(plan, albums=ok, files_to_move=sum(len(a.files) for a in ok),
                               tags_to_write=sum(len(f.tags) for a in ok for f in a.files))
                names = ", ".join(self._album_label(ap.album_id) for ap in ok)
                op = self._record("apply", done, f"Tagged and moved: {names}")
                self._manual_ops[op.id] = [ManualPair(ap.album_id, f.path_before, next(
                    (t.after for t in f.tags if t.key == "DISCOGS_POSITION"), ""),
                    self._pins.get(ap.album_id, {}).get(f.path_before, Pin(f.path_before, "", 0, False, False)).override_hold)
                    for ap in ok for f in ap.files if getattr(f, "pinned", False)]
                for ap in ok:                    # contract: a successful apply uses the album's pins up
                    self._pins.pop(ap.album_id, None)
                for ap in ok:
                    self.fake._set(ap.album_id, status=MatchStatus.APPLIED, folder=ap.folder_after, held_for=[])
            msg = f"Applied {len(ok)} album(s)"
            if refused:
                msg += f"; refused {refused} with problems (held; rebuild the plan)"
            if not ok:
                raise MMError(msg)
            return msg
        return self._finish(JobKind.APPLY, work)

    # ================================================================ journal
    def _record(self, kind: str, plan: Plan, summary: str, when: datetime | None = None, **kw) -> Operation:
        when = when or datetime.now(timezone.utc)
        op = Operation(next(self._ids), when, when + timedelta(seconds=40), len(plan.albums), plan.files_to_move,
                       kind, kw.pop("undoable", kind != "undo"), False, None, kw.pop("undo_of", None), summary,
                       kw.get("journal_format", 2), kw.get("journal_format", 2) < 2)
        self._ops[op.id] = op
        self._op_plans[op.id] = plan
        return op

    def _key_changes(self, keys: dict[str, str]) -> list:
        """MOCK of the engine's explicit key changes: an alias becomes the Picard key (value unchanged: a pure
        rename, before == after); a Vorbis YEAR is removed only with migrate_tag_keys (else kept, not shown)."""
        out = []
        for old, value in keys.items():
            if old in TAG_ALIASES:
                new = TAG_ALIASES[old]
                out.append(TagChange(new.lower(), value, value, old, new))
            elif old == "YEAR" and self._settings.migrate_tag_keys:
                out.append(TagChange("year", value, None, "YEAR", None, migration=True))
        return out

    def add_picard_demo(self, album_id: int) -> None:
        """MOCK: give an album legacy keys and held-back fields (contract "Picard tag names (2026-10-21)")."""
        self.mock_tag_keys[album_id] = {"TOTALTRACKS": "12", "YEAR": "1984"}
        U = T.UnwrittenField
        self.mock_unwritten[album_id] = [
            U("barcode", "conflicting_barcodes", "the Barcode identifiers give 0 18861 00232 7 and 0 18861 00233 4"),
            U("releasecountry", "region_not_country", "\"UK & Europe\" is a region, not a country"),
            U("media", "mixed_media", "the formats name CD and Vinyl"),
            U("releasetype", "no_single_type", "the formats describe Album and EP"),
            U("catalognumber", "missing_catalog_number", "SST Records has no catalog number (\"none\")"),
            U("releasestatus", "some_future_code", "Discogs lists this release as a test pressing"),
        ]

    @staticmethod
    def _reverse(plan: Plan, new_id: int) -> Plan:
        aps = [AlbumPlan(ap.album_id, ap.release_id, ap.folder_after, ap.folder_before,
                         [FileChange(f.kind, f.path_after, f.path_before,
                                     [TagChange(t.key, t.after, t.before, t.key_after, t.key_before, t.migration) for t in f.tags]) for f in ap.files], [],
                         ap.artist, ap.title)
               for ap in plan.albums]
        return Plan(new_id, aps, [], plan.files_to_move, plan.tags_to_write, 0)

    def _seed_journal(self):
        """Mock history: a legacy-format (pickle) apply (9 days ago), an undone apply + its undo (2 days ago)
        and an undoable apply (3 h ago)."""
        now = datetime.now(timezone.utc)
        for aid, ago, undone, fmt in ((5, timedelta(days=9), False, 1), (3, timedelta(days=2), True, 2),
                                      (2, timedelta(hours=3, minutes=12), False, 2)):
            album = self.fake.album(aid)
            cand = self._candidates(aid)[0]
            folder, files = self._layout(album, cand, BUILTINS[0].text)
            # e1eca5e: journals since 2026-10-17 record release_id, artist and title; the legacy one predates
            # that, so its undo preview has release_id 0 and "" names (the UI falls back to the library/folder)
            names = (cand.artist, cand.title) if fmt >= 2 else ("", "")
            plan = Plan(next(self._ids), [AlbumPlan(aid, cand.release_id if fmt >= 2 else 0, album.folder, folder,
                        [FileChange("track", b, a, [TagChange("DISCOGS_RELEASE_ID", None, str(cand.release_id))])
                         for b, a in files], [], *names)], [], len(files), len(files), 0)
            op = self._record("apply", plan, f"Tagged and moved: {album.artist} – {album.title}", now - ago,
                              journal_format=fmt)
            if aid == 2 and files:
                self.mock_changed[files[-1][1]] = "contents differ"
            if undone:
                u = self._record("undo", self._reverse(plan, 0), f"Undid: {op.summary}",
                                 now - ago + timedelta(minutes=5), undo_of=op.id)
                self._ops[op.id] = replace(op, undoable=False, undone=True, undone_by=u.id)

    def _journal_list(self, offset: int = 0, limit: int = 200) -> Page:
        ops = sorted(self._ops.values(), key=lambda o: o.started, reverse=True)
        return Page(ops[offset:offset + limit], len(ops), offset)

    def _undo_plan(self, operation_id: int, trust_legacy: bool = False) -> Plan:
        op = self._ops.get(operation_id)
        if op is None:
            raise NotFound(f"operation {operation_id}")
        if not op.undoable:
            raise MMError(f"Operation {operation_id} can't be undone" + (" (already undone)" if op.undone else ""))
        p = self._reverse(self._op_plans[operation_id], next(self._ids))
        p = replace(p, albums=[replace(ap, files=[replace(f, kind=self.undo_track_kind) if f.kind == "track" else f
                                                  for f in ap.files]) for ap in p.albums])
        # files changed since the apply: those steps stay in files with skip_reason set (contract 21:5x PT),
        # plus a FILE_CHANGED problem naming them; not counted in files_to_move/tags_to_write/bytes_to_copy
        aps = []
        for ap in p.albums:
            fcs = [replace(f, skip_reason=f"changed since ({self.mock_changed[f.path_before]})")
                   if f.path_before in self.mock_changed else f for f in ap.files]
            skipped = [f for f in fcs if f.skip_reason]
            if skipped:
                ap = replace(ap, files=fcs, problems=ap.problems + [
                    Problem(ProblemKind.FILE_CHANGED, f.skip_reason, [f.path_before]) for f in skipped])
            aps.append(ap)
        live = [f for a in aps for f in a.files if not f.skip_reason]
        p = replace(p, albums=aps, files_to_move=len(live), tags_to_write=sum(len(f.tags) for f in live),
                    skipped=sum(1 for a in aps for f in a.files if f.skip_reason))
        if op.legacy and not trust_legacy:   # tag steps of an old-format journal need explicit trust
            p = replace(p, albums=[replace(ap, problems=ap.problems + [Problem(
                ProblemKind.LEGACY_JOURNAL,
                "These tag changes were recorded in an older journal format; restoring them needs your confirmation.",
                [f.path_before for f in ap.files if f.tags])]) if any(f.tags for f in ap.files) else ap
                for ap in p.albums])
        self._plans[p.id] = p
        self._undo_preview[operation_id] = p.id
        return p

    def _undo(self, operation_id: int, plan_id: int) -> Job:
        def work():
            if self._undo_preview.get(operation_id) != plan_id:
                raise PlanStale(f"undo preview {plan_id} is not the current preview for operation {operation_id}")
            del self._undo_preview[operation_id]
            op = self._ops[operation_id]
            plan = self._plans[plan_id]
            if any(p.kind == ProblemKind.LEGACY_JOURNAL for ap in plan.albums for p in ap.problems):
                # journal format is per operation: an untrusted legacy preview blocks the whole undo
                self._undo_preview[operation_id] = plan_id
                raise MMError("Undo refused: legacy_journal needs trust_legacy=True")
            u = self._record("undo", plan, f"Undid: {op.summary}", undo_of=op.id)
            self._ops[op.id] = replace(op, undoable=False, undone=True, undone_by=u.id)
            for ap in plan.albums:
                self.fake._set(ap.album_id, status=MatchStatus.NEEDS_REVIEW, folder=ap.folder_after)
            return u.summary
        return self._finish(JobKind.UNDO, work)
