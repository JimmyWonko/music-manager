"""service.settings: the user's settings, saved in config.toml."""

from __future__ import annotations

import json
from dataclasses import fields, replace
from pathlib import Path

from ..providers.discogs import subtrack_groups
from . import coverart
from ._planning import all_templates
from ._state import events, state
from ._types import COVER_SOURCES, Settings, ValidationError


def get() -> Settings:
    st = state()
    cfg = st.config()
    cover = coverart.options(st)
    return Settings(library_roots=[*cfg.library_roots], active_template=cfg.active_template,
                    sequential_sides=cfg.sequential_sides, save_cover_art=cfg.save_cover_art,
                    clear_other_tags=cfg.clear_other_tags, embed_cover_art=cover.embed_cover_art,
                    replace_cover_art=cover.replace_cover_art, cover_source=cover.cover_source,
                    cover_max_bytes=cover.cover_max_bytes, merge_subtracks=cfg.merge_subtracks,
                    migrate_tag_keys=cfg.migrate_tag_keys)


def set(**changes) -> Settings:  # noqa: A001 - contract name
    """Change settings, e.g. set(active_template="archivist"). Unknown keys or bad values: ValidationError.

    The cover art settings (embed_cover_art, replace_cover_art, cover_source,
    cover_max_bytes) also take None: back to the active template's preset default.
    """
    st = state()
    known = {f.name for f in fields(Settings)}
    for key in changes:
        if key not in known:
            raise ValidationError(f"Unknown setting {key}", field=key)
    values = {}
    if "library_roots" in changes:
        roots = changes["library_roots"]
        if isinstance(roots, (str, Path)) or roots is None:
            raise ValidationError("library_roots must be a list of folders", field="library_roots")
        paths = [Path(r).expanduser() for r in roots]
        for p in paths:
            if not p.is_absolute():
                raise ValidationError(f"library root must be an absolute path: {p}", field="library_roots")
        values["library_roots"] = paths
    if "active_template" in changes:
        tid = changes["active_template"]
        if not any(t.id == tid for t in all_templates(st)):
            raise ValidationError(f"No template {tid!r}", field="active_template")
        values["active_template"] = tid
    for key in ("sequential_sides", "save_cover_art", "clear_other_tags", "merge_subtracks", "migrate_tag_keys"):
        if key in changes:
            if not isinstance(changes[key], bool):
                raise ValidationError(f"{key} must be True or False", field=key)
            values[key] = changes[key]
    for key in ("embed_cover_art", "replace_cover_art"):
        if key in changes:
            if changes[key] is not None and not isinstance(changes[key], bool):
                raise ValidationError(f"{key} must be True, False or None", field=key)
            values[key] = changes[key]
    if "cover_source" in changes:
        if changes["cover_source"] is not None and changes["cover_source"] not in COVER_SOURCES:
            raise ValidationError(f"cover_source must be one of {', '.join(COVER_SOURCES)} or None",
                                  field="cover_source")
        values["cover_source"] = changes["cover_source"]
    if "cover_max_bytes" in changes:
        value = changes["cover_max_bytes"]
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ValidationError("cover_max_bytes must be a whole number >= 0 (0: no limit) or None",
                                  field="cover_max_bytes")
        values["cover_max_bytes"] = value
    before = st.config().merge_subtracks
    st.save_config(replace(st.config(), **values))
    if "merge_subtracks" in values and values["merge_subtracks"] != before:
        for album_id in _albums_with_subtracks(st):
            events.match_view_changed.emit(album_id)
    return get()


def _albums_with_subtracks(st) -> list[int]:
    """Albums whose view merge_subtracks can change: a fetched candidate release with subtrack groups."""
    out: list[int] = []
    for row in st.index.query("SELECT album_id, release FROM candidates WHERE release IS NOT NULL "
                              "ORDER BY album_id, rank, release_id"):
        if out and out[-1] == row["album_id"]:
            continue
        try:
            if subtrack_groups(json.loads(row["release"])):
                out.append(row["album_id"])
        except (TypeError, ValueError):
            continue
    return out


__all__ = ["get", "set"]
