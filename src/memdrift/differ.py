"""Structured diff: two snapshots → a list of *field-level* changes.

Deliberately not ``git diff``. The product claim is that the digest can say
"客户 Acme 的报价从 $45k 改成 $48k", which is only possible if we attribute a
text change to a field before we store it.

Precision ladder (worst case is explicit, never silent):
1. frontmatter dotted key
2. ``section:<heading>``
3. ``body`` — heading structure collapsed, flagged ``low_precision``
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from difflib import SequenceMatcher
from typing import Any

from .parse import ParsedPage, parse_page

WS_RE = re.compile(r"\s+")
PREVIEW_LEN = 72


def preview(text: str | None, limit: int = PREVIEW_LEN) -> str | None:
    if text is None:
        return None
    flat = WS_RE.sub(" ", text).strip()
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"


def change_ratio(old_text: str, new_text: str) -> float:
    """0.0 = identical, 1.0 = nothing in common (line based)."""
    if old_text == new_text:
        return 0.0
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    if not old_lines and not new_lines:
        return 0.0
    if not old_lines or not new_lines:
        return 1.0
    return round(1.0 - SequenceMatcher(None, old_lines, new_lines).ratio(), 4)


@dataclass
class Change:
    path: str
    op: str  # add | modify | delete | rename
    field: str | None = None
    before: Any = None
    after: Any = None
    section: str | None = None
    change_ratio: float = 0.0
    sections: list[str] = dc_field(default_factory=list)
    rename_from: str | None = None
    low_precision: bool = False
    section_rename: bool = False

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"path": self.path, "op": self.op}
        if self.rename_from:
            out["from"] = self.rename_from
        if self.op == "add":
            out["sections"] = self.sections
        if self.field is not None:
            out["field"] = self.field
            out["before"] = self.before
            out["after"] = self.after
            out["section"] = self.section
            out["change_ratio"] = self.change_ratio
        if self.low_precision:
            out["low_precision"] = True
        if self.section_rename:
            out["section_rename"] = True
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Change":
        return cls(
            path=data.get("path", ""),
            op=data.get("op", "modify"),
            field=data.get("field"),
            before=data.get("before"),
            after=data.get("after"),
            section=data.get("section"),
            change_ratio=float(data.get("change_ratio") or 0.0),
            sections=list(data.get("sections") or []),
            rename_from=data.get("from"),
            low_precision=bool(data.get("low_precision")),
            section_rename=bool(data.get("section_rename")),
        )

    def label(self) -> str:
        """Field label for humans: 'frontmatter.pricing' → 'pricing'."""
        if not self.field:
            return ""
        if self.field.startswith("frontmatter."):
            return self.field[len("frontmatter.") :]
        if self.field.startswith("section:"):
            return self.field[len("section:") :]
        return self.field

    def describe(self) -> str:
        if self.op == "add":
            n = len(self.sections)
            return f"new page ({n} section{'s' if n != 1 else ''})"
        if self.op == "delete":
            return "page deleted"
        if self.op == "rename":
            return f"renamed from {self.rename_from}" if not self.section_rename else f"heading renamed from {self.before}"
        before = preview(str(self.before)) if self.before not in (None, "") else "∅"
        after = preview(str(self.after)) if self.after not in (None, "") else "∅"
        return f"{self.label()}: {before} → {after}"


def _diff_fields(old: ParsedPage, new: ParsedPage, ratio: float) -> list[Change]:
    out: list[Change] = []
    for key in sorted(set(old.fields) | set(new.fields)):
        if key not in old.fields:
            out.append(
                Change(new.path, "modify", f"frontmatter.{key}", None,
                       new.fields[key], None, ratio)
            )
        elif key not in new.fields:
            out.append(
                Change(new.path, "modify", f"frontmatter.{key}",
                       old.fields[key], None, None, ratio)
            )
        elif old.fields[key] != new.fields[key]:
            out.append(
                Change(new.path, "modify", f"frontmatter.{key}",
                       old.fields[key], new.fields[key], None, ratio)
            )
    return out


def _section_body(text: str) -> str:
    return WS_RE.sub(" ", text).strip()


def _pair_renamed_sections(old: ParsedPage, new: ParsedPage) -> dict[str, str]:
    """Match a vanished heading to an appeared one when the body is identical.

    Renaming ``## 周会纪要 11/21`` → ``## 周会纪要 11/22`` is one edit, not a
    delete plus an add. Bodies are compared first, then heading similarity.
    """
    disappeared = [n for n in old.sections if n not in new.sections]
    appeared = [n for n in new.sections if n not in old.sections]
    if not disappeared or not appeared:
        return {}

    pairs: dict[str, str] = {}
    used: set[str] = set()
    # pass 1: identical body → unambiguous rename
    by_body: dict[str, list[str]] = {}
    for name in appeared:
        by_body.setdefault(_section_body(new.sections[name]), []).append(name)
    for name in disappeared:
        candidates = by_body.get(_section_body(old.sections[name]), [])
        free = [c for c in candidates if c not in used]
        if len(free) == 1:
            pairs[name] = free[0]
            used.add(free[0])

    # pass 2: similar heading, similar-ish body
    for name in disappeared:
        if name in pairs:
            continue
        best, best_score = None, 0.0
        for cand in appeared:
            if cand in used:
                continue
            heading_score = SequenceMatcher(None, name, cand).ratio()
            if heading_score >= 0.6:
                body_score = SequenceMatcher(
                    None, _section_body(old.sections[name]), _section_body(new.sections[cand])
                ).ratio()
                score = heading_score * 0.4 + body_score * 0.6
                if score > best_score:
                    best, best_score = cand, score
        if best and best_score >= 0.7:
            pairs[name] = best
            used.add(best)
    return pairs


def _diff_sections(old: ParsedPage, new: ParsedPage, ratio: float) -> list[Change]:
    out: list[Change] = []
    renamed = _pair_renamed_sections(old, new)
    new_to_old = {v: k for k, v in renamed.items()}

    for old_name, new_name in renamed.items():
        out.append(
            Change(
                new.path,
                "rename",
                field=f"section:{new_name}",
                before=old_name,
                after=new_name,
                section=new_name,
                change_ratio=ratio,
                section_rename=True,
            )
        )
        # a rename plus a body edit is still one thing the user cares about
        if old.sections[old_name] != new.sections[new_name]:
            out.append(
                Change(
                    new.path,
                    "modify",
                    f"section:{new_name}",
                    preview(old.sections[old_name]),
                    preview(new.sections[new_name]),
                    new_name,
                    ratio,
                )
            )

    for name in new.sections:
        if name in new_to_old:
            continue
        if name not in old.sections:
            out.append(
                Change(new.path, "modify", f"section:{name}", None,
                       preview(new.sections[name]), name, ratio)
            )
        elif old.sections[name] != new.sections[name]:
            out.append(
                Change(new.path, "modify", f"section:{name}",
                       preview(old.sections[name]), preview(new.sections[name]),
                       name, ratio)
            )
    for name in old.sections:
        if name in renamed:
            continue
        if name not in new.sections:
            out.append(
                Change(new.path, "modify", f"section:{name}",
                       preview(old.sections[name]), None, name, ratio)
            )
    return out


def _looks_reshuffled(old: ParsedPage, new: ParsedPage) -> bool:
    """Headings all moved around → field attribution is guesswork."""
    old_names, new_names = set(old.sections), set(new.sections)
    if not old_names or not new_names:
        return False
    churn = len(old_names - new_names) + len(new_names - old_names)
    overlap = len(old_names & new_names)
    return churn >= 3 and overlap <= max(1, int(len(old_names) * 0.3))


def diff_pages(old: ParsedPage | None, new: ParsedPage | None) -> list[Change]:
    if old is None and new is None:
        return []
    if old is None:
        assert new is not None
        return [
            Change(
                new.path,
                "add",
                change_ratio=1.0,
                sections=new.section_names,
            )
        ]
    if new is None:
        return [Change(old.path, "delete", change_ratio=1.0)]

    ratio = change_ratio(old.raw, new.raw)
    if ratio == 0.0:
        return []

    if _looks_reshuffled(old, new):
        return [
            Change(
                new.path,
                "modify",
                field="body",
                before=preview(old.raw),
                after=preview(new.raw),
                change_ratio=ratio,
                low_precision=True,
            )
        ]

    changes = _diff_fields(old, new, ratio) + _diff_sections(old, new, ratio)

    if old.preamble != new.preamble:
        changes.append(
            Change(
                new.path,
                "modify",
                field="body",
                before=preview(old.preamble),
                after=preview(new.preamble),
                change_ratio=ratio,
            )
        )

    if not changes and ratio > 0:  # whitespace-only or outside-field drift
        changes.append(
            Change(
                new.path,
                "modify",
                field="body",
                before=preview(old.raw),
                after=preview(new.raw),
                change_ratio=ratio,
                low_precision=True,
            )
        )
    return changes


def diff_trees(
    old_tree: dict[str, str],
    new_tree: dict[str, str],
    read_text,
) -> list[Change]:
    """Compare two trees ({path: blob_id}); ``read_text(blob_id) -> str``."""
    added = [p for p in new_tree if p not in old_tree]
    removed = [p for p in old_tree if p not in new_tree]
    changes: list[Change] = []

    # rename = same content, different path
    new_by_blob = {new_tree[p]: p for p in added}
    renamed: set[str] = set()
    for path in sorted(removed):
        twin = new_by_blob.get(old_tree[path])
        if twin and twin not in renamed:
            renamed.add(twin)
            changes.append(
                Change(twin, "rename", change_ratio=0.0, rename_from=path)
            )

    for path in sorted(added):
        if path in renamed:
            continue
        page = parse_page(path, read_text(new_tree[path]))
        changes.extend(diff_pages(None, page))

    for path in sorted(removed):
        if any(c.rename_from == path for c in changes):
            continue
        page = parse_page(path, read_text(old_tree[path]))
        changes.extend(diff_pages(page, None))

    for path in sorted(set(old_tree) & set(new_tree)):
        if old_tree[path] == new_tree[path]:
            continue
        old_page = parse_page(path, read_text(old_tree[path]))
        new_page = parse_page(path, read_text(new_tree[path]))
        changes.extend(diff_pages(old_page, new_page))

    return changes
