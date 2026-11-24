"""Drive the MCP server over a real pipe, the way a client does.

Not a unit test of `handle()` — that would pass even if framing were broken.
This spawns `python -m memdrift mcp` as a subprocess, writes bytes to its
stdin, and reads responses off its stdout. If a client can talk to it, this
test notices; if only our own helper functions can, this test fails.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Client:
    def __init__(self, cwd: Path) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "memdrift", "-C", str(cwd), "mcp"],
            cwd=str(ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._id = 0

    def send(self, method: str, params: dict | None = None, *, notify: bool = False) -> dict | None:
        message: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notify:
            self._id += 1
            message["id"] = self._id
        self.proc.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
        self.proc.stdin.flush()
        if notify:
            return None
        line = self.proc.stdout.readline()
        assert line, f"server closed the pipe (stderr: {self.proc.stderr.read1(4000)!r})"
        return json.loads(line)

    def call(self, name: str, **arguments) -> dict:
        response = self.send("tools/call", {"name": name, "arguments": arguments})
        assert "result" in response, response
        return response["result"]["structuredContent"]

    def error(self, name: str, **arguments) -> dict:
        response = self.send("tools/call", {"name": name, "arguments": arguments})
        assert "error" in response, response
        return response["error"]

    def close(self) -> None:
        self.proc.stdin.close()
        self.proc.wait(timeout=10)


def open_client(vault: Path) -> Client:
    client = Client(vault)
    hello = client.send(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1"},
        },
    )
    assert hello["result"]["serverInfo"]["name"] == "memdrift"
    client.send("notifications/initialized", notify=True)
    return client


# ------------------------------------------------------------------ handshake


def test_initialize_advertises_tools(vault):
    client = open_client(vault)
    try:
        caps = client.send("initialize", {"protocolVersion": "2025-06-18"})["result"]["capabilities"]
        assert "tools" in caps
    finally:
        client.close()


def test_tools_list_has_dotted_names_and_hints(vault):
    client = open_client(vault)
    try:
        tools = client.send("tools/list")["result"]["tools"]
    finally:
        client.close()

    names = [t["name"] for t in tools]
    assert "memory.history" in names
    assert "memory.revert" in names
    assert all(n.startswith("memory.") for n in names)

    by_name = {t["name"]: t for t in tools}
    assert by_name["memory.history"]["annotations"]["readOnlyHint"] is True
    assert by_name["memory.revert"]["annotations"]["readOnlyHint"] is False
    assert by_name["memory.revert"]["annotations"]["destructiveHint"] is True
    assert by_name["memory.blame"]["inputSchema"]["required"] == ["page"]


def test_unknown_method_and_tool_are_structured_errors(vault):
    client = open_client(vault)
    try:
        assert client.send("nope/nope")["error"]["code"] == -32601
        assert client.error("memory.nope")["code"] == -32602
        assert "message" in client.error("memory.commit")["message"].lower()
    finally:
        client.close()


def test_notification_gets_no_response(vault):
    """A server that answers a notification desynchronises the client."""
    client = open_client(vault)
    try:
        client.send("notifications/initialized", notify=True)
        # If a stray response were queued, this next call would read it and
        # the id would not match.
        response = client.send("tools/list")
        assert response["id"] == client._id
    finally:
        client.close()


# ---------------------------------------------------------------------- tools


def test_history_reports_real_commits(vault):
    from click.testing import CliRunner

    from memdrift.cli import main

    CliRunner().invoke(main, ["init", str(vault)])
    acme = vault / "clients" / "acme.md"
    acme.write_text(acme.read_text(encoding="utf-8").replace('"$45k"', '"$48k"'), encoding="utf-8")
    CliRunner().invoke(main, ["-C", str(vault), "commit", "-m", "quote updated"])

    client = open_client(vault)
    try:
        result = client.call("memory.history", limit=5)
    finally:
        client.close()
    assert result["commits"][0]["message"] == "quote updated"
    assert len(result["commits"][0]["id"]) == 7


def test_blame_carries_evidence_and_values(vault):
    from click.testing import CliRunner

    from memdrift.cli import main

    CliRunner().invoke(main, ["init", str(vault)])
    acme = vault / "clients" / "acme.md"
    acme.write_text(acme.read_text(encoding="utf-8").replace('"$45k"', '"$48k"'), encoding="utf-8")
    CliRunner().invoke(
        main,
        ["-C", str(vault), "commit", "-m", "quote", "--evidence", "email:msg-1", "--confidence", "0.8"],
    )

    client = open_client(vault)
    try:
        result = client.call("memory.blame", page="clients/acme.md")
    finally:
        client.close()
    pricing = next(f for f in result["fields"] if f["field"] == "frontmatter.pricing")
    assert pricing["after"] == "$48k"
    assert pricing["evidence"][0]["ref"] == "msg-1"


def test_page_reads_head_and_refuses_private(vault):
    from click.testing import CliRunner

    from memdrift.cli import main

    CliRunner().invoke(main, ["init", str(vault)])
    (vault / "secret.md").write_text("---\nprivate: true\ntitle: Diary\n---\n# Diary\n\n今日心情。\n", encoding="utf-8")
    CliRunner().invoke(main, ["-C", str(vault), "commit", "-m", "add diary"])

    client = open_client(vault)
    try:
        page = client.call("memory.page", page="clients/acme.md")
        assert page["fields"]["pricing"] == "$45k"
        denial = client.error("memory.page", page="secret.md")
        assert "private" in denial["message"]
    finally:
        client.close()


def test_path_traversal_is_refused(vault):
    client = open_client(vault)
    try:
        err = client.error("memory.page", page="../../../etc/passwd")
    finally:
        client.close()
    assert err["code"] == -32602
    assert "escapes" in err["message"]


def test_commit_is_a_writer_and_reports_nothing_to_do(vault):
    from click.testing import CliRunner

    from memdrift.cli import main

    CliRunner().invoke(main, ["init", str(vault)])
    client = open_client(vault)
    try:
        noop = client.call("memory.commit", message="nothing changed yet")
        assert noop["committed"] is False

        acme = vault / "clients" / "acme.md"
        acme.write_text(acme.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        done = client.call("memory.commit", message="touch", evidence=["email:msg-2"], confidence=0.7)
        assert done["committed"] is True

        history = client.call("memory.history", limit=1)
        assert history["commits"][0]["author"] == "agent:mcp"
    finally:
        client.close()


def test_revert_defaults_to_dry_run_then_applies(vault):
    """The safety property: an agent cannot undo by accident."""
    from click.testing import CliRunner

    from memdrift.cli import main

    CliRunner().invoke(main, ["init", str(vault)])
    acme = vault / "clients" / "acme.md"
    original = acme.read_text(encoding="utf-8")
    acme.write_text(original.replace('"$45k"', '"$12k"'), encoding="utf-8")
    CliRunner().invoke(main, ["-C", str(vault), "commit", "-m", "bad extract"])

    client = open_client(vault)
    try:
        peek = client.call("memory.revert", field="frontmatter.pricing", page="clients/acme.md")
        assert peek["dry_run"] is True
        assert acme.read_text(encoding="utf-8") == original.replace('"$45k"', '"$12k"')

        client.call("memory.revert", field="frontmatter.pricing", page="clients/acme.md", dry_run=False)
        assert acme.read_text(encoding="utf-8") == original

        log = client.call("memory.history", limit=2)
        assert log["commits"][0]["message"].startswith("revert:")
        assert log["commits"][1]["message"] == "bad extract"
    finally:
        client.close()


def test_digest_returns_structured_events(vault):
    from click.testing import CliRunner

    from memdrift.cli import main

    CliRunner().invoke(main, ["init", str(vault)])
    acme = vault / "clients" / "acme.md"
    acme.write_text(acme.read_text(encoding="utf-8").replace("active", "churned"), encoding="utf-8")
    CliRunner().invoke(main, ["-C", str(vault), "commit", "-m", "status"])

    client = open_client(vault)
    try:
        result = client.call("memory.digest")
    finally:
        client.close()
    assert result["commits"] >= 1
    fields = {e["field"] for e in result["events"]}
    assert "frontmatter.status" in fields


def test_no_tool_leaks_an_absolute_path(vault):
    """The same guarantee the digest has, applied to the agent channel.

    A path pasted into a value or an evidence ref must come back as `~/...`,
    never as `C:\\Users\\...` — otherwise an agent in another editor tab sees
    the OS account name that the human deliberately hid from the digest.
    """
    from click.testing import CliRunner

    from memdrift.cli import main

    CliRunner().invoke(main, ["init", str(vault)])
    acme = vault / "clients" / "acme.md"
    machine_path = str(vault / "clients" / "contract.pdf")
    acme.write_text(
        acme.read_text(encoding="utf-8") + f"\nSource: {machine_path}\n",
        encoding="utf-8",
    )
    CliRunner().invoke(
        main,
        ["-C", str(vault), "commit", "-m", "note", "--evidence", f"file:{machine_path}"],
    )

    client = open_client(vault)
    try:
        outputs = [
            client.call("memory.history"),
            client.call("memory.blame", page="clients/acme.md"),
            client.call("memory.page", page="clients/acme.md"),
            client.call("memory.digest"),
        ]
    finally:
        client.close()

    blob = json.dumps(outputs, ensure_ascii=False)
    assert machine_path not in blob
    assert "C:\\Users" not in blob and "C:/Users" not in blob
    assert "AppData" not in blob, [
        blob[max(0, m.start() - 200) : m.start() + 80]
        for m in re.finditer("AppData", blob)
    ]


# ------------------------------------------------------------------- framing


def test_content_length_framing_works(vault):
    """LSP-style clients must work too, or half the ecosystem cannot connect."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "memdrift", "-C", str(vault), "mcp"],
        cwd=str(ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    message = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode("utf-8")
    proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(message) + message)
    proc.stdin.flush()

    header = proc.stdout.readline()
    assert header.lower().startswith(b"content-length:")
    blank = proc.stdout.readline()
    assert blank in (b"\r\n", b"\n")
    length = int(header.split(b":")[1].strip())
    body = json.loads(proc.stdout.read(length))
    proc.stdin.close()
    proc.wait(timeout=10)

    assert body["id"] == 1
    assert body["result"]["tools"]


def test_bad_json_does_not_kill_the_server(vault):
    proc = subprocess.Popen(
        [sys.executable, "-m", "memdrift", "-C", str(vault), "mcp"],
        cwd=str(ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    proc.stdin.write(b"this is not json\n")
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/list"}).encode() + b"\n")
    proc.stdin.flush()

    first = json.loads(proc.stdout.readline())
    assert first["error"]["code"] == -32700
    second = json.loads(proc.stdout.readline())
    assert second["id"] == 9
    proc.stdin.close()
    proc.wait(timeout=10)


def test_eof_exits_cleanly(vault):
    client = open_client(vault)
    client.proc.stdin.close()
    assert client.proc.wait(timeout=10) == 0
