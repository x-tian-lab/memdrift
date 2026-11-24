"""Local read-only web UI. Bound to loopback, on purpose.

``memdrift serve`` is for *you*, on *your* machine, looking at your own history
without leaving the terminal. It is not a sharing mechanism. Sharing is
``memdrift digest``, which writes a file you can hand over deliberately.

The refusal to bind anything but loopback is a decision, not a default:

* A memory directory is a biography. Exposing it on a LAN, let alone the
  internet, is not a feature anyone should get by adding ``--host 0.0.0.0``.
* There is no auth model, and bolting one on would be the wrong kind of
  design: it implies this endpoint is meant to be reachable.

What it does take seriously: a browser on the same machine is still a hostile
environment. Other sites can issue requests to ``localhost``. So every server
instance mints a random token, the URL carries it, and any request without a
matching token is rejected — a cross-site ``GET`` cannot guess it. Responses
also carry a restrictive CSP so a malicious memory file cannot escalate
through the UI.

Everything served comes from ``events.build`` — the same event stream the
digest uses — so the two views cannot drift, and neither can skip redaction.
"""

from __future__ import annotations

import json
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .digest import CSS, JS
from .events import build as build_events
from .events import working_tree
from .repo import Repo
from .security import Redactor

LOOPBACK = "127.0.0.1"

CSP = (
    "default-src 'none'; "
    "style-src 'unsafe-inline'; "
    "script-src 'unsafe-inline'; "
    "img-src data:; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)


class LoopbackOnly(ValueError):
    """Raised when someone asks for a public bind."""


def check_host(host: str) -> str:
    """Only loopback. No opt-out flag exists, deliberately."""
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise LoopbackOnly(
            f"refusing to bind {host!r}: serve is loopback-only. "
            "To share a digest, run `memdrift digest` and send the html file."
        )
    return LOOPBACK if host in {"localhost", "127.0.0.1"} else "::1"


LOOPBACK_NAMES = {"127.0.0.1", "localhost", "::1"}


def host_header_allowed(header: str | None) -> bool:
    """Does this request's ``Host`` name *this machine*?

    The token in the URL is the primary defence, so this looks redundant —
    until DNS rebinding. An attacker's page at ``evil.test`` keeps its own
    name in ``Host`` after that name is re-pointed at 127.0.0.1, so the
    browser sends our server ``Host: evil.test`` and the page can read the
    response. The token would still stop it (the attacker cannot guess it),
    but a second lock costs one line and removes the class entirely.

    A request with no ``Host`` is a script, not a browser; those are allowed
    because the token already gates them.
    """
    if not header or not header.strip():
        return True
    value = header.strip().lower()
    if value.startswith("["):  # [::1]:7787
        name = value[1:].split("]", 1)[0]
    elif value.count(":") == 1:  # 127.0.0.1:7787
        name = value.rsplit(":", 1)[0]
    else:
        name = value
    return name in LOOPBACK_NAMES


def origin_matches_host(origin: str | None, host: str | None) -> bool:
    """A browser adds ``Origin`` to every cross-site request; require it to
    name the same host we were addressed by. No ``Origin`` means no browser."""
    if not origin:
        return True
    from urllib.parse import urlsplit

    try:
        netloc = urlsplit(origin.strip()).netloc.lower()
    except ValueError:
        return False
    return bool(netloc) and netloc == (host or "").strip().lower()


PAGE_CSS = CSS + """
nav { display: flex; gap: 18px; margin: 22px 0 6px; font-size: 13.5px; }
nav a { color: var(--accent); text-decoration: none; }
nav a:hover { text-decoration: underline; }
.same { margin: 10px 0 0; font-size: 13px; color: var(--muted); }
table.simple { border-collapse: collapse; width: 100%; font-size: 13.5px; }
table.simple th { text-align: left; font-size: 11px; text-transform: uppercase;
                  letter-spacing: 0.07em; color: var(--faint); font-weight: 600;
                  padding: 0 12px 8px 0; border-bottom: 1px solid var(--line); }
table.simple td { padding: 8px 12px 8px 0; border-bottom: 1px solid var(--line);
                  vertical-align: top; }
table.simple td.mono { font-family: var(--mono); font-size: 12.5px; color: var(--muted); }
a.row-link { color: inherit; text-decoration: none; }
a.row-link:hover .mono { color: var(--accent); }
.notice { border-left: 3px solid var(--risky); padding: 8px 0 8px 12px;
          color: var(--muted); font-size: 13px; margin: 14px 0; }
"""


def _page(title: str, body: str, token: str) -> bytes:
    doc = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>{title}</title><style>{PAGE_CSS}</style></head>
<body><div class="wrap">
<nav><a href="/?t={token}">overview</a><a href="/pages?t={token}">pages</a>
<a href="/history?t={token}">history</a></nav>
{body}
</div><script>{JS}</script></body></html>"""
    return doc.encode("utf-8")


def _esc(text) -> str:
    from html import escape

    return escape("" if text is None else str(text), quote=True)


class _Handler(BaseHTTPRequestHandler):
    server_version = "memdrift"
    sys_version = ""

    # injected by the factory
    repo: Repo
    token: str

    def log_message(self, fmt: str, *args) -> None:  # keep the terminal clean
        if self.server.verbose:  # type: ignore[attr-defined]
            super().log_message(fmt, *args)

    # ---------------------------------------------------------------- helpers

    def _deny(self, status: HTTPStatus, message: str) -> None:
        body = f'<h1>{_esc(message)}</h1><p class="sub">memdrift serve</p>'.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send(self, body: bytes, ctype: str = "text/html; charset=utf-8") -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self, query: dict) -> bool:
        supplied = (query.get("t") or [""])[0]
        return secrets.compare_digest(supplied, self.token)

    def _request_allowed(self) -> tuple[bool, str]:
        """Three locks in order: Host names us, Origin (if any) agrees, token matches."""
        host = self.headers.get("Host")
        if not host_header_allowed(host):
            return False, "wrong Host header"
        if not origin_matches_host(self.headers.get("Origin"), host):
            return False, "cross-origin request"
        return True, ""

    def _redactor(self, query: dict) -> Redactor:
        keep_quotes = (query.get("quotes") or ["1"])[0] != "0"
        return Redactor(root=str(self.repo.root), keep_quotes=keep_quotes)

    # ---------------------------------------------------------------- routes

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        ok, why = self._request_allowed()
        if not ok:
            self._deny(HTTPStatus.FORBIDDEN, why)
            return
        if not self._authorised(query):
            self._deny(HTTPStatus.FORBIDDEN, "missing or wrong token")
            return
        path = parsed.path.rstrip("/") or "/"
        try:
            if path == "/":
                self._send(self._overview(query))
            elif path == "/pages":
                self._send(self._pages(query))
            elif path == "/history":
                self._send(self._history(query))
            elif path.startswith("/page/"):
                self._send(self._page_detail(path[len("/page/") :], query))
            else:
                self._deny(HTTPStatus.NOT_FOUND, "not found")
        except Exception as exc:  # a broken memory file must not kill the server
            self._deny(HTTPStatus.INTERNAL_SERVER_ERROR, f"error: {type(exc).__name__}")

    # ---------------------------------------------------------------- views

    def _overview(self, query: dict) -> bytes:
        from .events import human_time

        stream = build_events(self.repo, redactor=self._redactor(query))
        pending = working_tree(self.repo)
        rows = []
        for event in stream.events[:60]:
            when = human_time(event.get("ts"))
            flag = ' <span class="tag warn">review</span>' if event.get("risk") else ""
            field = event.get("field") or event.get("op")
            delta = ""
            if event.get("field"):
                delta = f'{_esc(event.get("before"))} → <b>{_esc(event.get("after"))}</b>'
            elif event.get("op") == "rename":
                delta = f'{_esc(event.get("from"))} → <b>{_esc(event.get("path"))}</b>'
            else:
                delta = f'<b>{_esc(event.get("op"))}</b>'
            rows.append(
                f'<tr><td class="mono">{_esc(when)}</td>'
                f'<td class="mono">{_esc(event.get("short"))}</td>'
                f'<td>{_esc(event.get("author", {}).get("name"))}</td>'
                f'<td class="mono"><a class="row-link" href="/page/{_esc(event.get("path"))}?t={self.token}">'
                f'{_esc(event.get("path"))}</a></td>'
                f"<td>{_esc(field)}{flag}</td><td>{delta}</td></tr>"
            )
        pending_note = ""
        if pending:
            pending_note = f'<p class="notice">{len(pending)} uncommitted change(s) in the working tree.</p>'
        body = f"""
<header><h1>memdrift</h1>
<p class="sub">local view · <code>{_esc(self.repo.root.name)}</code> ·
{stream.commits} commit(s) · {len(stream.events)} change(s) ·
{len(stream.hidden)} private page(s) hidden</p></header>
{pending_note}
<h2>Latest changes</h2>
<table class="simple">
<tr><th>when</th><th>commit</th><th>who</th><th>page</th><th>field</th><th>change</th></tr>
{"".join(rows) or '<tr><td colspan="6" class="mono">nothing recorded yet</td></tr>'}
</table>
<p class="same">Loopback only. Share a <code>memdrift digest</code> file instead of this URL.</p>
"""
        return _page("memdrift", body, self.token)

    def _pages(self, query: dict) -> bytes:
        stream = build_events(self.repo, redactor=self._redactor(query))
        rows = "".join(
            f'<tr><td class="mono"><a class="row-link" href="/page/{_esc(p.path)}?t={self.token}">'
            f'{_esc(p.path)}</a></td><td>{_esc(p.title)}</td></tr>'
            for p in stream.pages
        )
        hidden = ""
        if stream.hidden:
            hidden = (
                f'<p class="notice">{len(stream.hidden)} page(s) marked private are not listed '
                f"and never appear in exports.</p>"
            )
        body = f"""
<header><h1>Pages</h1><p class="sub">{len(stream.pages)} tracked markdown file(s)</p></header>
{hidden}
<table class="simple"><tr><th>path</th><th>title</th></tr>
{rows or '<tr><td colspan="2" class="mono">none</td></tr>'}</table>
"""
        return _page("pages · memdrift", body, self.token)

    def _history(self, query: dict) -> bytes:
        from .events import group_by_commit, human_time
        from .index import parse_since

        since = (query.get("since") or [None])[0]
        stream = build_events(self.repo, since=since, redactor=self._redactor(query))
        rows = []
        for group in group_by_commit(stream.events):
            author = group.get("author") or {}
            rows.append(
                f'<tr><td class="mono">{_esc(human_time(group.get("ts")))}</td>'
                f'<td class="mono">{_esc(group.get("short"))}</td>'
                f'<td>{_esc(group.get("message"))}</td>'
                f'<td>{_esc(author.get("name"))} · {_esc(author.get("type"))}</td>'
                f'<td class="mono">{len(group["events"])}</td></tr>'
            )
        body = f"""
<header><h1>History</h1><p class="sub">window <code>{_esc(since or "all")}</code> ·
{stream.commits} commit(s)</p></header>
<table class="simple"><tr><th>when</th><th>commit</th><th>message</th><th>who</th><th>#</th></tr>
{"".join(rows) or '<tr><td colspan="5" class="mono">empty</td></tr>'}</table>
"""
        return _page("history · memdrift", body, self.token)

    def _page_detail(self, raw_path: str, query: dict) -> bytes:
        from urllib.parse import unquote

        from .events import human_time
        from .operations import blame

        target = unquote(raw_path)
        # A path from the URL is untrusted: keep it inside the memory root.
        resolved = (self.repo.root / target).resolve()
        if self.repo.root not in resolved.parents and resolved != self.repo.root:
            self._deny(HTTPStatus.FORBIDDEN, "path outside memory root")
            return b""
        redactor = self._redactor(query)
        entries = blame(self.repo, target)
        rows = []
        for entry in entries:
            delta = ""
            if entry.get("before") is not None or entry.get("after") is not None:
                delta = f'{_esc(entry.get("before"))} → <b>{_esc(entry.get("after"))}</b>'
            else:
                delta = f'<b>{_esc(entry.get("op"))}</b>'
            rows.append(
                f'<tr><td class="mono">{_esc(entry.get("field"))}</td><td>{delta}</td>'
                f'<td class="mono">{_esc(entry.get("commit"))}</td>'
                f'<td class="mono">{_esc(human_time(entry.get("ts")))}</td>'
                f'<td>{_esc(entry.get("author"))}</td></tr>'
            )
        body = f"""
<header><h1>{_esc(target)}</h1>
<p class="sub">last change per field · {len(entries)} field(s)</p></header>
<table class="simple"><tr><th>field</th><th>change</th><th>commit</th><th>when</th><th>who</th></tr>
{"".join(rows) or '<tr><td colspan="5" class="mono">no history</td></tr>'}</table>
"""
        return _page(f"{target} · memdrift", body, self.token)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, handler, *, repo: Repo, token: str, verbose: bool = False):
        super().__init__(addr, handler)
        self.repo = repo
        self.token = token
        self.verbose = verbose


def make_server(repo: Repo, host: str = LOOPBACK, port: int = 0, verbose: bool = False) -> Server:
    token = secrets.token_urlsafe(24)
    handler = type("BoundHandler", (_Handler,), {"repo": repo, "token": token})
    bound = check_host(host)
    return Server((bound, port), handler, repo=repo, token=token, verbose=verbose)


def url_for(server: Server) -> str:
    host, port = server.server_address[:2]
    return f"http://{host}:{port}/?t={server.token}"


def serve(repo: Repo, host: str = LOOPBACK, port: int = 7787, *, verbose: bool = False, on_ready=None) -> None:
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
