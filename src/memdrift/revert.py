"""Undo, without rewriting history.

The PRD's third gap was **Irreversible**: "one bad extraction poisons the page,
your only tool is a manual delete." Revert closes it, and the shape of the fix
matters as much as the feature.

**A revert is a new commit, never a rewrite.** Nothing that already happened is
edited or deleted. So `log` still tells the truth about what your agent did at
00:12, and `blame` still points at the commit that introduced the bad value —
it just also points at the commit that took it back out. Rewriting would make
the history lie, which is the exact failure this project exists to prevent.

Three granularities, because "undo" means different things at different times:

* ``--field frontmatter.pricing`` — one value on one page. The surgical case.
* ``--page clients/acme.md``     — one page back to a given commit.
* ``--commit 9f2c1ab``           — everything that commit did, undone.

Writes go through the same structured path as any other change, so a revert
produces a normal field-level `Change`; `diff` and `digest` render it with no
special case. YAML frontmatter is edited key by key (not re-serialised) so a
revert does not reformat a file or reorder keys the agent never touched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .differ import Change
from .operations import commit as commit_op
from .parse import flatten_meta, parse_page, split_sections
from .repo import Repo, RepoError, short

#: ``frontmatter.a.b`` / ``section:Notes`` / ``body``
FIELD_RE = re.compile(r"^(frontmatter\.[^:]+|section:.+|body)$")
FRONTMATTER_OPEN = "---"


@dataclass
class Plan:
    """What a revert *would* do. Computed with zero writes, always."""

    path: str
    field: str | None
    before: Any = None  # current value (what will be replaced)
    after: Any = None  # value to restore
    note: str = ""
    skipped: bool = False
    reason: str = ""
    #: The single commit this plan undoes, when there is exactly one. Carried
    #: into the new commit as ``reverts: <id>`` so the pair stays traceable in
    #: both directions without rewriting either.
    origin: str = ""


@dataclass
class RevertResult:
    plans: list[Plan] = field(default_factory=list)
    commit: dict | None = None
    written: list[str] = field(default_factory=list)

    @property
    def changes(self) -> int:
        return sum(1 for p in self.plans if not p.skipped)

    @property
    def skipped(self) -> list[Plan]:
        return [p for p in self.plans if p.skipped]


# --------------------------------------------------------------------- values


def _frontmatter_bounds(text: str) -> tuple[int, int] | None:
    """(start, end) line indexes of the YAML block, ``---`` lines included."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != FRONTMATTER_OPEN:
        return None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == FRONTMATTER_OPEN:
            return 0, idx
    return None


def _yaml_scalar(value: Any) -> str:
    """Enough YAML for the values a memory file actually holds.

    Deliberately not a full emitter: re-serialising the whole block would
    reformat keys and comments the agent never touched, which turns a
    one-field revert into a whole-page diff.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_yaml_scalar(v) for v in value) + "]"
    text = str(value)
    if text == "" or re.search(r"^[\s\-?:,\[\]{}#&*!|>'\"%@`]|: | #", text):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def _scalar_like(original: str, value: Any) -> str:
    """Match the quoting style the file already uses for this key.

    ``pricing: "$45k"`` must come back as ``pricing: "$45k"``, not
    ``pricing: $45k``: both are valid YAML with the same meaning, but a
    revert that reformats a byte it was not asked to touch turns a one-field
    undo into a whole-file diff, and the user has to review changes they
    never made.

    Only quoting is borrowed, never content — the *value* always comes from
    the snapshot, so this cannot keep a stale byte alive.
    """
    rendered = _yaml_scalar(value)
    if not isinstance(value, str) or not value:
        return rendered
    if rendered[:1] in {"'", '"'}:
        return rendered  # _yaml_scalar already had to quote it; leave it alone

    trimmed = original.strip()
    if len(trimmed) < 2:
        return rendered
    if trimmed[0] == trimmed[-1] == "'":
        return "'" + rendered.replace("'", "''") + "'"
    if trimmed[0] == trimmed[-1] == '"':
        escaped = rendered.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return rendered


def _set_frontmatter_key(text: str, dotted: str, value: Any) -> str:
    """Rewrite exactly one dotted key, leaving every other byte alone."""
    bounds = _frontmatter_bounds(text)
    if bounds is None:
        return _insert_frontmatter(text, dotted, value)
    start, end = bounds
    lines = text.splitlines(keepends=True)
    body_lines = lines[start + 1 : end]
    top, _, sub = dotted.partition(".")

    for idx, line in enumerate(body_lines):
        match = re.match(r"^(\s*)([A-Za-z0-9_.-]+)\s*:\s*(.*)$", line.rstrip("\r\n"))
        if not match or match.group(1).strip():
            continue  # nested key or not a key line
        if match.group(2) != top:
            continue
        newline = "\r\n" if line.endswith("\r\n") else "\n"
        if not sub:
            body_lines[idx] = f"{top}: {_scalar_like(match.group(3), value)}{newline}"
            out = lines[: start + 1] + body_lines + lines[end:]
            return "".join(out)
        # nested: replace the child line inside this block
        block_end = idx + 1
        while block_end < len(body_lines) and (
            body_lines[block_end].startswith((" ", "\t")) or not body_lines[block_end].strip()
        ):
            if not body_lines[block_end].strip():
                break
            block_end += 1
        child = sub.split(".", 1)[0]
        indent = None
        for jdx in range(idx + 1, block_end):
            child_match = re.match(r"^(\s+)([A-Za-z0-9_.-]+)\s*:\s*(.*)$", body_lines[jdx].rstrip("\r\n"))
            if child_match and child_match.group(2) == child:
                indent = child_match.group(1)
                body_lines[jdx] = (
                    f"{indent}{child}: {_scalar_like(child_match.group(3), value)}{newline}"
                )
                out = lines[: start + 1] + body_lines + lines[end:]
                return "".join(out)
        # child missing under an existing parent: append it
        indent = "  "
        body_lines.insert(idx + 1, f"{indent}{child}: {_yaml_scalar(value)}{newline}")
        out = lines[: start + 1] + body_lines + lines[end:]
        return "".join(out)

    # key (and its parent) absent entirely
    newline = "\r\n" if text.find("\r\n") != -1 else "\n"
    if sub:
        new_lines = [f"{top}:{newline}", f"  {sub}: {_yaml_scalar(value)}{newline}"]
    else:
        new_lines = [f"{top}: {_yaml_scalar(value)}{newline}"]
    out = lines[:end] + new_lines + lines[end:]
    return "".join(out)


def _insert_frontmatter(text: str, dotted: str, value: Any) -> str:
    top, _, sub = dotted.partition(".")
    block = [FRONTMATTER_OPEN]
    if sub:
        block += [f"{top}:", f"  {sub}: {_yaml_scalar(value)}"]
    else:
        block += [f"{top}: {_yaml_scalar(value)}"]
    block.append(FRONTMATTER_OPEN)
    return "\n".join(block) + "\n\n" + text.lstrip("\n")


def _delete_frontmatter_key(text: str, dotted: str) -> str:
    bounds = _frontmatter_bounds(text)
    if bounds is None:
        return text
    start, end = bounds
    lines = text.splitlines(keepends=True)
    top, _, sub = dotted.partition(".")
    keep = []
    skip_depth = 0
    for line in lines[start + 1 : end]:
        stripped = line.strip()
        if skip_depth:
            if stripped.startswith("-") or re.match(r"^\s+\S", line):
                continue
            skip_depth = 0
        match = re.match(r"^([A-Za-z0-9_.-]+)\s*:", line)
        if match:
            if skip_depth == 0 and match.group(1) == top and not sub:
                skip_depth = 1
                continue
        keep.append(line)
    return "".join(lines[: start + 1] + keep + lines[end:])


def _set_section(text: str, heading: str, value: str | None) -> str:
    """Replace (or remove) the body under one heading, keeping the heading."""
    bounds = _frontmatter_bounds(text)
    prefix_lines = text.splitlines(keepends=True)
    offset = (bounds[1] + 1) if bounds else 0
    head = "".join(prefix_lines[:offset])
    body = "".join(prefix_lines[offset:])

    lines = body.splitlines(keepends=True)
    level = None
    start = None
    end = len(lines)
    for idx, line in enumerate(lines):
        match = re.match(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$", line.rstrip("\r\n"))
        if not match:
            continue
        name = re.sub(r"\s+", " ", match.group(2).strip())
        if start is None:
            if name == heading and not name.endswith(" #2"):
                start = idx
                level = len(match.group(1))
            continue
        if len(match.group(1)) <= level:
            end = idx
            break
    if start is None:
        return text  # heading gone: nothing to restore into

    newline = "\r\n" if body.find("\r\n") != -1 else "\n"
    heading_line = lines[start]
    payload = [newline] if value else []
    if value:
        payload += [line + newline for line in value.splitlines()]
    return head + "".join(lines[: start + 1] + payload + lines[end:])


def apply_field(text: str, field_name: str, value: Any) -> str:
    """Return ``text`` with ``field_name`` set to ``value`` (no disk writes)."""
    if field_name == "body":
        return _replace_body(text, str(value or ""))
    if field_name.startswith("section:"):
        return _set_section(text, field_name[len("section:") :], value)
    if field_name.startswith("frontmatter."):
        dotted = field_name[len("frontmatter.") :]
        if value is None:
            return _delete_frontmatter_key(text, dotted)
        return _set_frontmatter_key(text, dotted, value)
    raise RepoError(f"cannot revert field {field_name!r} (expected frontmatter.*, section:*, body)")


def _replace_body(text: str, value: str) -> str:
    bounds = _frontmatter_bounds(text)
    if bounds is None:
        return value
    lines = text.splitlines(keepends=True)
    return "".join(lines[: bounds[1] + 1]) + str(value)


# ------------------------------------------------------------------ resolution


def _value_at(repo: Repo, commit_id: str, path: str, field_name: str) -> Any:
    """The value of one field as it stood in ``commit_id``'s snapshot."""
    tree = repo.read_tree(repo.read_commit(commit_id)["tree"])
    blob = tree.get(path)
    if blob is None:
        return None
    text = repo.read_blob_text(blob) or ""
    page = parse_page(path, text)
    return page.field_text(field_name)


def _field_at(repo: Repo, commit_id: str, path: str) -> list[str]:
    tree = repo.read_tree(repo.read_commit(commit_id)["tree"])
    blob = tree.get(path)
    if blob is None:
        return []
    page = parse_page(path, repo.read_blob_text(blob) or "")
    names = [f"frontmatter.{k}" for k in page.fields]
    names += [f"section:{s}" for s in page.sections]
    if page.preamble.strip():
        names.append("body")
    return names


def _any_field_named(history: list[dict], path: str, field_name: str) -> bool:
    """Did any commit ever record a change to this exact field?

    Cheap guard against the typo case: a name that appears nowhere in history
    is not a field, no matter what the current page looks like.
    """
    return any(
        ch.get("path") == path and ch.get("field") == field_name
        for c in history
        for ch in c.get("changes") or []
    )


def plan_field(repo: Repo, path: str, field_name: str, commit_ref: str | None) -> Plan:
    """Restore one field to its value in ``commit_ref`` (default: the commit
    *before* the one that last touched this field)."""
    current_file = repo.root / path
    if not current_file.exists():
        return Plan(path, field_name, skipped=True, reason="no such page in the working tree")
    current = parse_page(path, current_file.read_text(encoding="utf-8", newline="")).field_text(field_name)

    history = [c for c in repo.walk_history()]
    # A field that exists nowhere — not in the working tree, not in the newest
    # snapshot — is a typo. Catch it *before* matching, because an `add` commit
    # would otherwise "match" any name you happen to type.
    known = _field_at(repo, repo.head(), path) if repo.head() else []
    if current is None and field_name not in known and not _any_field_named(history, path, field_name):
        near = [k for k in known if field_name.split(".")[-1].lower() in k.lower()]
        hint = (
            " — did you mean " + ", ".join(near[:3])
            if near
            else f" — fields on this page: {', '.join(known[:6])}"
        )
        return Plan(
            path,
            field_name,
            skipped=True,
            reason=f"no commit ever changed this field{hint}",
        )

    touching = [
        c
        for c in history
        if any(
            ch.get("path") == path
            # An `add` carries no field, but it *is* the commit that brought
            # this field into existence, so undoing it lands on "absent".
            and (ch.get("field") == field_name or ch.get("op") == "add")
            for ch in c.get("changes") or []
        )
    ]
    if not touching:
        # Not a typo, yet no commit recorded a change: only possible if the
        # page predates field-level capture. Still worth a clear message.
        hint = f" — fields on this page: {', '.join(known[:6])}" if known else ""
        return Plan(
            path,
            field_name,
            skipped=True,
            reason=f"no commit ever changed this field{hint}",
        )

    if commit_ref:
        target = _resolve_strict(repo, commit_ref)
        origin = touching[-1]
    else:
        # Newest first, so touching[0] is where the value came from and
        # touching[1] is where it was before. With only one entry the field
        # has existed since the first snapshot: restore to "absent".
        if len(touching) < 2:
            origin = touching[0]
            if current in (None, ""):
                return Plan(
                    path,
                    field_name,
                    skipped=True,
                    reason="field is already absent; nothing to undo",
                )
            note = f"remove (introduced by {short(origin['id'])})"
            return Plan(
                path=path, field=field_name, before=current, after=None,
                note=note, origin=origin["id"],
            )
        origin = touching[0]
        target = touching[1]["id"]

    restore = _value_at(repo, target, path, field_name)
    note = f"restore from {short(target)} (undo {short(origin['id'])})"
    return Plan(
        path=path, field=field_name, before=current, after=restore,
        note=note, origin=origin["id"],
    )


def _resolve_strict(repo: Repo, ref: str | None) -> str:
    """Resolve a ref, but never silently treat garbage as "nothing to do".

    ``Repo.resolve`` answers ``None`` for both "no such commit" and "there is
    no HEAD yet", which is fine internally and wrong here: a typo would
    produce an empty plan and read as success.
    """
    if not ref:
        raise RepoError("no commit given")
    target = repo.resolve(ref)
    if not target:
        raise RepoError(f"unknown commit: {ref}")
    return target


def plan_page(repo: Repo, path: str, commit_ref: str) -> list[Plan]:
    """Every field that differs between the working file and ``commit_ref``."""
    target = _resolve_strict(repo, commit_ref)
    old_fields = _field_at(repo, target, path)
    current_file = repo.root / path
    if not current_file.exists():
        raise RepoError(f"{path} does not exist in the working tree")
    page = parse_page(path, current_file.read_text(encoding="utf-8"))
    current_fields = [f"frontmatter.{k}" for k in page.fields]
    current_fields += [f"section:{s}" for s in page.sections]
    if page.preamble.strip():
        current_fields.append("body")

    plans: list[Plan] = []
    for name in sorted(set(old_fields) | set(current_fields)):
        before = page.field_text(name)
        after = _value_at(repo, target, path, name)
        if before != after:
            plans.append(Plan(path, name, before, after, f"restore from {short(target)}"))
    if not plans:
        plans.append(Plan(path, None, skipped=True, reason=f"already identical to {short(target)}"))
    return plans


def plan_commit(repo: Repo, commit_ref: str) -> list[Plan]:
    """Undo every change ``commit_ref`` made, field by field."""
    target = _resolve_strict(repo, commit_ref)
    payload = repo.read_commit(target)
    parent = payload.get("parent")
    if not parent:
        raise RepoError("cannot revert the initial snapshot — there is nothing before it")

    plans: list[Plan] = []
    for raw in payload.get("changes") or []:
        change = Change.from_dict(raw)
        if change.op == "add":
            plans.append(
                Plan(
                    change.path, None, "exists", None,
                    f"delete (was created by {short(target)})", origin=target,
                )
            )
            continue
        if change.op == "rename" and change.rename_from:
            plans.append(
                Plan(
                    change.path, None, change.path, change.rename_from,
                    f"rename back to {change.rename_from}", origin=target,
                )
            )
            continue
        if not change.field:
            plans.append(Plan(change.path, None, skipped=True, reason=f"cannot undo a {change.op} without a field"))
            continue
        restore = _value_at(repo, parent, change.path, change.field)
        plans.append(
            Plan(
                change.path, change.field, change.before, restore,
                f"undo {short(target)}", origin=target,
            )
        )
    return plans


def plan(repo: Repo, *, field_name=None, page=None, commit_ref=None) -> list[Plan]:
    if field_name and page:
        return [plan_field(repo, page, field_name, commit_ref)]
    if page and commit_ref:
        return plan_page(repo, page, commit_ref)
    if commit_ref:
        return plan_commit(repo, commit_ref)
    raise RepoError("say what to revert: --field with --page, --page with --commit, or --commit alone")


# ------------------------------------------------------------------- execution


def execute(
    repo: Repo,
    plans: list[Plan],
    *,
    dry_run: bool = False,
    author_name: str | None = None,
    reverts: str | None = None,
) -> RevertResult:
    """Apply the plans to disk, then record one commit. Nothing on dry_run.

    ``reverts`` names the commit being undone. It goes into the *new* commit as
    a forward pointer (``reverts: sha256:…``) and into its message for humans.
    The old commit keeps its id, its tree and its place in the chain — undoing
    is an append, never an edit, so the record of what actually happened that
    night cannot be erased by undoing it.
    """
    result = RevertResult(plans=plans)
    actionable = [p for p in plans if not p.skipped]
    if dry_run or not actionable:
        return result

    for item in actionable:
        target = repo.root / item.path
        if item.field is None:
            if item.after is None:
                if target.exists():
                    target.unlink()
                    result.written.append(f"deleted {item.path}")
                continue
            # rename back
            dest = repo.root / str(item.after)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                target.replace(dest)
                result.written.append(f"{item.path} -> {item.after}")
            continue

        if not target.exists():
            continue
        text = target.read_text(encoding="utf-8", newline="")
        updated = apply_field(text, item.field, item.after)
        if updated != text:
            # newline="" on the way out too: without it, Windows translates
            # every \n to \r\n and a one-field revert rewrites the whole file.
            target.write_text(updated, encoding="utf-8", newline="")
            result.written.append(f"{item.path} · {item.field}")

    if not result.written:
        return result

    # When every actionable plan points at the same commit, that commit is
    # what this revert undoes and the new commit should say so. When the plans
    # came from different commits (a whole-page restore), there is no single
    # answer, and inventing one would make the pointer lie.
    if reverts is None:
        origins = {p.origin for p in actionable if p.origin}
        reverts = next(iter(origins)) if len(origins) == 1 else None

    message = _message(plans, reverts)
    result.commit = commit_op(
        repo,
        message,
        evidence=["manual:memdrift revert"],
        author_type="human",
        author_name=author_name or "you",
        reverts=reverts,
    )
    return result


def _message(plans: list[Plan], reverts: str | None = None) -> str:
    actionable = [p for p in plans if not p.skipped]
    if len(actionable) == 1:
        item = actionable[0]
        where = f"{item.path} · {item.field}" if item.field else item.path
        base = f"revert: {where}"
    else:
        base = f"revert: {len(actionable)} change(s)"
    # Both halves of the pointer: the message is what a human reads in `log`,
    # the `reverts` field is what a machine follows.
    return f"{base} (undoes {short(reverts)})" if reverts else base


def describe(item: Plan) -> str:
    """One line for the CLI / MCP output."""
    if item.skipped:
        return f"skip  {item.path}{' · ' + item.field if item.field else ''}  ({item.reason})"
    if item.field is None and item.after is None:
        return f"delete {item.path}  {item.note}"
    where = f"{item.path} · {item.field}" if item.field else item.path
    old = "∅" if item.before in (None, "") else str(item.before)
    new = "∅" if item.after in (None, "") else str(item.after)
    if len(old) > 60:
        old = old[:59] + "…"
    if len(new) > 60:
        new = new[:59] + "…"
    return f"{where}: {old} → {new}  {item.note}"
