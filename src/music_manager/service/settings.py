"""service.settings: the user's settings, saved in config.toml."""

from __future__ import annotations

from dataclasses import fields, replace
from pathlib import Path

from ._planning import all_templates
from ._state import state
from ._types import Settings, ValidationError


def get() -> Settings:
    cfg = state().config()
    return Settings(library_roots=[*cfg.library_roots], active_template=cfg.active_template,
                    sequential_sides=cfg.sequential_sides, save_cover_art=cfg.save_cover_art,
                    clear_other_tags=cfg.clear_other_tags)


def set(**changes) -> Settings:  # noqa: A001 - contract name
    """Change settings, e.g. set(active_template="archivist"). Unknown keys or bad values: ValidationError."""
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
    for key in ("sequential_sides", "save_cover_art", "clear_other_tags"):
        if key in changes:
            if not isinstance(changes[key], bool):
                raise ValidationError(f"{key} must be True or False", field=key)
            values[key] = changes[key]
    st.save_config(replace(st.config(), **values))
    return get()


__all__ = ["get", "set"]
