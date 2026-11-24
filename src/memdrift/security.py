"""Everything that decides what a *stranger* is allowed to see.

The threat model is not "someone hacks the process". It is the boring, common
one: you paste a daily digest into a group chat, or hand a screenshare to a
client. At that moment three things leak by default if nobody stops them.

1. **Absolute paths.** ``C:\\Users\\alice\\vault\\clients\\acme.md`` tells a
   stranger the OS account name. Redacted to ``~/vault/clients/acme.md``.
2. **Verbatim quotes.** ``evidence[].quote`` is the feature (it proves *why*
   the agent changed the price) and the liability (it re-copies a private
   conversation). Truncated, and droppable with ``--no-quote``.
3. **Pages marked private.** A page with ``private: true`` in its frontmatter
   must not appear — and neither may any evidence that points at it, otherwise
   the leak just moves one hop.

Redaction happens on the *event stream*, not in the renderer. That is the whole
design: ``digest`` and ``serve`` share the same events, so they cannot drift
into "one of them forgot to redact".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

#: How much of an evidence quote survives into output, before truncation.
QUOTE_LIMIT = 120

# Windows drive prefix, e.g. ``C:\Users\alice\vault``.
WIN_PATH_RE = re.compile(r"[A-Za-z]:\\(?:[^\\\s\"'<>|]+\\)*[^\\\s\"'<>|]*")
# POSIX home prefix, e.g. ``/home/alice/vault`` or ``/Users/alice/vault``.
NIX_HOME_RE = re.compile(r"/(?:home|Users)/[^/\s]+")
# Bare file URLs pointing at a local disk.
FILE_URL_RE = re.compile(r"file:///[^\s\"'<>]*")

PRIVATE_KEYS = ("private", "confidential", "secret")


def _shorten_path(raw: str, root: str | None) -> str:
    """Strip the real root and any home prefix. Never invents a path."""
    text = raw.replace("\\", "/")
    if root:
        root_posix = root.replace("\\", "/").rstrip("/")
        if root_posix and text.startswith(root_posix):
            text = "~" + text[len(root_posix) :]
    # Whatever absolute prefix survives (a different machine, a symlink) goes.
    text = NIX_HOME_RE.sub("~", text)
    text = re.sub(r"^[A-Za-z]:/", "~/", text)
    return text


def redact_text(text: str | None, root: str | None = None, limit: int | None = None) -> str | None:
    """Remove machine-specific paths and optionally clamp length.

    Idempotent: redacting already-redacted text is a no-op, which lets the
    server call it again on cached events without mangling them.
    """
    if text is None:
        return None
    out = _shorten_path(text, root) if (root or re.search(r"[A-Za-z]:\\|/(?:home|Users)/", text)) else text
    out = FILE_URL_RE.sub("file://", out) if FILE_URL_RE.search(out) else out
    if limit is not None and len(out) > limit:
        out = out[: limit - 1].rstrip() + "…"
    return out


def truncate_quote(text: str | None, limit: int = QUOTE_LIMIT) -> str | None:
    """Quotes carry a marker when cut, so nobody mistakes a fragment for the whole."""
    if text is None:
        return None
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def is_private(fields: dict[str, Any]) -> bool:
    """``private: true`` / ``confidential: yes`` in frontmatter marks a page."""
    for key in PRIVATE_KEYS:
        value = fields.get(key)
        if value is True:
            return True
        if isinstance(value, str) and value.strip().lower() in {"true", "yes", "1"}:
            return True
    return False


@dataclass
class Redactor:
    """Applies the rules above to event dicts, in place-safe fashion."""

    root: str | None = None
    keep_quotes: bool = True
    quote_limit: int = QUOTE_LIMIT
    #: Pages excluded for being private; surfaced so the CLI can say *why*
    #: something is missing instead of silently showing less.
    hidden_paths: set[str] = field(default_factory=set)

    def path(self, raw: str | None) -> str | None:
        if raw is None:
            return None
        return redact_text(raw, root=self.root)

    def evidence(self, items: Iterable[dict] | None) -> list[dict]:
        out: list[dict] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            clean = {
                "kind": item.get("kind"),
                "ref": redact_text(item.get("ref"), root=self.root),
            }
            if self.keep_quotes and item.get("quote"):
                clean["quote"] = truncate_quote(str(item["quote"]), self.quote_limit)
            elif not self.keep_quotes and item.get("quote"):
                clean["quote_omitted"] = True
            out.append(clean)
        return out

    def change(self, change: dict, *, private: bool = False) -> dict | None:
        """One change. Returns None when the page is private and ``drop`` is on."""
        if private:
            self.hidden_paths.add(str(change.get("path")))
            return None
        clean = dict(change)
        clean["path"] = self.path(change.get("path"))
        if clean.get("from"):
            clean["from"] = self.path(change["from"])
        for key in ("before", "after"):
            if isinstance(clean.get(key), str):
                clean[key] = redact_text(clean[key], root=self.root)
        return clean
