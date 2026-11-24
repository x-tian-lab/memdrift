"""JSON-RPC 2.0 over stdio, the smallest correct MCP transport.

Why hand-rolled instead of the official SDK: this package's only job is to sit
next to a memory directory and answer questions about it. Pulling in a web
framework to talk on stdin/stdout would be more code than the thing it serves,
and every dependency is a thing that can break in *someone else's* editor.
Two hundred lines of framing is cheaper than that risk.

Two frame shapes exist in the wild and both must work, because a server that
only speaks one is a server that mysteriously "fails to connect" in half the
clients:

* **newline-delimited** — one JSON object per line (what most CLIs do)
* **Content-Length framed** — ``Content-Length: 42\\r\\n\\r\\n{...}`` (LSP style)

We sniff which one is arriving and answer in kind. Detection is per-message,
not per-process, so a client that starts framed and degrades still works.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable

#: JSON-RPC codes we actually use. Nothing exotic: the spec's own numbers.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class RpcError(Exception):
    """Raised by a handler to return a structured error to the client."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _read_framed(stream, first_line: bytes) -> dict | None:
    """Read one ``Content-Length`` framed message.

    ``first_line`` is the header line already consumed by the sniffer; passing
    it in (rather than pushing it back) is what keeps the two framings from
    stepping on each other.
    """
    length: int | None = None
    line = first_line
    while line:
        if line in (b"\r\n", b"\n"):
            break
        name, _, value = line.decode("ascii", "replace").partition(":")
        if name.strip().lower() == "content-length":
            try:
                length = int(value.strip())
            except ValueError:
                return None
        line = stream.readline()
    if length is None:
        return None
    body = stream.read(length)
    if not body:
        return None
    return json.loads(body.decode("utf-8"))


def read_message(stream) -> tuple[dict | None, str]:
    """Read one message; return ``(payload, framing)``. ``payload`` None = EOF."""
    while True:
        line = stream.readline()
        if not line:
            return None, "eof"
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith(b"content-length"):
            return _read_framed(stream, line), "framed"
        try:
            return json.loads(stripped.decode("utf-8")), "line"
        except json.JSONDecodeError:
            return {"__parse_error__": stripped.decode("utf-8", "replace")}, "line"


def write_message(stream, payload: dict, framing: str = "line") -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if framing == "framed":
        data = body.encode("utf-8")
        stream.write(b"Content-Length: %d\r\n\r\n" % len(data))
        stream.write(data)
    else:
        stream.write(body.encode("utf-8") + b"\n")
    stream.flush()


class Server:
    """Method table + lifecycle. Knows nothing about memory or git."""

    def __init__(self, name: str, version: str) -> None:
        self.name = name
        self.version = version
        self._methods: dict[str, Callable[[dict], Any]] = {}
        self._initialized = False

    def method(self, name: str) -> Callable:
        def register(fn: Callable[[dict], Any]) -> Callable:
            self._methods[name] = fn
            return fn

        return register

    # ------------------------------------------------------------- lifecycle

    def handle(self, request: dict, framing: str) -> dict | None:
        """Dispatch one message. Returns the response, or None for notifications."""
        if "__parse_error__" in request:
            return _error(None, PARSE_ERROR, "could not parse JSON", request["__parse_error__"])

        req_id = request.get("id")
        method = request.get("method")
        is_notification = "id" not in request

        if not isinstance(method, str):
            return None if is_notification else _error(req_id, INVALID_REQUEST, "missing method")

        if is_notification:
            # `notifications/initialized`, `notifications/cancelled`, and any
            # client chatter we do not model. Never answer a notification:
            # an unsolicited response is a protocol violation.
            if method == "notifications/initialized":
                self._initialized = True
            return None

        try:
            result = self._dispatch(method, request.get("params") or {})
        except RpcError as exc:
            return _error(req_id, exc.code, exc.message, exc.data)
        except Exception as exc:  # a tool crash must not kill the server
            return _error(req_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def _dispatch(self, method: str, params: dict) -> Any:
        if method == "initialize":
            self._initialized = True
            return {
                "protocolVersion": params.get("protocolVersion") or "2025-06-18",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": self.name, "version": self.version},
            }
        if method == "ping":
            return {}
        handler = self._methods.get(method)
        if handler is None:
            raise RpcError(METHOD_NOT_FOUND, f"unknown method: {method}")
        return handler(params)

    # ------------------------------------------------------------------ loop

    def serve(self, stdin=None, stdout=None) -> int:
        stdin = stdin or sys.stdin.buffer
        stdout = stdout or sys.stdout.buffer
        while True:
            try:
                request, framing = read_message(stdin)
            except (json.JSONDecodeError, UnicodeDecodeError):
                write_message(stdout, _error(None, PARSE_ERROR, "bad frame"))
                continue
            if request is None:
                return 0
            response = self.handle(request, framing)
            if response is not None:
                write_message(stdout, response, framing)


def _error(req_id: Any, code: int, message: str, data: Any = None) -> dict:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": error}
