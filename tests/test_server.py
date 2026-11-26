from __future__ import annotations

import threading
import urllib.error
import urllib.request

import pytest

from memdrift import operations as ops
from memdrift import server as server_mod


@pytest.fixture
def live(repo):
    srv = server_mod.make_server(repo, port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        yield base, srv.token
    finally:
        srv.shutdown()
        srv.server_close()


def fetch(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8"), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8"), dict(exc.headers)


def test_binds_loopback_only(live):
    base, _ = live
    assert base.startswith("http://127.0.0.1:")


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "example.com", ""])
def test_refuses_public_bind(host):
    with pytest.raises(server_mod.LoopbackOnly):
        server_mod.check_host(host)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_allows_loopback_bind(host):
    assert server_mod.check_host(host) in {"127.0.0.1", "::1"}


def test_token_is_required(live):
    base, _ = live
    status, _, _ = fetch(f"{base}/")
    assert status == 403
    status, _, _ = fetch(f"{base}/?t=nope")
    assert status == 403


def test_overview_renders_changes(live):
    base, token = live
    status, body, headers = fetch(f"{base}/?t={token}")
    assert status == 200
    assert "clients/acme.md" in body
    assert headers.get("Content-Security-Policy", "").startswith("default-src 'none'")
    assert headers.get("X-Content-Type-Options") == "nosniff"


def test_no_absolute_paths_in_any_view(live, repo):
    base, token = live
    for path in ("/?t=", "/pages?t=", "/history?t=", "/page/clients/acme.md?t="):
        _, body, _ = fetch(f"{base}{path}{token}")
        assert str(repo.root) not in body, path


def test_page_detail_redacts_a_value_that_is_a_path(live, repo, vault):
    """A field value can be a pasted path; the digest redacts those."""
    target = vault / "clients" / "acme.md"
    target.write_text(
        target.read_text(encoding="utf-8").replace(
            "client: Acme", "client: Acme\nsource: 'C:\\Users\\alice\\notes.md'"
        ),
        encoding="utf-8",
    )
    ops.commit(repo, "add source")
    base, token = live
    status, body, _ = fetch(f"{base}/page/clients/acme.md?t={token}")
    assert status == 200
    assert "alice" not in body
    assert "~/notes.md" in body


def test_path_traversal_is_blocked(live):
    base, token = live
    status, _, _ = fetch(f"{base}/page/../../../../etc/passwd?t={token}")
    assert status in (403, 404)


def test_unknown_route_404s(live):
    base, token = live
    status, _, _ = fetch(f"{base}/nope?t={token}")
    assert status == 404


def test_private_page_hidden_from_pages_view(live, repo, vault):
    (vault / "secret").mkdir()
    (vault / "secret" / "d.md").write_text("---\nprivate: true\n---\n# D\n\nx\n", encoding="utf-8")
    ops.commit(repo, "add private")
    base, token = live
    _, body, _ = fetch(f"{base}/pages?t={token}")
    assert "secret/d.md" not in body


def test_private_page_cannot_be_opened_by_url(live, repo, vault):
    """Omitting a page from the list is not the same as refusing to serve it."""
    (vault / "secret").mkdir()
    (vault / "secret" / "d.md").write_text(
        "---\nprivate: true\ntitle: Diary\n---\n# D\n\n今日心情。\n", encoding="utf-8"
    )
    ops.commit(repo, "add private")
    base, token = live
    status, body, _ = fetch(f"{base}/page/secret/d.md?t={token}")
    assert status == 403
    assert "private" in body
    assert "今日心情" not in body


def test_quotes_can_be_disabled(live, repo, vault):
    (vault / "clients" / "acme.md").write_text((vault / "clients" / "acme.md").read_text(encoding="utf-8") + "\n", encoding="utf-8")
    ops.commit(repo, "change", evidence=["email:msg-1"])
    base, token = live
    status, body, _ = fetch(f"{base}/?t={token}&quotes=0")
    assert status == 200


def test_tokens_differ_between_servers(repo):
    a = server_mod.make_server(repo, port=0)
    b = server_mod.make_server(repo, port=0)
    try:
        assert a.token != b.token
        assert len(a.token) >= 20
    finally:
        a.server_close()
        b.server_close()


def test_url_carries_token(repo):
    srv = server_mod.make_server(repo, port=0)
    try:
        assert f"t={srv.token}" in server_mod.url_for(srv)
    finally:
        srv.server_close()


# --------------------------------------------------- DNS rebinding / CSRF


@pytest.mark.parametrize(
    "header,expected",
    [
        ("127.0.0.1:7787", True),
        ("localhost:7787", True),
        ("LocalHost:7787", True),
        ("[::1]:7787", True),
        ("127.0.0.1", True),
        # a rebinding attack keeps the attacker's own name in Host
        ("evil.test", False),
        ("evil.test:7787", False),
        ("attacker.example.com:7787", False),
        ("192.168.1.10:7787", False),
        # no Host at all: a script, not a browser
        (None, True),
        ("", True),
    ],
)
def test_host_header_allowed(header, expected):
    assert server_mod.host_header_allowed(header) is expected


@pytest.mark.parametrize(
    "origin,host,expected",
    [
        (None, "127.0.0.1:1", True),
        ("http://127.0.0.1:1", "127.0.0.1:1", True),
        ("http://evil.test", "127.0.0.1:1", False),
        ("https://evil.test:7787", "evil.test:7787", True),  # matches, caught by Host check
        ("null", "127.0.0.1:1", False),
        ("http://127.0.0.1:1", None, False),
    ],
)
def test_origin_matches_host(origin, host, expected):
    assert server_mod.origin_matches_host(origin, host) is expected


def test_rebinding_request_is_rejected_end_to_end(repo):
    """A valid token with a foreign Host must still be refused."""
    import http.client

    srv = server_mod.make_server(repo, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.putrequest("GET", f"/?t={srv.token}", skip_host=True)
        conn.putheader("Host", "evil.test")
        conn.endheaders()
        resp = conn.getresponse()
        assert resp.status == 403
        conn.close()
    finally:
        srv.shutdown()
        srv.server_close()


def test_cross_origin_request_is_rejected(repo):
    import http.client

    srv = server_mod.make_server(repo, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", f"/?t={srv.token}", headers={"Origin": "http://evil.test"})
        resp = conn.getresponse()
        assert resp.status == 403
        conn.close()
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_client_that_leaves_mid_response_is_not_a_traceback(repo, capsys):
    """Closing a tab must not print a traceback full of local paths."""
    srv = server_mod.make_server(repo, port=0)
    try:
        try:
            raise BrokenPipeError(32, "Broken pipe")
        except BrokenPipeError:
            srv.handle_error(None, ("127.0.0.1", 0))
        assert capsys.readouterr().err == ""

        try:
            raise RuntimeError(f"cannot open {repo.root}/notes.md")
        except RuntimeError:
            srv.handle_error(None, ("127.0.0.1", 0))
        err = capsys.readouterr().err
        assert err == "memdrift: RuntimeError while answering a request\n"

        srv.verbose = True
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            srv.handle_error(None, ("127.0.0.1", 0))
        assert "Traceback" in capsys.readouterr().err
    finally:
        srv.server_close()
