"""JSON API behind ``memdrift ui`` — the one place a browser can write.

``serve`` is read-only and stays that way. This module is its own command
(``memdrift ui``) rather than a mode of ``serve``, for one reason: it contains
the only HTTP endpoint in the project that changes files on disk. Keeping the
two apart means a read-only view can never accidentally grow a write path —
the writable surface has its own server, its own CSP, and its own locks.

A wheel-level split (``memdrift-gui`` as a second distribution) is the next
step; today the module ships with the package and is inert until the command
is run.

Nothing here invents a rule. Reads go through ``events.build`` and the same
``Redactor`` as the digest; writes go through ``revert.plan`` / ``execute`` and
``operations.commit``, exactly as the CLI does them.

What a browser adds is a hostile neighbour, so every request passes the three
locks ``serve`` uses (Host, Origin, token), and writes add two more:

* a required custom header, which forces a CORS preflight from any other
  origin — and we answer no CORS headers, so the browser stops it there;
* ``expect``: the page sends back what it showed the human — a fingerprint of
  the plan, or the list of pending files. The server recomputes and refuses
  with 409 if anything moved in the meantime. You confirm what you looked at,
  never a stale plan. An empty ``expect`` is not "skip the check"; it is a 400.

And the surface is deliberately narrower than the CLI's. The page can put one
field on one page back to its previous value, and it can commit the working
tree. It cannot delete a page, rename one, or revert a whole commit — those
stay on the command line where the plan is printed in full.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import parse_qs, urlparse

from .. import operations as ops
from .. import revert as revert_mod
from ..digest import default_filename, render_digest
from ..events import build as build_events
from ..events import is_private_page, mask_private, private_paths
from ..parse import parse_page
from ..repo import Repo, RepoError, short
from ..security import Redactor, is_private
from ..server import CSP, Server, check_host, host_header_allowed, origin_matches_host
from .app import PAGE

#: Sent by our own page on every write. A cross-origin page cannot attach a
#: custom header without a preflight, and there are no CORS headers to pass it.
WRITE_HEADER = ("X-Memdrift", "ui")
#: ``serve`` answers no fetch at all, so its CSP can be ``default-src 'none'``.
#: This page is an application: it needs same-origin ``fetch`` and nothing
#: else, so the directive is opened for exactly that.
GUI_CSP = CSP + "; connect-src 'self'"
MAX_BODY = 16 * 1024
MAX_EVENTS = 500
MAX_MESSAGE = 200
#: A human acting through a browser is a human. Recording the click under the
#: vault's configured agent name would put the person's undo in the agent's mouth.
HUMAN = "you"
#: Recorded on a commit made from the page, in place of caller-supplied evidence.
GUI_EVIDENCE = "manual:memdrift ui"


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _evidence(items: Any) -> list[dict]:
    """Evidence arrives as dicts (fresh read) or a JSON string (sqlite)."""
    if not items:
        return []
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except json.JSONDecodeError:
            return []
    return [i for i in items if isinstance(i, dict)]


def _fingerprint(item: revert_mod.Plan) -> str:
    """Identifies a plan by its *raw* values.

    The page only ever sees redacted values, and redaction is lossy: two
    different absolute paths can shorten to the same ``~/…``. Comparing what
    was shown would call those two plans "the same". The fingerprint is taken
    before redaction, so a stale plan is detected even when the difference is
    one the human could not have seen.
    """
    raw = json.dumps(
        [item.path, item.field, item.before, item.after],
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _plan_fingerprint(plans: list[revert_mod.Plan]) -> str:
    """One fingerprint over a whole undo-a-commit plan.

    Order matters and is stable (the planner walks the commit's change list),
    so two different plans cannot collide by reordering their rows.
    """
    raw = json.dumps(
        [
            [p.path, p.field, p.before, p.after, p.skipped]
            for p in plans
        ],
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class Gui:
    """Every endpoint, as a plain function over (query | body). No HTTP here.

    Keeping the handlers free of ``self.request`` is what lets the test suite
    drive the real logic without a socket, and keeps the HTTP layer to routing.
    """

    def __init__(self, repo: Repo) -> None:
        self.repo = repo
        self.redactor = Redactor(root=str(repo.root))
        #: ``refs/head`` has no lock, so the only writer this UI can spawn is
        #: serialised here. Two tabs undoing at once take a number.
        self.write_lock = threading.Lock()

    # -------------------------------------------------------------- reads

    def state(self, query: dict) -> dict:
        """Everything the first screen needs, from ONE pass over history.

        One pass is the point: the numbers above the list and the list itself
        come from the same snapshot, so they cannot disagree with each other.
        """
        from .. import scope

        since = query.get("since")
        stream = build_events(self.repo, since=since, redactor=self.redactor)
        hidden = set(stream.hidden)
        return {
            "vault": self.repo.root.name,
            "has_history": bool(self.repo.head()),
            # A shared vault you may only read is a first-class case, not an
            # error: the page needs to know before it draws a Commit button.
            "writable": scope.allows(self.repo.root, write=True),
            "commits": stream.commits,
            "changes": len(stream.events),
            "risky": len(stream.risky),
            "unsure": len(stream.unsure),
            "hidden": len(stream.hidden),
            "events": stream.events[:MAX_EVENTS],
            "truncated": max(0, len(stream.events) - MAX_EVENTS),
            "page_list": [{"path": p.path, "title": p.title} for p in stream.pages],
            "pending": self._pending(hidden),
        }

    def page(self, query: dict) -> dict:
        target = self._page(query.get("path"))
        if not target:
            raise ApiError(HTTPStatus.BAD_REQUEST, "name a page")
        tree = self.repo.head_tree() if self.repo.head() else {}
        blob = tree.get(target)
        if blob is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "this page is not in the last commit")
        if is_private_page(self.repo, target):
            raise ApiError(HTTPStatus.FORBIDDEN, "this page is marked private")
        entries = ops.blame(self.repo, target)
        hidden = private_paths(self.repo)
        for entry in entries:
            entry["before"] = self.redactor.text(entry.get("before"))
            entry["after"] = self.redactor.text(entry.get("after"))
            # The commit message is free text and can carry a pasted path —
            # or name a private page it touched in the same commit. It is
            # shown in this pane like any other value, so it is redacted and
            # masked like any other value.
            entry["message"] = mask_private(
                self.redactor.text(entry.get("message")), hidden
            )
            entry["evidence"] = self.redactor.evidence(_evidence(entry.get("evidence")))
        return {"path": target, "fields": entries}

    def digest(self, query: dict) -> bytes:
        stream = build_events(self.repo, since=query.get("since"), redactor=self.redactor)
        return render_digest(
            stream, title=f"memdrift · {self.repo.root.name}"
        ).encode("utf-8")

    def mcp_config(self, _query: dict) -> dict:
        """Deliberately not redacted: this is for the human to paste, and a
        config with ``~`` in it does not launch anything. It never leaves the
        machine unless the human copies it out."""
        return {
            "mcpServers": {
                "memdrift": {
                    "command": sys.executable,
                    "args": ["-m", "memdrift", "-C", str(self.repo.root), "mcp"],
                }
            }
        }

    # ------------------------------------------------------------- writes

    def revert(self, body: dict) -> dict:
        """One field on one page. Plan first; apply only when ``dry_run`` is
        false *and* the plan still matches what the human approved."""
        page = self._page(body.get("page"))
        field_name = body.get("field")
        if not page or not field_name or not isinstance(field_name, str):
            raise ApiError(HTTPStatus.BAD_REQUEST, "a revert needs one page and one field")
        # Undo answers with before/after values, so it is a read too. The page
        # view refuses private pages; this endpoint must not be a way around it.
        if is_private_page(self.repo, page):
            raise ApiError(HTTPStatus.FORBIDDEN, "this page is marked private")
        dry_run = body.get("dry_run", True)
        if not isinstance(dry_run, bool):
            raise ApiError(HTTPStatus.BAD_REQUEST, "dry_run must be true or false")

        with self.write_lock:
            try:
                (item,) = revert_mod.plan(self.repo, field_name=field_name, page=page)
            except RepoError as exc:
                raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
            shown = self._shown(item)
            result = None
            if not dry_run:
                self._require_write()
                self._assert_approved(shown, body.get("expect"))
                result = revert_mod.execute(self.repo, [item], author_name=HUMAN)

        return {
            "dry_run": dry_run,
            "plans": [shown],
            "commit": short(result.commit["id"]) if result and result.commit else None,
            "reverts": short(result.commit.get("reverts")) if result and result.commit else None,
            "written": result.written if result else [],
        }

    def commit(self, body: dict) -> dict:
        """Record the working tree, as the list of pending files the human saw."""
        message = body.get("message")
        if not isinstance(message, str) or not message.strip():
            raise ApiError(HTTPStatus.BAD_REQUEST, "a commit needs a message")
        message = message.strip()
        if len(message) > MAX_MESSAGE:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"keep the message under {MAX_MESSAGE} characters")
        expect = body.get("expect")
        if not isinstance(expect, list):
            raise ApiError(HTTPStatus.BAD_REQUEST, "send the list of changes you looked at")
        reviewed = [_pair(p) for p in expect]
        if any(path is None or op is None for path, op in reviewed):
            # Checked, not sorted-then-crashed: comparing None with a string
            # raises inside sorted(), which would have been a 500 for a
            # malformed body instead of the 400 it is.
            raise ApiError(HTTPStatus.BAD_REQUEST, "each change needs a path and an op")

        with self.write_lock:
            self._require_write()
            pending = self._pending(set(self._hidden()))
            seen = sorted((p["path"], p["op"]) for p in pending)
            if seen != sorted(reviewed):
                raise ApiError(
                    HTTPStatus.CONFLICT,
                    "uncommitted changes moved since you looked — the list was refreshed, review it again",
                )
            payload = ops.commit(
                self.repo,
                message,
                evidence=[GUI_EVIDENCE],
                author_type="human",
                author_name=HUMAN,
            )
        if payload is None:
            return {"committed": False, "reason": "nothing changed"}
        return {
            "committed": True,
            "id": short(payload["id"]),
            "commit_id": payload["id"],
            "changes": len(payload.get("changes") or []),
            # The page offers "undo this" only when undoing has a meaning: the
            # very first commit has nothing before it to restore.
            "can_undo": bool(payload.get("parent")),
        }

    def undo(self, body: dict) -> dict:
        """Undo the commit that was just made, by appending one that says so.

        Scoped to HEAD on purpose. "Undo the newest commit" has exactly one
        meaning and a plan a human can read in one screen; "undo commit 12
        from here" needs three-way reasoning the CLI can print in full and
        the page cannot.
        """
        ref = body.get("commit")
        if not isinstance(ref, str) or not ref.strip():
            raise ApiError(HTTPStatus.BAD_REQUEST, "say which commit to undo")
        dry_run = body.get("dry_run", True)
        if not isinstance(dry_run, bool):
            raise ApiError(HTTPStatus.BAD_REQUEST, "dry_run must be true or false")

        head = self.repo.head()
        if not head:
            raise ApiError(HTTPStatus.BAD_REQUEST, "there is no commit to undo yet")
        try:
            target = self.repo.resolve(ref.strip())
        except RepoError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        if target != head:
            raise ApiError(
                HTTPStatus.CONFLICT,
                f"only the newest commit can be undone here — {short(target)} is not HEAD\n"
                f"  use: memdrift revert --commit {short(target)} --dry-run",
            )

        with self.write_lock:
            try:
                plans = revert_mod.plan(self.repo, commit_ref=head)
            except RepoError as exc:
                raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
            if any(is_private_page(self.repo, p.path) for p in plans if p.path):
                raise ApiError(HTTPStatus.FORBIDDEN, "this change touches a page marked private")
            shown = [self._shown(p) for p in plans]
            fingerprint = _plan_fingerprint(plans)
            result = None
            if not dry_run:
                self._require_write()
                self._assert_commit_approved(body.get("expect"), fingerprint, short(head))
                result = revert_mod.execute(
                    self.repo, plans, author_name=HUMAN, reverts=head
                )

        new_id = short(result.commit["id"]) if result and result.commit else None
        return {
            "dry_run": dry_run,
            "commit": short(head),
            "plans": shown,
            "fingerprint": fingerprint,
            "changes": sum(1 for p in plans if not p.skipped),
            "new_commit": new_id,
            # The old id never disappears; the page says so out loud.
            "reverts": short(head) if new_id else None,
            "written": result.written if result else [],
        }

    # ----------------------------------------------------------- internals

    def _page(self, raw: Any) -> str | None:
        """A path off the wire is untrusted: keep it inside the memory root."""
        if not raw or not isinstance(raw, str) or "\x00" in raw:
            return None
        try:
            resolved = (self.repo.root / raw).resolve()
        except (OSError, ValueError):
            return None
        if self.repo.root not in resolved.parents:
            raise ApiError(HTTPStatus.FORBIDDEN, "path outside the memory root")
        return resolved.relative_to(self.repo.root).as_posix()

    def _hidden(self) -> list[str]:
        return build_events(self.repo, redactor=self.redactor).hidden

    def _is_private(self, path: str, hidden: set[str]) -> bool:
        """Private in the last commit, or private in the file as it stands now.

        The second check matters for a page the agent created since the last
        commit: it is not in any snapshot yet, but it is already on disk with
        ``private: true`` and must not be named on the page.
        """
        if path in hidden:
            return True
        target = self.repo.root / path
        try:
            if target.is_file() and is_private(
                parse_page(path, target.read_text(encoding="utf-8")).fields
            ):
                return True
        except (OSError, ValueError):  # unreadable: leave it to commit to say so
            return False
        return False

    def _pending(self, hidden: set[str]) -> list[dict]:
        """Uncommitted changes as ``{path, op}``, private pages left out.

        The digest only ever reports a *count* of these, so it never had to
        ask whether one of them was a private page. Listing them by name does.
        """
        report = ops.status(self.repo)
        raw = [("add", p) for p in report.added]
        raw += [("modify", p) for p in report.modified]
        raw += [("delete", p) for p in report.deleted]
        raw += [("rename", new) for _old, new in report.renamed]
        return [
            {"path": self.redactor.path(path), "op": op}
            for op, path in raw
            if not self._is_private(path, hidden)
        ]

    def _shown(self, item: revert_mod.Plan) -> dict:
        """The plan as the page is allowed to see it: redacted and clipped,
        plus a fingerprint over the raw values that the page sends back."""
        shown = {
            "path": self.redactor.path(item.path),
            "field": item.field,
            "skipped": item.skipped,
            "reason": item.reason,
            "note": item.note,
        }
        if not item.skipped:
            shown["before"] = _clip(self.redactor.text(item.before))
            shown["after"] = _clip(self.redactor.text(item.after))
            shown["fingerprint"] = _fingerprint(item)
        return shown

    def _assert_approved(self, shown: dict, expect: Any) -> None:
        """Refuse to apply a plan that is not the one the human approved."""
        if not isinstance(expect, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "send back the plan you were shown")
        if shown["skipped"]:
            raise ApiError(HTTPStatus.CONFLICT, "this change is no longer there")
        if (
            expect.get("fingerprint") != shown["fingerprint"]
            or expect.get("path") != shown["path"]
            or (expect.get("field") or None) != shown["field"]
        ):
            raise ApiError(
                HTTPStatus.CONFLICT,
                "this value changed since you looked at it — review, then try again",
            )

    def _assert_commit_approved(self, expect: Any, fingerprint: str, commit: str) -> None:
        """Same rule as a single-field undo, one level up: the *whole* plan the
        human read, not just its first row."""
        if not isinstance(expect, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "send back the plan you were shown")
        if expect.get("fingerprint") != fingerprint or expect.get("commit") != commit:
            raise ApiError(
                HTTPStatus.CONFLICT,
                "this commit no longer undoes the way you saw it — review, then try again",
            )

    def _require_write(self) -> None:
        """Re-read the space list on every write.

        The process was started with permission; that is not the same as having
        it now. Revoking a space in another terminal takes effect on the next
        click, without restarting the server.
        """
        from .. import scope

        try:
            scope.check(self.repo.root, write=True)
        except scope.OutOfScope as exc:
            raise ApiError(HTTPStatus.FORBIDDEN, str(exc)) from exc


def _clip(value: Any, limit: int = 500) -> Any:
    """A section can be pages long; the confirmation box cannot."""
    if isinstance(value, str) and len(value) > limit:
        return value[: limit - 1].rstrip() + "…"
    return value


def _pair(item: Any) -> tuple[str | None, str | None]:
    """One ``expect`` entry as a (path, op) pair; anything else as nothing."""
    if not isinstance(item, dict):
        return (None, None)
    path = item.get("path") if isinstance(item.get("path"), str) else None
    op = item.get("op") if isinstance(item.get("op"), str) else None
    return (path, op)


class Handler(BaseHTTPRequestHandler):
    server_version = "memdrift-ui"
    sys_version = ""

    # injected by make_server
    gui: Gui
    token: str

    def log_message(self, fmt: str, *args) -> None:
        if self.server.verbose:  # type: ignore[attr-defined]
            super().log_message(fmt, *args)

    # ------------------------------------------------------------- plumbing

    def _reply(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Security-Policy", GUI_CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200, extra: dict | None = None) -> None:
        self._reply(
            status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8", extra,
        )

    def _fail(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    #: Refusing a write before reading its body leaves bytes in the socket.
    #: Closing with unread data sends RST on Windows and the browser shows a
    #: network error instead of the 403 we just wrote. So swallow a bounded
    #: amount first — bounded, because "Content-Length: 9GB" is not an
    #: invitation to read 9GB, and this is a loopback server with a token.
    DRAIN_MAX = 1 << 20

    def _drain(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return
        if length <= 0:
            return
        remaining = min(length, self.DRAIN_MAX)
        try:
            self.connection.settimeout(2.0)
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    return
                remaining -= len(chunk)
        except OSError:
            pass

    def _refuse(self, status: int, message: str) -> None:
        """A refusal the client can actually read."""
        self._drain()
        self._fail(status, message)
        self.close_connection = True

    def _gate(self, *, write: bool) -> dict | None:
        """All the locks. A response has been sent (and None returned) as soon
        as one of them fails, so a refused request never reaches a handler."""
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        host = self.headers.get("Host")
        if not host_header_allowed(host):
            self._refuse(HTTPStatus.FORBIDDEN, "wrong Host header")
            return None
        if not origin_matches_host(self.headers.get("Origin"), host):
            self._refuse(HTTPStatus.FORBIDDEN, "cross-origin request")
            return None
        supplied = (query.get("t") or [""])[0]
        # isascii first: compare_digest refuses non-ASCII strings, and a
        # garbage token must be a 403, not a traceback in the handler thread.
        if not supplied.isascii() or not secrets.compare_digest(supplied, self.token):
            self._refuse(HTTPStatus.FORBIDDEN, "missing or wrong token")
            return None
        if write:
            if self.headers.get(WRITE_HEADER[0]) != WRITE_HEADER[1]:
                self._refuse(HTTPStatus.FORBIDDEN, "writes need the X-Memdrift header")
                return None
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
            if ctype != "application/json":
                self._refuse(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "send JSON")
                return None
        return {k: v[0] for k, v in query.items()}

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            # Same reason as ``_refuse``: leaving the body in the socket makes
            # Windows answer with RST and the page shows a network error
            # instead of the 413.
            self._drain()
            self.close_connection = True
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body too large")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError(HTTPStatus.BAD_REQUEST, "body is not JSON")
        if not isinstance(body, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "body must be a JSON object")
        return body

    # -------------------------------------------------------------- routes

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        query = self._gate(write=False)
        if query is None:
            return
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            if path == "/":
                self._reply(HTTPStatus.OK, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/state":
                self._json(self.gui.state(query))
            elif path == "/api/page":
                self._json(self.gui.page(query))
            elif path == "/api/mcp-config":
                self._json(self.gui.mcp_config(query))
            elif path == "/api/digest":
                self._reply(
                    HTTPStatus.OK,
                    self.gui.digest(query), "text/html; charset=utf-8",
                    {"Content-Disposition": f'attachment; filename="{default_filename()}"'},
                )
            else:
                self._fail(HTTPStatus.NOT_FOUND, "not found")
        except ApiError as exc:
            self._fail(exc.status, exc.message)
        except (ValueError, RepoError) as exc:
            self._fail(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:  # a broken memory file must not kill the server
            self._fail(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}")

    def do_POST(self) -> None:  # noqa: N802
        query = self._gate(write=True)
        if query is None:
            return
        path = urlparse(self.path).path.rstrip("/")
        try:
            body = self._body()
            if path == "/api/revert":
                self._json(self.gui.revert(body))
            elif path == "/api/undo":
                self._json(self.gui.undo(body))
            elif path == "/api/commit":
                self._json(self.gui.commit(body))
            else:
                self._fail(HTTPStatus.NOT_FOUND, "not found")
        except ApiError as exc:
            self._fail(exc.status, exc.message)
        except (ValueError, RepoError) as exc:
            self._fail(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:
            self._fail(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}")


class Server(Server):  # noqa: F811 — same server, named so its origin is obvious
    pass


def make_server(repo: Repo, host: str = "127.0.0.1", port: int = 0, verbose: bool = False) -> Server:
    token = secrets.token_urlsafe(24)
    handler = type("BoundHandler", (Handler,), {"gui": Gui(repo), "token": token})
    bound = check_host(host)
    return Server((bound, port), handler, repo=repo, token=token, verbose=verbose)


def url_for(server: Server) -> str:
    host, port = server.server_address[:2]
    return f"http://{host}:{port}/?t={server.token}"


def serve(repo: Repo, host: str = "127.0.0.1", port: int = 7788, *, verbose: bool = False, on_ready=None) -> None:
    server = make_server(repo, host=host, port=port, verbose=verbose)
    if on_ready:
        on_ready(server)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
