"""Naming templates: a small Picard-style engine and the built-in presets.

Syntax: literal text, %field% and $function(arg,...). A backslash escapes
the next character ("\\$", "\\%", "\\,", "\\(", "\\)", "\\/", "\\\\"). A "/" at
top level separates path segments: every segment but the last is a folder,
the last is the file name; the engine adds the extension.

An empty field drops along with its surrounding brackets, its connector
(" • ", " ⁄ ", ", ", " - ", " – ") and its spacing, so
"[%leadyear%]  %album% [%reissueyear%]" renders "My War" when neither year
is known. In a folder segment an empty %label% becomes "Unknown label".

Folder segments are rendered once per album: track fields (%title%,
%tracknumber%, ...) are empty there.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from . import format_album_title, format_artist, format_catalog, sanitize_filename

ALBUM_FIELDS = (
    "albumartist", "album", "leadyear", "originalyear", "reissueyear", "catno", "catno_raw", "label",
    "format", "disctotal",
)
TRACK_FIELDS = ("title", "artist", "tracknumber", "position", "discnumber")
FIELDS = ALBUM_FIELDS + TRACK_FIELDS
FUNCTIONS = {"if": (2, 3), "if2": (1, 99), "left": (2, 2), "num": (2, 2), "lower": (1, 1), "upper": (1, 1)}
UNKNOWN_LABEL = "Unknown label"

# (id, name, text): templates.list() order. The source of truth is API-CONTRACT.md.
PRESETS: tuple[tuple[str, str, str], ...] = (
    ("archivist", "Archivist (default)",
     "%albumartist%/[%leadyear%]  %album% [%reissueyear%] [%catno%]/%position%. %title%"),
    ("simple", "Simple", "%albumartist%/%album% (%originalyear%)/$num(%tracknumber%,2) - %title%"),
    ("edition-detail", "Edition detail",
     "%albumartist%/%album% (%originalyear% \u2044 %reissueyear%) [%catno% \u2022 %format%]/"
     "$num(%tracknumber%,2) %title%"),
    ("label-archive", "Label archive", "%label%/[%catno%] %albumartist% - %album% (%leadyear%)/%position%. %title%"),
)
PRESET_IDS = tuple(p[0] for p in PRESETS)

_EMPTY = "\x00"  # stands for an empty field until the segment is cleaned up
_CONNECTORS = "\u2022\u2044,\u2013\\-"  # • ⁄ , – -
_CATNO_RAW_BAD = re.compile(r'[/\\:*?"<>|]')


class TemplateError(ValueError):
    """A template that cannot be used. field names the offending field or function ("" for syntax)."""

    def __init__(self, message: str, field: str = ""):
        super().__init__(message)
        self.field = field


# --- parsing --------------------------------------------------------------

@dataclass(frozen=True)
class _Lit:
    text: str


@dataclass(frozen=True)
class _Field:
    name: str


@dataclass(frozen=True)
class _Func:
    name: str
    args: tuple[tuple, ...]  # each arg is a tuple of nodes


_SEP = object()  # top-level "/"


class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.i = 0

    def parse(self) -> list:
        nodes = self._sequence(top=True, stop="")
        if self.i < len(self.text):
            raise TemplateError(f"unexpected {self.text[self.i]!r} at position {self.i + 1}")
        return nodes

    def _sequence(self, top: bool, stop: str) -> list:
        nodes: list = []
        buf: list[str] = []

        def flush():
            if buf:
                nodes.append(_Lit("".join(buf)))
                buf.clear()

        t = self.text
        while self.i < len(t):
            ch = t[self.i]
            if ch == "\\":
                if self.i + 1 >= len(t):
                    raise TemplateError("template ends with a lone backslash")
                buf.append(t[self.i + 1])
                self.i += 2
            elif ch in stop:
                break
            elif ch == "%":
                end = t.find("%", self.i + 1)
                if end < 0:
                    raise TemplateError(f"unclosed %field at position {self.i + 1}")
                name = t[self.i + 1:end]
                if not re.fullmatch(r"_?[a-z][a-z0-9_]*", name):
                    raise TemplateError(f"bad field name %{name}%", field=name)
                if name not in FIELDS:
                    raise TemplateError(f"unknown field %{name}%", field=name)
                flush()
                nodes.append(_Field(name))
                self.i = end + 1
            elif ch == "$":
                m = re.compile(r"\$([a-z0-9_]+)\(").match(t, self.i)
                if not m:
                    raise TemplateError(f"expected $function( at position {self.i + 1}")
                name = m.group(1)
                if name not in FUNCTIONS:
                    raise TemplateError(f"unknown function ${name}", field=name)
                self.i = m.end()
                args = []
                while True:
                    args.append(tuple(self._sequence(top=False, stop=",)")))
                    if self.i >= len(t):
                        raise TemplateError(f"unclosed ${name}(", field=name)
                    if t[self.i] == ")":
                        self.i += 1
                        break
                    self.i += 1  # ","
                low, high = FUNCTIONS[name]
                if not low <= len(args) <= high:
                    raise TemplateError(f"${name} takes {low}" + (f"-{high}" if high != low else "")
                                        + f" arguments, got {len(args)}", field=name)
                flush()
                nodes.append(_Func(name, tuple(args)))
            elif ch == "/" and top:
                flush()
                nodes.append(_SEP)
                self.i += 1
            else:
                buf.append(ch)
                self.i += 1
        flush()
        return nodes


def _blank(value: str) -> bool:
    return not value.replace(_EMPTY, "").strip()


def _eval(nodes, fields: dict[str, str]) -> str:
    out: list[str] = []
    for node in nodes:
        if isinstance(node, _Lit):
            out.append(node.text)
        elif isinstance(node, _Field):
            value = fields.get(node.name, "")
            out.append(value if value else _EMPTY)
        else:
            out.append(_call(node, fields))
    return "".join(out)


def _call(node: _Func, fields: dict[str, str]) -> str:
    args = node.args
    if node.name == "if":
        cond = _eval(args[0], fields)
        if not _blank(cond):
            return _eval(args[1], fields)
        return _eval(args[2], fields) if len(args) > 2 else ""
    if node.name == "if2":
        for arg in args:
            value = _eval(arg, fields)
            if not _blank(value):
                return value
        return _EMPTY
    values = [_eval(a, fields) for a in args]
    if node.name == "lower":
        return values[0].lower()
    if node.name == "upper":
        return values[0].upper()
    if node.name == "left":
        n = values[1].replace(_EMPTY, "").strip()
        if not n.isdigit():
            return values[0]
        text = values[0].replace(_EMPTY, "")
        return text[: int(n)] if text else _EMPTY
    if node.name == "num":
        text = values[0].replace(_EMPTY, "").strip()
        width = values[1].replace(_EMPTY, "").strip()
        if not text:
            return _EMPTY
        if text.isdigit() and width.isdigit():
            return text.zfill(int(width))
        return text
    raise TemplateError(f"unknown function ${node.name}", field=node.name)  # pragma: no cover


_DROP_CONNECTOR_BEFORE = re.compile(rf"\s*[{_CONNECTORS}]\s*{_EMPTY}")
_DROP_CONNECTOR_AFTER = re.compile(rf"{_EMPTY}\s*[{_CONNECTORS}]\s*")
_EMPTY_GROUP = re.compile(rf"(\s*)([\[(])([\s{_CONNECTORS}{_EMPTY}]*)([\])])(\s*)")


def _clean(segment: str) -> str:
    """Drop empty fields with their connectors, brackets and spacing."""
    if _EMPTY not in segment:
        return segment
    prev = None
    while prev != segment:
        prev = segment
        segment = _DROP_CONNECTOR_BEFORE.sub(_EMPTY, segment, count=1) if _DROP_CONNECTOR_BEFORE.search(
            segment) else _DROP_CONNECTOR_AFTER.sub(_EMPTY, segment, count=1)

        def group(m: re.Match) -> str:
            if _EMPTY not in m.group(3):
                return m.group(0)
            before, after = m.group(1), m.group(5)
            # keep one side's spacing so neighbours stay apart
            if before and after:
                return before if m.end() < len(m.string) else ""
            return ""
        segment = _EMPTY_GROUP.sub(group, segment)
    segment = re.sub(rf"\s*{_EMPTY}\s*", lambda m: " " if m.start() and m.end() < len(segment) and
                     " " in m.group(0) else "", segment)
    return segment.replace(_EMPTY, "").strip()


@dataclass(frozen=True)
class CompiledTemplate:
    text: str
    folders: tuple[tuple, ...]  # node lists, one per folder segment
    file: tuple

    def uses(self, name: str) -> bool:
        def walk(nodes):
            for n in nodes:
                if isinstance(n, _Field) and n.name == name:
                    return True
                if isinstance(n, _Func) and any(walk(a) for a in n.args):
                    return True
            return False
        return any(walk(seg) for seg in (*self.folders, self.file))

    def folder_parts(self, album: dict[str, str]) -> list[str]:
        fields = dict(album)
        for name in TRACK_FIELDS:
            fields[name] = ""
        if not fields.get("label"):
            fields["label"] = UNKNOWN_LABEL
        return [sanitize_filename(_clean(_eval(seg, fields))) for seg in self.folders]

    def file_name(self, album: dict[str, str], track: dict[str, str], suffix: str) -> str:
        fields = {**album, **track}
        return sanitize_filename(_clean(_eval(self.file, fields))) + suffix.lower()


def compile_template(text: str) -> CompiledTemplate:
    """Parse and check a template; raises TemplateError naming the bad field or function."""
    if not text or not text.strip():
        raise TemplateError("the template is empty", field="text")
    nodes = _Parser(text).parse()
    segments: list[list] = [[]]
    for node in nodes:
        if node is _SEP:
            segments.append([])
        else:
            segments[-1].append(node)
    if len(segments) < 2:
        raise TemplateError("the template needs at least one folder (use / between parts)", field="text")
    if any(not seg for seg in segments):
        raise TemplateError("the template has an empty path segment (// or a leading or trailing /)", field="text")
    if not any(isinstance(n, (_Field, _Func)) for n in segments[-1]):
        raise TemplateError("the file name part needs a field such as %title%", field="text")
    return CompiledTemplate(text, tuple(tuple(s) for s in segments[:-1]), tuple(segments[-1]))


# --- field values -----------------------------------------------------------

def first_catalog_raw(catno: str | None) -> str:
    """The first catalog number as Discogs lists it, with only file-name-unsafe characters replaced."""
    if not catno:
        return ""
    first = re.split(r";\s*", catno.strip())[0].strip()
    if first.casefold() in {"", "none", "[none]"}:
        return ""
    return _CATNO_RAW_BAD.sub("_", first)


def format_field(formats: list[dict] | None) -> str:
    """'CD', '2xCD', 'Vinyl' ... from the release's first format."""
    if not formats:
        return ""
    first = formats[0]
    name = (first.get("name") or "").strip()
    qty = str(first.get("qty") or "1").strip()
    if name and qty.isdigit() and int(qty) > 1:
        return f"{qty}x{name}"
    return name


def album_fields(*, album_artist: str, title: str, lead_year: str, original_year: str, reissue_year: str,
                 catno: str, label: str, formats: list[dict] | None, discs: int) -> dict[str, str]:
    return {
        "albumartist": format_artist(album_artist),
        "album": format_album_title(title),
        "leadyear": lead_year,
        "originalyear": original_year or lead_year,
        "reissueyear": reissue_year,
        "catno": format_catalog(catno) or "",
        "catno_raw": first_catalog_raw(catno),
        "label": (label or "").strip(),
        "format": format_field(formats),
        "disctotal": str(discs or ""),
    }


def track_fields(*, title: str, artist: str, number: int, position: str, disc: int) -> dict[str, str]:
    return {
        "title": title,
        "artist": artist,
        "tracknumber": str(number) if number else "",
        "position": position,
        "discnumber": str(disc or ""),
    }


Namer = Callable[[dict[str, str], dict[str, str], str], str]
