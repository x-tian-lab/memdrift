"""Markdown → structured page.

The whole point of memdrift is that a change is not "line 42 changed" but
"clients/acme.md · frontmatter.pricing: $45k → $48k". So parsing has to lift a
file into addressable fields:

* ``frontmatter.<dotted.path>`` — nested YAML flattened
* ``section:<heading>``         — body under a markdown heading
* ``body``                      — anything outside a heading (preamble)

Anything we cannot attribute to a field degrades to ``body`` and is reported at
low precision, per the PRD's risk plan.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

import frontmatter

HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
WS_RE = re.compile(r"\s+")

INTRO_KEY = "(intro)"


def _norm_heading(text: str) -> str:
    return WS_RE.sub(" ", text.strip())


def _scalar(value: Any) -> Any:
    """Normalise YAML types so they compare and serialize predictably."""
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_scalar(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _scalar(v) for k, v in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def flatten_meta(obj: dict, prefix: str = "") -> dict[str, Any]:
    """{'a': {'b': 1}} → {'a.b': 1}. Lists stay whole — order is meaning."""
    out: dict[str, Any] = {}
    for key, value in obj.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(flatten_meta(value, dotted + "."))
        else:
            out[dotted] = _scalar(value)
    return out


def split_sections(body: str) -> tuple[str, dict[str, str]]:
    """Split markdown body into (preamble, {heading: text})."""
    preamble: list[str] = []
    sections: dict[str, str] = {}
    buf: list[str] = []
    current: str | None = None

    def flush() -> None:
        nonlocal buf
        if current is None:
            preamble.extend(buf)
        else:
            key = current
            n = 2
            while key in sections:  # duplicate heading: keep both, suffix it
                key = f"{current} #{n}"
                n += 1
            sections[key] = "\n".join(buf).strip("\n")
        buf = []

    for line in body.splitlines():
        match = HEADING_RE.match(line)
        if match:
            flush()
            current = _norm_heading(match.group(2))
        else:
            buf.append(line)
    flush()
    return "\n".join(preamble).strip("\n"), sections


@dataclass
class ParsedPage:
    path: str
    fields: dict[str, Any] = field(default_factory=dict)
    sections: dict[str, str] = field(default_factory=dict)
    preamble: str = ""
    raw: str = ""
    frontmatter_ok: bool = True

    @property
    def lines(self) -> list[str]:
        return self.raw.splitlines()

    @property
    def section_names(self) -> list[str]:
        return list(self.sections)

    def field_text(self, dotted: str) -> str | None:
        """Human-readable value for a field path, used by diff output.

        Accepts the change-record spelling (``frontmatter.pricing``) as well as
        the bare flattened key (``pricing``); ``fields`` itself holds the bare
        form, so the prefix has to come off before the lookup.
        """
        if dotted.startswith("section:"):
            return self.sections.get(dotted[len("section:") :])
        if dotted == "body":
            return self.preamble
        key = dotted[len("frontmatter.") :] if dotted.startswith("frontmatter.") else dotted
        if key in self.fields:
            value = self.fields[key]
            if isinstance(value, list):
                return ", ".join(str(v) for v in value)
            return "" if value is None else str(value)
        return None


def parse_page(path: str, text: str) -> ParsedPage:
    fields: dict[str, Any] = {}
    frontmatter_ok = True
    body = text

    if text.lstrip().startswith("---"):
        try:
            post = frontmatter.loads(text)
            fields = flatten_meta(dict(post.metadata))
            body = post.content
        except Exception:  # broken YAML must not break the snapshot
            frontmatter_ok = False
            body = text

    preamble, sections = split_sections(body)
    return ParsedPage(
        path=path,
        fields=fields,
        sections=sections,
        preamble=preamble,
        raw=text,
        frontmatter_ok=frontmatter_ok,
    )
