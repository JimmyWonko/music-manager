"""User configuration: $XDG_CONFIG_HOME/music-manager/config.toml.

Holds the settings (library roots, active naming template, toggles) and the
user's naming templates. There are no built-in library paths: library_roots
is empty until the user picks one.
"""

from __future__ import annotations

import json
import os
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "music-manager"


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "music-manager"


class ConfigFileError(ValueError):
    """config.toml exists but cannot be read."""


@dataclass
class Config:
    library_roots: list[Path] = field(default_factory=list)
    active_template: str = "archivist"
    sequential_sides: bool = False
    save_cover_art: bool = True
    clear_other_tags: bool = False
    templates: dict[str, tuple[str, str]] = field(default_factory=dict)  # id -> (name, text), user templates

    BOOLS = ("sequential_sides", "save_cover_art", "clear_other_tags")


def load_config(path: Path) -> Config:
    if not path.exists():
        return Config()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigFileError(f"cannot read {path}: {exc}") from exc
    cfg = Config()
    roots = data.get("library_roots", [])
    if not isinstance(roots, list) or not all(isinstance(r, str) for r in roots):
        raise ConfigFileError(f"{path}: library_roots must be a list of paths")
    cfg.library_roots = [Path(r) for r in roots]
    if isinstance(data.get("active_template"), str):
        cfg.active_template = data["active_template"]
    for name in Config.BOOLS:
        if name in data:
            if not isinstance(data[name], bool):
                raise ConfigFileError(f"{path}: {name} must be true or false")
            setattr(cfg, name, data[name])
    for tid, entry in (data.get("templates") or {}).items():
        if isinstance(entry, dict) and isinstance(entry.get("name"), str) and isinstance(entry.get("text"), str):
            cfg.templates[tid] = (entry["name"], entry["text"])
    return cfg


def _q(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)  # a JSON string is a valid TOML basic string


def dump_config(cfg: Config) -> str:
    lines = [
        "# music-manager settings (written by the app; comments are not kept)",
        "library_roots = [" + ", ".join(_q(str(r)) for r in cfg.library_roots) + "]",
        f"active_template = {_q(cfg.active_template)}",
    ]
    for name in Config.BOOLS:
        lines.append(f"{name} = {'true' if getattr(cfg, name) else 'false'}")
    for tid, (name, text) in cfg.templates.items():
        lines += ["", f"[templates.{_q(tid)}]", f"name = {_q(name)}", f"text = {_q(text)}"]
    return "\n".join(lines) + "\n"


def save_config(cfg: Config, path: Path) -> None:
    """Atomic write: readers see the old or the complete new file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(dump_config(cfg))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
