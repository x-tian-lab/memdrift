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
