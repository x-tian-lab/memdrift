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

# Windows drive prefix, e.g. ``C:\Users\alice\vault``. Paths are normalised to
# forward slashes before matching, so this regex works on both spellings.
WIN_PATH_RE = re.compile(r"[A-Za-z]:/(?:[^/\s\"'<>|]+/)*[^/\s\"'<>|]*")
# POSIX home prefix, e.g. ``/home/alice/vault`` or ``/Users/alice/vault``.
NIX_HOME_RE = re.compile(r"/(?:home|Users)/[^/\s]+(?:/[^/\s\"'<>|]*)*")
# Either shape, anywhere in a string — not just at position 0. A path pasted
# into the middle of a sentence ("Source: C:\Users\...") must be redacted too.
ABS_PATH_RE = re.compile(f"(?:{WIN_PATH_RE.pattern}|{NIX_HOME_RE.pattern})")
# Bare file URLs pointing at a local disk.
FILE_URL_RE = re.compile(r"file:///[^\s\"'<>]*")

PRIVATE_KEYS = ("private", "confidential", "secret")


def _shorten_one(path: str, root_posix: str) -> str:
    """One absolute path → a harmless relative-looking one."""
    if root_posix and path.startswith(root_posix):
        # Inside the vault: keep the structure the human chose, drop the disk.
        return "~" + path[len(root_posix) :]
    # A different machine (or a path under someone else's account). First
    # drop the drive + ``/Users/<name>`` (or ``/home/<name>``) prefix — that
    # is where the OS account name lives. Then keep only the last two
    # segments: Change values are *previews* truncated to 72 chars, so the
    # path may end mid-directory and cannot be matched against the root, and
    # the middle (`AppData/Local/Temp/...`) is machine-specific anyway.
    home = re.match(r"^(?:[A-Za-z]:)?/(?:home|Users)/[^/]+", path)
    rest = path[home.end() :] if home else path
    parts = [seg for seg in rest.split("/") if seg]
    # `preview()` may have cut the path *mid-segment*; the ellipsis is the
    # marker. Two trailing segments would then be `AppData/Loc…` — still a
    # machine-specific directory name. A truncated path is useless to a
    # reader anyway, so drop the whole tail rather than guess.
    if "…" in path:
        return "~/…"
    tail = "/".join(parts[-2:]) if parts else ""
    return "~/" + tail if tail else "~/…"


def _shorten_path(raw: str, root: str | None) -> str:
    """Strip the real root and any home prefix. Never invents a path."""
    text = raw.replace("\\", "/")
    root_posix = root.replace("\\", "/").rstrip("/") if root else ""
    # file:// URLs are neutered first: otherwise the URL regex and the
    # absolute-path regex would both claim the same characters.
    text = FILE_URL_RE.sub("file://", text)
    return ABS_PATH_RE.sub(lambda m: _shorten_one(m.group(0), root_posix), text)


def redact_text(text: str | None, root: str | None = None, limit: int | None = None) -> str | None:
    """Remove machine-specific paths and optionally clamp length.

    Idempotent: redacting already-redacted text is a no-op, which lets the
    server call it again on cached events without mangling them. ``root=None``
    means "unknown vault location", *not* "do not redact" — every absolute
    path still loses its account name. Callers that genuinely want paths kept
    must disable the :class:`Redactor` instead.
    """
    if text is None:
        return None
    out = _shorten_path(text, root)
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
    #: Set False only by an explicit ``--no-redact``. Distinguishing "vault
    #: location unknown" from "do not redact" is what keeps that flag honest:
    #: a None root still scrubs account names, a disabled redactor does not.
    enabled: bool = True
    #: Pages excluded for being private; surfaced so the CLI can say *why*
    #: something is missing instead of silently showing less.
    hidden_paths: set[str] = field(default_factory=set)

    def path(self, raw: str | None) -> str | None:
        if raw is None or not self.enabled:
            return raw
        return redact_text(raw, root=self.root)

    def text(self, raw: Any) -> Any:
        """Any scalar that may carry a path or a home directory.

        Non-strings pass through untouched: a number cannot leak a username,
        and coercing it here would quietly change an API between surfaces.
        """
        if isinstance(raw, str) and self.enabled:
            return redact_text(raw, root=self.root)
        return raw

    def evidence(self, items: Iterable[dict] | None) -> list[dict]:
        out: list[dict] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            ref = item.get("ref")
            clean = {
                "kind": item.get("kind"),
                "ref": redact_text(ref, root=self.root) if self.enabled else ref,
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
        if not self.enabled:
            return dict(change)
        clean = dict(change)
        clean["path"] = self.path(change.get("path"))
        if clean.get("from"):
            clean["from"] = self.path(change["from"])
        for key in ("before", "after"):
            if isinstance(clean.get(key), str):
                clean[key] = redact_text(clean[key], root=self.root)
        return clean
