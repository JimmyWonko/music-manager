"""service.templates: built-in naming presets, user templates, and the live preview."""

from __future__ import annotations

import re
from dataclasses import replace

from ..naming.templates import TemplateStructureError
from ._planning import AlbumTarget, album_target, all_templates, compile_text, cross_album_collisions
from ._state import state
from ._types import Collision, MMError, NotFound, Template, TemplatePreview, ValidationError

UNKNOWN_LABEL = "Unknown label"


def list() -> list[Template]:  # noqa: A001 - contract name
    """Built-in presets first (contract order), then user templates."""
    return all_templates()


def save(name: str, text: str) -> Template:
    """Create or overwrite the user template "user:<slug of name>". ValidationError if the text is bad."""
    if not name or not name.strip():
        raise ValidationError("Template name is empty", field="name")
    compile_text(text)
    st = state()
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().casefold()).strip("-") or "template"
    tid = f"user:{slug}"
    cfg = st.config()
    templates = dict(cfg.templates)
    templates[tid] = (name.strip(), text)
    st.save_config(replace(cfg, templates=templates))
    return Template(tid, name.strip(), text, False)


def delete(template_id: str) -> None:
    """User templates only. Deleting the active template makes "archivist" active."""
    st = state()
    if any(t.builtin and t.id == template_id for t in all_templates(st)):
        raise ValidationError("Built-in templates can't be deleted", field="template_id")
    cfg = st.config()
    if template_id not in cfg.templates:
        raise NotFound(f"template {template_id!r}")
    templates = dict(cfg.templates)
    del templates[template_id]
    active = "archivist" if cfg.active_template == template_id else cfg.active_template
    st.save_config(replace(cfg, templates=templates, active_template=active))


def preview(text: str, album_ids: list[int]) -> TemplatePreview:
    """Render raw template text against albums. Read-only.

    Raises ValidationError for unknown fields, functions or bad syntax. A
    layout that cannot be used (no folder, empty segment) is returned as a
    warning with no moves, so the editor can show it while typing.
    """
    try:
        compiled = compile_text(text, structure=False)
    except TemplateStructureError as exc:
        return TemplatePreview([], [], [str(exc)])
    st = state()
    folder_label = compiled is not None and any(_uses_label(seg) for seg in compiled.folders)
    targets: list[AlbumTarget] = []
    moves = []
    no_release = no_label = 0
    collisions: list[Collision] = []
    for aid in album_ids:
        try:
            t = album_target(int(aid), None, compiled, st=st)
        except NotFound:
            raise
        except MMError:
            no_release += 1
            continue
        targets.append(t)
        files = t.files
        moves += files if files else [(t.album.path, t.target)]
        if folder_label and t.label_missing:
            no_label += 1
        seen: dict[str, int] = {}
        for _before, after in files:
            seen[str(after).casefold()] = seen.get(str(after).casefold(), 0) + 1
        collisions += [Collision(after, [t.album_id]) for _b, after in files if seen[str(after).casefold()] > 1
                       and not any(c.target == after for c in collisions)]
    cross = cross_album_collisions(targets)
    by_target: dict[str, list[int]] = {}
    for t in targets:
        if t.album_id in cross:
            by_target.setdefault(str(t.target), []).append(t.album_id)
    collisions = [Collision(next(t.target for t in targets if str(t.target) == key), sorted(ids))
                  for key, ids in by_target.items()] + collisions
    warnings = []
    if no_label:
        warnings.append(f"{no_label} albums have no label: {UNKNOWN_LABEL}/" if no_label != 1
                        else f"1 album has no label: {UNKNOWN_LABEL}/")
    if no_release:
        warnings.append(f"{no_release} album(s) have no release yet and are not shown")
    return TemplatePreview(moves, collisions, warnings)


def _uses_label(segment) -> bool:
    from ..naming.templates import _Field, _Func

    for node in segment:
        if isinstance(node, _Field) and node.name == "label":
            return True
        if isinstance(node, _Func) and any(_uses_label(arg) for arg in node.args):
            return True
    return False


__all__ = ["delete", "list", "preview", "save"]
