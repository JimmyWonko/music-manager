"""service.coverart: which front cover plan.apply embeds, its preview, and the Discogs image cache.

plan.build never calls Discogs: a Discogs cover is embedded only once it is
in the image cache (data_dir/cache/covers), which coverart.fetch (or the
Plan.cover_job that plan.build queues) fills. Folder art (cover/folder/front
.jpg/.jpeg/.png in the album folder) needs no download. Images are never
resized: Pillow is not a dependency; cover_max_bytes only skips large ones.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .. import coverart as engine
from ._common import album_record, cluster_guess, is_loose_tracks, local_tracks
from ._planning import chosen_release_id
from ._releases import _primary_image, provider_call, release_for
from ._state import State, events, state
from ._types import (
    COVER_SOURCES,
    CoverArt,
    CoverDefaults,
    CoverPreview,
    Job,
    JobKind,
    JobState,
    MMError,
    NotFound,
)

# Per naming preset. All four: embedding is opt-in (off), existing art is kept, nothing is downscaled; user templates use archivist's.
PRESET_DEFAULTS: dict[str, CoverDefaults] = {
    "archivist": CoverDefaults(),
    "simple": CoverDefaults(),
    "edition-detail": CoverDefaults(),
    "label-archive": CoverDefaults(),
}


def preset_defaults(template_id: str) -> CoverDefaults:
    """The cover art defaults of a naming preset (user templates: archivist's)."""
    return PRESET_DEFAULTS.get(template_id, PRESET_DEFAULTS["archivist"])


def options(st: State | None = None) -> CoverDefaults:
    """Effective cover settings: the user's values, else the active template's preset defaults."""
    cfg = (st or state()).config()
    base = preset_defaults(cfg.active_template)
    return CoverDefaults(
        embed_cover_art=base.embed_cover_art if cfg.embed_cover_art is None else cfg.embed_cover_art,
        replace_cover_art=base.replace_cover_art if cfg.replace_cover_art is None else cfg.replace_cover_art,
        cover_source=base.cover_source if cfg.cover_source not in COVER_SOURCES else cfg.cover_source,
        cover_max_bytes=base.cover_max_bytes if cfg.cover_max_bytes is None else cfg.cover_max_bytes)


# --- the image cache ---------------------------------------------------------------------

def _cache_path(st: State, url: str) -> Path:
    return st.data_dir / "cache" / "covers" / hashlib.sha256(url.encode("utf-8")).hexdigest()


def cached_image(st: State, url: str) -> bytes | None:
    try:
        return _cache_path(st, url).read_bytes()
    except OSError:
        return None


def _store(st: State, url: str, data: bytes) -> None:
    dest = _cache_path(st, url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".cover.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- choosing the cover --------------------------------------------------------------------

@dataclass
class Choice:
    image: engine.Image | None = None
    art: CoverArt | None = None
    pending_url: str | None = None  # a Discogs cover not downloaded yet
    warnings: list[str] = field(default_factory=list)


def _art(source, image: engine.Image, path: Path | None, url: str | None) -> CoverArt:
    return CoverArt(source, image.mime, image.width, image.height, len(image.data), path, url, image.data)


def choose(st: State, folder: Path, release, opts: CoverDefaults, *, folder_ok: bool = True,
           search_url: str | None = None) -> Choice:
    """The front cover to embed for an album folder and release, by opts.cover_source. Never downloads."""
    out = Choice()
    if not opts.embed_cover_art:
        return out
    url = _primary_image(release)[0] or search_url
    order = {"folder_first": ("folder", "discogs"), "discogs_first": ("discogs", "folder"),
             "folder": ("folder",), "discogs": ("discogs",)}[opts.cover_source]
    for source in order:
        if source == "folder":
            path = engine.folder_art(folder) if folder_ok else None
            if path is None:
                continue
            try:
                data = path.read_bytes()
            except OSError as exc:
                out.warnings.append(f"cannot read {path}: {exc}")
                continue
            image, where, link = engine.sniff(data), path, None
            label = path.name
        else:
            if not url:
                continue
            data = cached_image(st, url)
            if data is None:
                out.pending_url = url
                continue
            image, where, link = engine.sniff(data), None, url
            label = "the Discogs cover"
        if image is None:
            out.warnings.append(f"{folder.name}: {label} is not a JPEG or PNG: not embedded")
            continue
        if opts.cover_max_bytes and len(image.data) > opts.cover_max_bytes:
            out.warnings.append(f"{folder.name}: {label} is larger than {opts.cover_max_bytes} bytes "
                                f"({len(image.data)}): not embedded (images are never resized)")
            continue
        out.image, out.art = image, _art(source, image, where, link)
        break
    return out


def fetch_job(st: State, wanted: list[tuple[int, str]]) -> int | None:
    """Queue (or reuse) a FETCH_COVER job downloading these (album id, url) covers into the cache."""
    with st.lock:
        covering: list[int] = []
        todo: list[tuple[int, str]] = []
        for album_id, url in wanted:
            job_id = st.cover_fetching.get(url)
            if job_id is not None:
                try:
                    live = st.runner.get(job_id).state not in (JobState.DONE, JobState.FAILED, JobState.CANCELLED)
                except NotFound:
                    live = False
                if live:
                    covering.append(job_id)
                    continue
            todo.append((album_id, url))
        if not todo:
            return max(covering) if covering else None

        def work(ctx) -> str:
            done, errors = 0, []
            try:
                for i, (album_id, url) in enumerate(todo):
                    ctx.progress(i, len(todo), "downloading cover art from Discogs")
                    if cached_image(st, url) is None:
                        try:
                            _store(st, url, provider_call(st.client().image, url))
                        except (MMError, OSError) as exc:
                            errors.append(f"album {album_id}: {exc}")
                            continue
                    done += 1
                    events.cover_art_ready.emit(album_id)
                ctx.progress(len(todo), len(todo), "downloaded")
            finally:
                with st.lock:
                    for _aid, url in todo:
                        if st.cover_fetching.get(url) == ctx.job_id:
                            st.cover_fetching.pop(url, None)
            msg = f"Downloaded {done} cover(s): rebuild the plan"
            if errors:
                msg += f"; {len(errors)} failed: " + " | ".join(errors)
            if not done:
                raise MMError(msg)
            return msg

        job = st.runner.submit(JobKind.FETCH_COVER, work, total=len(todo), message="queued")
        for _aid, url in todo:
            st.cover_fetching[url] = job.id
        return job.id


# --- public API ------------------------------------------------------------------------------

def _album_source(st: State, album_id: int):
    """(folder, release or None, search cover url, folder art allowed, release id) for an album."""
    row = album_record(album_id, st)
    rid = chosen_release_id(album_id, st)
    release, search = None, {}
    if rid is not None:
        release, _year, search = release_for(album_id, rid, st, fetch=False)
    from ._common import album_folder

    album = album_folder(album_id, st)
    folder_ok = not cluster_guess(row) and not is_loose_tracks(album)
    return album.path, release, (search or {}).get("cover_image") or None, folder_ok, rid


def preview(album_id: int) -> CoverPreview:
    """The album's cover art as plan.build would embed it now, with the image bytes. Read-only, no network."""
    st = state()
    album_id = int(album_id)
    folder, release, search_url, folder_ok, _rid = _album_source(st, album_id)
    opts = options(st)
    choice = choose(st, folder, release, opts, folder_ok=folder_ok, search_url=search_url)
    tracks = [t.path for t in local_tracks(album_id, st)]
    existing = None
    for path in tracks:
        try:
            found = engine.embedded_front(path)
        except Exception:  # noqa: BLE001 - unreadable file: no art shown
            found = None
        if found is not None:
            existing = _art("embedded", found, path, None)
            break
    embed = keep = 0
    if choice.image is not None:
        for path in tracks:
            try:
                change = engine.preview_embed(path, choice.image, opts.replace_cover_art)
            except Exception:  # noqa: BLE001 - unreadable file: counted nowhere
                change = False
            embed += bool(change)
            keep += change is None
    if not opts.embed_cover_art:
        message = "cover art embedding is off (settings embed_cover_art)"
    elif choice.art is None and choice.pending_url:
        message = "the Discogs cover is not downloaded yet: coverart.fetch"
    elif choice.art is None:
        message = "; ".join(choice.warnings) or "no cover found (folder art or Discogs image)"
    else:
        message = ""
    return CoverPreview(album_id, choice.art, existing, embed, keep, choice.pending_url, message)


def fetch(album_ids: list[int]) -> Job:
    """Download the Discogs front covers of these albums into the image cache (a FETCH_COVER job)."""
    st = state()
    wanted = []
    for aid in album_ids:
        _folder, release, search_url, _ok, _rid = _album_source(st, int(aid))
        url = _primary_image(release)[0] or search_url
        if url:
            wanted.append((int(aid), url))
    if not wanted:
        raise MMError("No Discogs cover to download for these albums")
    job_id = fetch_job(st, wanted)
    if job_id is None:
        raise MMError("No Discogs cover to download for these albums")
    return st.runner.get(job_id)


__all__ = ["fetch", "preset_defaults", "preview"]
