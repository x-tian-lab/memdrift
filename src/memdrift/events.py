"""Commits + working tree → one flat, redacted event stream.

Both outputs (the static digest and the local server) read *this* and nothing
else. That is the only reason the two views can never disagree about what
happened, and the only reason redaction cannot be forgotten in one of them.

An event is one field-level change with its provenance already joined on:

    {
      "id": "sha256:…", "short": "9f2c1ab", "ts": "2026-11-22T00:00:12+08:00",
      "author": {"type": "agent", "name": "linger"},
      "message": "nightly consolidation",
      "path": "clients/acme.md", "op": "modify",
      "field": "frontmatter.pricing", "before": "$45k", "after": "$48k",
      "evidence": [{"kind": "email", "ref": "msg-1882"}],
      "confidence": 0.82, "low_precision": false, "risk": false
    }
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .differ import Change
from .index import _epoch
from .parse import parse_page
from .repo import Repo, short
from .security import Redactor, is_private

#: A change is worth a human's attention if it is a delete, a big rewrite, or
#: the agent itself was unsure. Matches the PRD's confidence/change_ratio pair.
DELETE_OPS = {"delete"}
FIELD_ORDER = {"frontmatter": 0, "section": 1, "": 2, "body": 3}


def _field_rank(field_name: str | None) -> tuple[int, str]:
    if not field_name:
        return (2, "")
    if field_name.startswith("frontmatter."):
        return (0, field_name)
    if field_name.startswith("section:"):
        return (1, field_name)
    if field_name == "body":
        return (3, field_name)
    return (2, field_name)


def _flag_risk(op: str, change_ratio: float, thresholds: tuple[float, float]) -> bool:
    """Is *this* change worth a human's eyes?

    Deliberately narrow: a delete, or a rewrite big enough that attribution
    starts lying. An earlier version also flagged every change from a
    low-confidence commit, which turned one unsure nightly run into ten red
    rows — the fastest way to teach someone to ignore the red. Low confidence
    is reported once per commit instead (``EventStream.unsure``).
    """
    _risk_confidence, risk_ratio = thresholds
    if op in DELETE_OPS:
        return True
    return change_ratio >= risk_ratio


@dataclass
class PageInfo:
    path: str
    title: str
    private: bool = False


@dataclass
class EventStream:
    events: list[dict] = field(default_factory=list)
    pages: list[PageInfo] = field(default_factory=list)
    hidden: list[str] = field(default_factory=list)
    commits: int = 0
    since: str | None = None
    until: str | None = None
    #: Commit ids whose own confidence came back below the risk threshold.
    #: Reported once per commit rather than smeared over every change.
    unsure: list[str] = field(default_factory=list)

    @property
    def risky(self) -> list[dict]:
        return [e for e in self.events if e.get("risk")]


def _page_title(repo: Repo, path: str) -> str:
    """First ``# heading`` if there is one, else the filename stem."""
    try:
        text = repo.read_blob_text(repo.head_tree().get(path, "")) or ""
    except Exception:
        text = ""
    if not text:
        try:
            text = (repo.root / path).read_text(encoding="utf-8")
        except OSError:
            text = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip() or path
    return path.rsplit("/", 1)[-1].removesuffix(".md")


def collect_pages(repo: Repo) -> dict[str, PageInfo]:
    """Head snapshot → {path: PageInfo}, with the private flag read from frontmatter."""
    pages: dict[str, PageInfo] = {}
    for path, blob in repo.head_tree().items():
        private = False
        try:
            text = repo.read_blob_text(blob) or ""
            private = is_private(parse_page(path, text).fields)
        except Exception:
            private = False
        pages[path] = PageInfo(path=path, title=_page_title(repo, path), private=private)
    return pages


def build(
    repo: Repo,
    *,
    since: str | None = None,
    until: str | None = None,
    page: str | None = None,
    redactor: Redactor | None = None,
) -> EventStream:
    """Walk history, join provenance, redact, drop private pages."""
    from .index import parse_since

    redactor = redactor or Redactor(root=str(repo.root))
    cutoff = parse_since(since) if since else None
    ceiling = parse_since(until) if until else None

    pages = collect_pages(repo)
    private_paths = {p for p, info in pages.items() if info.private}

    out: list[dict] = []
    commit_count = 0
    unsure: list[str] = []
    for item in repo.walk_history():  # newest first
        ts = item.get("ts")
        if cutoff or ceiling:
            epoch = _epoch(ts)
            if cutoff is not None and epoch < cutoff:
                continue
            if ceiling is not None and epoch >= ceiling:
                continue

        commit_events: list[dict] = []
        author = item.get("author") or {}
        thresholds = (repo.config.risk_confidence, repo.config.risk_change_ratio)
        for raw in item.get("changes") or []:
            change = Change.from_dict(raw)
            if page and change.path != page and change.rename_from != page:
                continue
            touched = [change.path] + ([change.rename_from] if change.rename_from else [])
            if any(p in private_paths for p in touched if p):
                redactor.hidden_paths.update(p for p in touched if p)
                continue
            clean = redactor.change(change.to_dict())
            if clean is None:
                continue
            # add / delete / rename carry no field by construction; give every
            # event a comparable label so the renderer never has to branch.
            clean.setdefault("field", None)
            clean.setdefault("before", None)
            clean.setdefault("after", None)
            clean.update(
                {
                    "id": item["id"],
                    "short": short(item["id"]),
                    "ts": ts,
                    "author": {
                        "type": author.get("type") or "unknown",
                        "name": author.get("name") or "unknown",
                    },
                    "message": item.get("message") or "",
                    "evidence": redactor.evidence(item.get("evidence")),
                    "confidence": item.get("confidence"),
                }
            )
            clean["risk"] = _flag_risk(change.op, change.change_ratio, thresholds)
            commit_events.append(clean)

        if commit_events:
            # Newest commit first, but inside a commit the most meaningful
            # field order reads better than the raw diff order.
            commit_events.sort(key=lambda e: _field_rank(e.get("field")))
            out.extend(commit_events)
            commit_count += 1
            conf = item.get("confidence")
            if conf is not None and float(conf) < repo.config.risk_confidence:
                unsure.append(item["id"])

    visible_pages = [info for path, info in pages.items() if path not in private_paths]
    visible_pages.sort(key=lambda p: p.path)
    return EventStream(
        events=out,
        pages=[PageInfo(p.path, p.title) for p in visible_pages],
        hidden=sorted(private_paths),
        commits=commit_count,
        since=since,
        until=until,
        unsure=unsure,
    )


def working_tree(repo: Repo, redactor: Redactor | None = None) -> list[dict]:
    """Uncommitted changes, same shape as committed events (id/ts are null)."""
    from .operations import status as status_report

    redactor = redactor or Redactor(root=str(repo.root))
    report = status_report(repo)
    rows: list[dict] = []
    for path in report.added:
        rows.append(_uncommitted(path, "add", redactor))
    for path in report.modified:
        rows.append(_uncommitted(path, "modify", redactor))
    for path in report.deleted:
        rows.append(_uncommitted(path, "delete", redactor))
    for old, new in report.renamed:
        rows.append(_uncommitted(new, "rename", redactor, rename_from=old))
    return rows


def _uncommitted(path: str, op: str, redactor: Redactor, rename_from: str | None = None) -> dict:
    clean = {
        "path": redactor.path(path),
        "op": op,
        "field": None,
        "id": None,
        "short": "(working)",
        "ts": None,
        "author": {"type": "human", "name": "uncommitted"},
        "message": "not committed yet",
        "evidence": [],
        "confidence": None,
        "low_precision": False,
        "risk": False,
    }
    if rename_from:
        clean["from"] = redactor.path(rename_from)
    return clean


def group_by_commit(events: Iterable[dict]) -> list[dict]:
    """Events → ``[{commit meta, events: [...]}]`` preserving newest-first order."""
    groups: list[dict] = []
    index: dict[str, int] = {}
    for event in events:
        key = event.get("id") or "(working)"
        if key not in index:
            index[key] = len(groups)
            groups.append(
                {
                    "id": event.get("id"),
                    "short": event.get("short"),
                    "ts": event.get("ts"),
                    "author": event.get("author"),
                    "message": event.get("message"),
                    "events": [],
                }
            )
        groups[index[key]]["events"].append(event)
    return groups


def human_time(ts: str | None, now: datetime | None = None) -> str:
    """"12 分钟前" style labels; falls back to the raw date for old commits."""
    if not ts:
        return "uncommitted"
    now = now or datetime.now(timezone.utc).astimezone()
    try:
        when = datetime.fromisoformat(ts)
    except ValueError:
        return ts
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    delta = now - when
    if delta < timedelta(0):
        delta = timedelta(0)
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    if seconds < 86400 * 7:
        return f"{seconds // 86400} d ago"
    return when.strftime("%Y-%m-%d")
