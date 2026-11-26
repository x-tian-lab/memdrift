"""The MCP surface: what an agent may ask about your memory.

Design decisions, and the failure each one prevents:

**Dotted names — ``memory.history``, not ``memory_history``.** A model reading
a tool list sees ``memory.*`` and knows these belong together; a flat prefix
still reads as five unrelated tools. Some providers reject dots in name fields,
so :func:`provider_name` maps them to the safe ``memory_history`` form *only*
where a provider needs it — the wire keeps the readable name.

**Read tools are annotated ``readOnlyHint: true``.** This is the whole
approval story. A client that trusts hints will auto-run history/blame/diff and
stop to ask before ``memory.revert``, without any custom client code. A client
that ignores hints falls back to its own prompt-every-time default, which is
also safe. We never *rely* on the hint for safety — ``memory.revert`` is
honest about being a writer either way.

**Read-only means read-only.** No tool writes to disk unless it is
``memory.commit`` or ``memory.revert``. ``memory.diff`` compares against HEAD
using the stored snapshots rather than re-snapshotting the working tree, so an
agent asking "what changed?" cannot accidentally stage something.

**Every string that leaves this module is redacted.** The same
:class:`~memdrift.security.Redactor` the digest and the web UI use, so an agent
in one editor tab cannot see a path the human running the CLI cannot.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import operations as ops
from .differ import Change
from .events import is_private_page, mask_private, private_paths
from .parse import parse_page
from .repo import Repo, RepoError, short
from .rpc import FORBIDDEN, INVALID_PARAMS, RpcError
from .rpc import Server as RpcServer
from .security import Redactor, is_private

#: Provider name fields often require ``^[a-zA-Z0-9_-]{1,64}$``.
SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")


def provider_name(name: str) -> str:
    """``memory.history`` → ``memory_history``. Only for provider name fields."""
    return SAFE_NAME_RE.sub("_", name)[:64]


def _as_dicts(items: Any) -> list[dict]:
    """Evidence arrives as dicts from a fresh read and as JSON strings from the
    sqlite index. Normalise, because the redactor only understands dicts."""
    if not items:
        return []
    if isinstance(items, str):
        import json

        try:
            items = json.loads(items)
        except json.JSONDecodeError:
            return []
    return [i for i in items if isinstance(i, dict)]


def _change_json(change: Change, redactor: Redactor) -> dict[str, Any]:
    payload = {
        "path": change.path,
        "op": change.op,
        "field": change.field,
        "before": change.before,
        "after": change.after,
        "change_ratio": round(change.change_ratio, 3),
        "low_precision": change.low_precision,
    }
    if change.rename_from:
        payload["rename_from"] = change.rename_from
    clean = redactor.change(payload)
    # `change()` only returns None when dropping a private page; we never
    # flag private here (the caller already refused such pages), but never
    # fall back to the *unredacted* payload — that would be a real leak.
    return clean if clean is not None else {
        "path": None, "op": change.op, "field": change.field, "hidden": True
    }


class Toolbox:
    """Binds a repo to the MCP tool table."""

    def __init__(self, repo: Repo, *, keep_quotes: bool = True) -> None:
        self.repo = repo
        self.redactor = Redactor(root=str(repo.root), keep_quotes=keep_quotes)

    # ------------------------------------------------------------- guardrails

    def _page(self, raw: str | None) -> str | None:
        """Reject paths that escape the memory root, before touching the disk."""
        if raw is None:
            return None
        candidate = (self.repo.root / raw).resolve()
        if self.repo.root not in candidate.parents and candidate != self.repo.root:
            raise RpcError(INVALID_PARAMS, f"path escapes the memory root: {raw}")
        return candidate.relative_to(self.repo.root).as_posix()

    def _require_write(self) -> None:
        """The write tools ask the scope, not the caller, for permission.

        An agent that was handed a shell is still not the operator: the space
        list is what decides, and it is re-read on every write so revoking a
        space takes effect without restarting the server.
        """
        from . import scope

        try:
            scope.check(self.repo.root, write=True)
        except scope.OutOfScope as exc:
            raise RpcError(FORBIDDEN, str(exc)) from exc

    def _commit(self, ref: str | None) -> str | None:
        if ref is None:
            return None
        resolved = self.repo.resolve(ref)
        if not resolved:
            raise RpcError(INVALID_PARAMS, f"unknown commit: {ref}")
        return resolved

    def _blob(self, commit_id: str, path: str) -> str | None:
        tree = self.repo.read_tree(self.repo.read_commit(commit_id)["tree"])
        blob = tree.get(path)
        return self.repo.read_blob_text(blob) if blob else None

    def _refuse_private(self, page: str | None) -> None:
        """A private page is not served, and "served" includes blame/diff/revert.

        ``events.build`` drops private pages, which protects every *list*
        surface. A tool that takes a path is different: it can be pointed at a
        page that no list mentions, so the rule has to be asked again here.
        Without this, ``memory.blame`` on a page marked private returns its
        field values — the exact leak the marker exists to prevent.
        """
        if page and is_private_page(self.repo, page):
            raise RpcError(INVALID_PARAMS, f"{page} is marked private")

    # ----------------------------------------------------------------- tools

    def history(self, params: dict) -> dict:
        page = self._page(params.get("page"))
        self._refuse_private(page)
        limit = int(params.get("limit") or 20)
        rows = ops.log(self.repo, page=page, since=params.get("since"), limit=limit)
        hidden = private_paths(self.repo)
        kept: list[dict] = []
        hidden_commits = 0
        for row in rows:
            touched = {c.get("path") for c in row.get("changes") or []} - {None}
            if touched and touched <= hidden:
                # A commit that only touched private pages is not history a
                # reader of this vault is allowed to see. Counted, not hidden
                # silently — "no commits" and "all private" differ.
                hidden_commits += 1
                continue
            kept.append(row)
        return {
            "commits": [
                {
                    "id": short(c["id"]),
                    "commit_id": c["id"],
                    "ts": c.get("ts"),
                    # The log index returns flat columns; do not invent a
                    # nested `author` key that only some callers have.
                    "author": f"{c.get('author_type')}:{mask_private(c.get('author_name'), hidden)}",
                    "author_type": c.get("author_type"),
                    "author_name": mask_private(self.redactor.text(c.get("author_name")), hidden),
                    "message": mask_private(self.redactor.text(c.get("message")), hidden),
                    "confidence": c.get("confidence"),
                    "evidence": self.redactor.evidence(_as_dicts(c.get("evidence"))),
                    "changed": len(c.get("changes") or []),
                    # Present only on an undo commit; the pair (this id, that
                    # id) is what makes "who erased it" answerable without
                    # either commit being rewritten.
                    "reverts": short(c.get("reverts")) if c.get("reverts") else None,
                }
                for c in kept
            ],
            "page": page,
            "hidden_commits": hidden_commits,
        }

    def blame(self, params: dict) -> dict:
        page = self._page(params.get("page"))
        if not page:
            raise RpcError(INVALID_PARAMS, "page is required")
        self._refuse_private(page)
        entries = ops.blame(self.repo, page)
        if not entries:
            raise RpcError(INVALID_PARAMS, f"no history for {page}")
        hidden = private_paths(self.repo)
        for entry in entries:
            entry["before"] = self.redactor.text(entry.get("before"))
            entry["after"] = self.redactor.text(entry.get("after"))
            entry["message"] = mask_private(self.redactor.text(entry.get("message")), hidden)
            entry["evidence"] = self.redactor.evidence(_as_dicts(entry.get("evidence")))
        return {"page": page, "fields": entries}

    def diff(self, params: dict) -> dict:
        """What commit ``to`` changed relative to ``from`` (default: HEAD~1→HEAD)."""
        to = self._commit(params.get("to"))
        from_ = self._commit(params.get("from"))
        head = self.repo.head()
        if to is None and from_ is None:
            if not head:
                return {"from": None, "to": None, "changes": [], "hidden": 0}
            payload = self.repo.read_commit(head)
            from_ = self.repo.resolve(payload.get("parent"))
            to = head
            if not from_:
                changes = [Change.from_dict(c) for c in payload.get("changes") or []]
                public, hidden = self._public(changes)
                return {
                    "from": None,
                    "to": short(to),
                    "changes": [_change_json(c, self.redactor) for c in public],
                    "hidden": hidden,
                }
        if to is None or from_ is None:
            raise RpcError(INVALID_PARAMS, "give both `from` and `to`, or neither")
        changes, hidden = self._public(ops.diff_commits(self.repo, from_, to))
        return {
            "from": short(from_),
            "to": short(to),
            "changes": [_change_json(c, self.redactor) for c in changes],
            # Reported, not silently dropped: a diff that hides rows and says
            # nothing would read as "this commit only touched what you see".
            "hidden": hidden,
        }

    def _public(self, changes: list[Change]) -> tuple[list[Change], int]:
        """Split out changes belonging to private pages.

        Unlike a list view, a diff is *about* specific paths, so filtering has
        to happen before rendering and the count has to be reported: the
        caller must be able to tell "nothing happened" from "what happened is
        private".
        """
        hidden_paths = private_paths(self.repo)
        if not hidden_paths:
            return changes, 0
        public: list[Change] = []
        dropped = 0
        for change in changes:
            touched = {change.path, change.rename_from} - {None}
            if touched & hidden_paths:
                dropped += 1
            else:
                public.append(change)
        return public, dropped

    def page_at(self, params: dict) -> dict:
        """Read a page as it was at a commit. The retrieval primitive."""
        page = self._page(params.get("page"))
        if not page:
            raise RpcError(INVALID_PARAMS, "page is required")
        self._refuse_private(page)
        commit = params.get("commit")
        commit_id = self._commit(commit) if commit else self.repo.head()
        if not commit_id:
            raise RpcError(INVALID_PARAMS, "no commits yet")
        text = self._blob(commit_id, page)
        if text is None:
            raise RpcError(INVALID_PARAMS, f"{page} does not exist at {short(commit_id)}")
        parsed = parse_page(page, text)
        if is_private(parsed.fields):
            # A private page is not "hidden from the renderer", it is not served.
            raise RpcError(INVALID_PARAMS, f"{page} is marked private")
        return {
            "page": page,
            "commit": short(commit_id),
            # Field values are memory content, not machine metadata, but they
            # can still contain a pasted absolute path. Same redactor, same
            # guarantee as every other tool in this module.
            "fields": {k: self.redactor.text(v) for k, v in parsed.fields.items()},
            "sections": list(parsed.section_names),
            "text": self.redactor.text(text),
        }

    def digest(self, params: dict) -> dict:
        """Everything that happened in a window, in one structured answer."""
        from . import events as events_mod

        page = self._page(params.get("page"))
        stream = events_mod.build(
            self.repo, since=params.get("since"), until=params.get("until"),
            page=page, redactor=self.redactor,
        )
        return {
            "since": params.get("since"),
            "commits": stream.commits,
            "changes": len(stream.events),
            "risky": len(stream.risky),
            "unsure_commits": [short(c) for c in stream.unsure],
            "hidden_pages": len(stream.hidden),
            "events": stream.events,
        }

    def commit(self, params: dict) -> dict:
        """Write tool: snapshot the working tree into a commit."""
        self._require_write()
        message = (params.get("message") or "").strip()
        if not message:
            raise RpcError(INVALID_PARAMS, "message is required — an unexplained commit is useless")
        try:
            payload = ops.commit(
                self.repo,
                message,
                evidence=list(params.get("evidence") or []),
                author_type="agent",
                author_name=params.get("author") or "mcp",
                confidence=params.get("confidence"),
            )
        except RepoError as exc:
            raise RpcError(INVALID_PARAMS, str(exc)) from exc
        if payload is None:
            return {"committed": False, "reason": "nothing changed since the last commit"}
        return {
            "committed": True,
            "id": short(payload["id"]),
            "commit_id": payload["id"],
            "changes": len(payload.get("changes") or []),
        }

    def revert(self, params: dict) -> dict:
        """Write tool: undo by committing the inverse. Never rewrites history."""
        from . import revert as revert_mod

        page = self._page(params.get("page"))
        field_name = params.get("field")
        commit_ref = params.get("commit")
        if not (field_name and page) and not (page and commit_ref) and not commit_ref:
            raise RpcError(
                INVALID_PARAMS,
                "say what to revert: field+page, page+commit, or commit",
            )
        # A revert answers with before/after values, so it is a read as well
        # as a write: the private-page rule applies to both halves.
        self._refuse_private(page)
        if commit_ref:
            commit_ref = self._commit(commit_ref)
        try:
            plans = revert_mod.plan(
                self.repo, field_name=field_name, page=page, commit_ref=commit_ref
            )
        except RepoError as exc:
            raise RpcError(INVALID_PARAMS, str(exc)) from exc
        if any(is_private_page(self.repo, p.path) for p in plans if p.path):
            # `--commit` can name a commit that touched a private page even
            # when no page was given.
            raise RpcError(INVALID_PARAMS, "this change touches a page marked private")

        dry_run = bool(params.get("dry_run", True))  # safe default: look first
        if not dry_run:
            # A dry run is a read and stays available on a read-only space; the
            # apply is what needs the operator's permission.
            self._require_write()
        result = revert_mod.execute(self.repo, plans, dry_run=dry_run)
        return {
            "dry_run": dry_run,
            "plans": [
                {
                    "path": p.path,
                    "field": p.field,
                    "before": self.redactor.text(p.before),
                    "after": self.redactor.text(p.after),
                    "skipped": p.skipped,
                    "reason": p.reason,
                    "note": p.note,
                }
                for p in plans
            ],
            "commit": short(result.commit["id"]) if result.commit else None,
            "reverts": short(result.commit.get("reverts")) if result.commit else None,
            "written": result.written,
        }


#: name → (handler attr, title, description, read_only, schema properties, required)
TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "memory.history",
        "attr": "history",
        "title": "Memory history",
        "description": (
            "List commits that touched the memory directory, newest first. "
            "Use this to answer 'what did the agent change lately?'."
        ),
        "readOnly": True,
        "props": {
            "page": {"type": "string", "description": "Only commits touching this page, e.g. clients/acme.md"},
            "since": {"type": "string", "description": "Window like 7d, 24h, 2w, or 2026-11-01"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20},
        },
        "required": [],
    },
    {
        "name": "memory.blame",
        "attr": "blame",
        "title": "Who last changed each field",
        "description": (
            "For every field of one page: the commit that last touched it, the value, "
            "and the evidence that justified it. Use this before trusting a value."
        ),
        "readOnly": True,
        "props": {"page": {"type": "string", "description": "Page path, e.g. clients/acme.md"}},
        "required": ["page"],
    },
    {
        "name": "memory.diff",
        "attr": "diff",
        "title": "Diff two commits",
        "description": (
            "Field-level diff of one commit against another. With no arguments, "
            "diffs HEAD against its parent."
        ),
        "readOnly": True,
        "props": {
            "from": {"type": "string", "description": "Baseline commit (id or prefix)"},
            "to": {"type": "string", "description": "Compare this commit (id or prefix)"},
        },
        "required": [],
    },
    {
        "name": "memory.page",
        "attr": "page_at",
        "title": "Read a page at a commit",
        "description": (
            "The page exactly as it stood at a commit, with parsed fields and sections. "
            "Default commit is HEAD. Pages marked private are refused."
        ),
        "readOnly": True,
        "props": {
            "page": {"type": "string", "description": "Page path"},
            "commit": {"type": "string", "description": "Commit id or prefix; default HEAD"},
        },
        "required": ["page"],
    },
    {
        "name": "memory.digest",
        "attr": "digest",
        "title": "Digest a window",
        "description": (
            "Every field change in a time window as structured events, with risk flags. "
            "The same data the HTML digest renders."
        ),
        "readOnly": True,
        "props": {
            "since": {"type": "string", "description": "Window like 7d, 24h, or 2026-11-01"},
            "until": {"type": "string", "description": "Upper bound, same formats as since"},
            "page": {"type": "string", "description": "Only this page"},
        },
        "required": [],
    },
    {
        "name": "memory.commit",
        "attr": "commit",
        "title": "Commit memory changes",
        "description": (
            "WRITES: snapshot the memory directory into a new commit. Requires a message "
            "explaining why. Returns committed=false when nothing changed."
        ),
        "readOnly": False,
        "props": {
            "message": {"type": "string", "description": "Why this change happened"},
            "evidence": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Refs like email:msg-1882 or file:notes.md",
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "author": {"type": "string", "description": "Name to record; default 'mcp'"},
        },
        "required": ["message"],
    },
    {
        "name": "memory.revert",
        "attr": "revert",
        "title": "Undo a change",
        "description": (
            "WRITES: undo by committing the inverse; history is never rewritten. "
            "Defaults to dry_run=true — call again with dry_run=false to apply. "
            "Targets: field+page, page+commit, or commit alone."
        ),
        "readOnly": False,
        "props": {
            "field": {"type": "string", "description": "e.g. frontmatter.pricing"},
            "page": {"type": "string", "description": "Page path"},
            "commit": {"type": "string", "description": "Restore to / undo this commit"},
            "dry_run": {"type": "boolean", "default": True, "description": "Look, do not write"},
        },
        "required": [],
    },
]


def tool_list() -> list[dict]:
    """The ``tools/list`` payload."""
    out = []
    for spec in TOOL_SPECS:
        schema: dict[str, Any] = {"type": "object", "properties": spec["props"], "required": spec["required"]}
        out.append(
            {
                "name": spec["name"],
                "title": spec["title"],
                "description": spec["description"],
                "inputSchema": schema,
                "annotations": {
                    "readOnlyHint": spec["readOnly"],
                    "destructiveHint": spec["name"] == "memory.revert",
                    "idempotentHint": spec["readOnly"],
                    "openWorldHint": False,
                },
            }
        )
    return out


def call_tool(toolbox: Toolbox, name: str, arguments: dict | None) -> dict:
    """Run one tool and wrap the result in MCP's content envelope."""
    import json

    spec = next((s for s in TOOL_SPECS if s["name"] == name), None)
    if spec is None:
        raise RpcError(INVALID_PARAMS, f"unknown tool: {name}")

    args = arguments or {}
    missing = [k for k in spec["required"] if args.get(k) in (None, "")]
    if missing:
        raise RpcError(INVALID_PARAMS, f"{name} needs {', '.join(missing)}")

    result = getattr(toolbox, spec["attr"])(args)
    return {
        "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}],
        "structuredContent": result,
        "isError": False,
    }


def build_server(repo: Repo, *, keep_quotes: bool = True) -> "RpcServer":
    """Wire the tool table onto a JSON-RPC server."""
    from .repo import VERSION

    server = RpcServer("memdrift", VERSION)
    box = Toolbox(repo, keep_quotes=keep_quotes)

    @server.method("tools/list")
    def _list(_params: dict) -> dict:
        return {"tools": tool_list()}

    @server.method("tools/call")
    def _call(params: dict) -> dict:
        return call_tool(box, params.get("name"), params.get("arguments") or {})

    @server.method("resources/list")
    def _no_resources(_params: dict) -> dict:
        # Declared as an empty list rather than omitted: some clients treat a
        # missing method as a broken server and offer to restart it.
        return {"resources": []}

    @server.method("prompts/list")
    def _no_prompts(_params: dict) -> dict:
        return {"prompts": []}

    return server
