"""Operations the CLI and the (future) MCP server both call.

Keeping them here means the digest/MCP layer never has to shell out to the CLI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .differ import Change, diff_trees
from .index import Index
from .repo import Repo, short

EVIDENCE_KINDS = {"email", "message", "file", "url", "meeting", "manual"}


@dataclass
class StatusReport:
    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    renamed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.added or self.modified or self.deleted or self.renamed)

    @property
    def total(self) -> int:
        return len(self.added) + len(self.modified) + len(self.deleted) + len(self.renamed)


def open_index(repo: Repo) -> Index:
    return Index(repo.meta / "index.sqlite")


def parse_evidence(items: Iterable[str] | None) -> list[dict]:
    """``email:msg-1882`` → {'kind': 'email', 'ref': 'msg-1882'}."""
    out: list[dict] = []
    for raw in items or []:
        raw = raw.strip()
        if not raw:
            continue
        kind, sep, ref = raw.partition(":")
        if sep and kind in EVIDENCE_KINDS:
            out.append({"kind": kind, "ref": ref})
        elif "://" in raw:
            out.append({"kind": "url", "ref": raw})
        else:
            out.append({"kind": "manual", "ref": raw})
    return out


def status(repo: Repo) -> StatusReport:
    old_tree = repo.head_tree()
    new_tree = repo.snapshot(store=False)

    report = StatusReport()
    added = [p for p in new_tree if p not in old_tree]
    removed = [p for p in old_tree if p not in new_tree]
    new_by_blob = {new_tree[p]: p for p in added}
    claimed: set[str] = set()
    for path in sorted(removed):
        twin = new_by_blob.get(old_tree[path])
        if twin and twin not in claimed:
            claimed.add(twin)
            report.renamed.append((path, twin))
        else:
            report.deleted.append(path)
    report.added = sorted(p for p in added if p not in claimed)
    report.modified = sorted(
        p for p in set(old_tree) & set(new_tree) if old_tree[p] != new_tree[p]
    )
    return report


def commit(
    repo: Repo,
    message: str,
    *,
    evidence: list[str] | None = None,
    author_type: str | None = None,
    author_name: str | None = None,
    confidence: float | None = None,
    update_index: bool = True,
) -> dict | None:
    """Snapshot, diff against HEAD, write a commit. Returns None if nothing changed."""
    new_tree = repo.snapshot()
    old_tree = repo.head_tree()
    changes = diff_trees(old_tree, new_tree, repo.read_blob_text)
    if not changes:
        return None

    parent = repo.head()
    is_first = parent is None
    author = {
        "type": author_type or ("human" if is_first else repo.config.author_type),
        "name": author_name or ("you" if is_first else repo.config.author_name),
    }
    payload = repo.write_commit(
        tree=repo.write_tree(new_tree),
        parent=parent,
        changes=[c.to_dict() for c in changes],
        message=message or ("initial snapshot" if is_first else "update"),
        author=author,
        evidence=parse_evidence(evidence),
        confidence=confidence,
    )
    if update_index:
        index = open_index(repo)
        try:
            index.add_commit(payload)
        finally:
            index.close()
    return payload


def log(
    repo: Repo,
    *,
    page: str | None = None,
    since: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Commits, newest first.

    The index is a cache: if it is missing, deleted, or behind HEAD (a crash
    mid-commit, a copied .memgit), rebuild it from the commit files rather than
    serving stale history.
    """
    from .index import parse_since

    cutoff = parse_since(since)
    head = repo.head()
    if not head:
        return []

    index = open_index(repo)
    try:
        if not index.has_commit(head):
            index.rebuild(repo.walk_history())
        return index.log(page=page, since=cutoff, limit=limit)
    finally:
        index.close()


def diff_commits(repo: Repo, a: str | None, b: str | None) -> list[Change]:
    """git-compatible semantics.

    \b
    (None, None)  working tree vs empty
    (a, None)     working tree vs snapshot a
    (a, b)        b vs a, i.e. "what b changed relative to a"
    """
    left_ref = a or repo.head()
    left = repo.resolve(left_ref) if left_ref else None
    old_tree = repo.read_tree(repo.read_commit(left)["tree"]) if left else {}

    if b:
        right = repo.resolve(b)
        new_tree = repo.read_tree(repo.read_commit(right)["tree"])
    else:
        new_tree = repo.snapshot()  # working tree; stores blobs for new files

    return diff_trees(old_tree, new_tree, repo.read_blob_text)


def diff_commit(repo: Repo, cid: str | None) -> list[Change]:
    """Changes introduced by a single commit (its changes list, no re-diff)."""
    target = repo.resolve(cid)
    if not target:
        return []
    return [Change.from_dict(c) for c in repo.read_commit(target).get("changes", [])]


OP_LABELS = {"add": "(page created)", "delete": "(page deleted)", "rename": "(renamed)"}


def blame(repo: Repo, path: str) -> list[dict]:
    """For each field of ``path``, the most recent commit that touched it."""
    entries: dict[str, dict] = {}
    for item in repo.walk_history():
        for raw in item.get("changes", []):
            if raw.get("path") != path:
                continue
            change = Change.from_dict(raw)
            key = change.field or OP_LABELS.get(change.op, f"({change.op})")
            if key in entries:
                continue
            entries[key] = {
                "field": key,
                "op": change.op,
                "before": change.before,
                "after": change.after,
                "change_ratio": change.change_ratio,
                "low_precision": change.low_precision,
                "commit": short(item["id"]),
                "commit_id": item["id"],
                "ts": item.get("ts"),
                "author": (item.get("author") or {}).get("name"),
                "author_type": (item.get("author") or {}).get("type"),
                "message": item.get("message"),
                "confidence": item.get("confidence"),
                "evidence": item.get("evidence") or [],
            }

    def order(key: str) -> tuple[int, str]:
        if key.startswith("frontmatter."):
            return (0, key)
        if key.startswith("section:"):
            return (1, key)
        if key == "body":
            return (3, key)
        return (2, key)

    return [entries[k] for k in sorted(entries, key=order)]


def rebuild_index(repo: Repo) -> int:
    index = open_index(repo)
    try:
        return index.rebuild(repo.walk_history())
    finally:
        index.close()
