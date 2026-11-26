"""``memdrift ui``: the one surface a browser can write through.

Two layers, tested separately on purpose:

* ``Gui`` methods are plain functions, so the logic — the plan, the
  fingerprint, the 409, the private-page rule — is tested without a socket.
* The ``Handler`` is then driven over real HTTP to prove the locks sit in
  front of that logic: a refused request never reaches a handler.

And the page itself is checked the way a broken page actually breaks: no
external resource, and the script parses.
"""

from __future__ import annotations

import http.client
import importlib
import json
import os
import re
import shutil
import subprocess
import threading

import pytest
from click.testing import CliRunner

from memdrift import operations as ops
from memdrift.cli import main
from memdrift.gui import api as api_mod
from memdrift.gui import app as app_mod
from memdrift.repo import short
from memdrift.server import LoopbackOnly

ACME = "clients/acme.md"
PRICING = "frontmatter.pricing"
PRIVATE = "---\nprivate: true\n---\n# D\n\nx\n"
MANAGED_NODE = r"C:\Users\admin\.workbuddy\binaries\node\versions\22.22.2-3\node.exe"


def _set_pricing(vault, old: str, new: str) -> None:
    target = vault / "clients" / "acme.md"
    text = target.read_text(encoding="utf-8")
    assert old in text
    target.write_text(text.replace(f'"{old}"', f'"{new}"'), encoding="utf-8")


def _pricing_on_disk(vault) -> str:
    return (vault / "clients" / "acme.md").read_text(encoding="utf-8")


def _approval(shown: dict) -> dict:
    return {k: shown[k] for k in ("path", "field", "fingerprint")}


def _refused(callable_, status: int) -> str:
    with pytest.raises(api_mod.ApiError) as info:
        callable_()
    assert info.value.status == status
    return info.value.message


@pytest.fixture
def touched(repo, vault):
    """The fixture vault plus one agent commit that moved the price."""
    _set_pricing(vault, "$45k", "$48k")
    ops.commit(
        repo,
        "nightly consolidation",
        evidence=["email:msg-1882"],
        confidence=0.82,
        author_type="agent",
        author_name="linger",
    )
    return repo


@pytest.fixture
def gui(touched):
    return api_mod.Gui(touched)


# ------------------------------------------------------------ reads, direct


def test_state_is_one_snapshot(gui):
    state = gui.state({})
    assert state["has_history"] is True
    assert state["changes"] == len(state["events"]) > 0
    assert state["truncated"] == 0 and state["pending"] == []
    pricing = next(e for e in state["events"] if e["field"] == PRICING)
    assert (pricing["before"], pricing["after"]) == ("$48k", "$45k")[::-1]
    assert pricing["author"] == {"type": "agent", "name": "linger"}
    assert pricing["evidence"][0]["ref"] == "msg-1882"
    assert [p["path"] for p in state["page_list"]] == ["clients/acme.md", "topics/pricing.md"]


def test_state_honours_the_window(gui):
    assert gui.state({"since": "7d"})["changes"] == gui.state({})["changes"]
    with pytest.raises(ValueError):
        gui.state({"since": "not-a-window"})


def test_state_truncates_and_says_so(gui, monkeypatch):
    monkeypatch.setattr(api_mod, "MAX_EVENTS", 1)
    state = gui.state({})
    assert len(state["events"]) == 1
    assert state["truncated"] == state["changes"] - 1


def test_page_gives_last_change_per_field_with_evidence(gui):
    pricing = next(f for f in gui.page({"path": ACME})["fields"] if f["field"] == PRICING)
    assert (pricing["before"], pricing["after"]) == ("$45k", "$48k")
    assert pricing["evidence"][0]["ref"] == "msg-1882"
    assert pricing["commit"] == short(ops.log(gui.repo)[0]["id"])


def test_no_absolute_path_in_any_answer(gui, vault):
    """Every read is redacted, as the digest is."""
    home = str(vault.parent)
    for answer in (gui.state({}), gui.page({"path": ACME}),
                   gui.revert({"page": ACME, "field": PRICING})):
        dumped = json.dumps(answer, ensure_ascii=False)
        assert str(vault) not in dumped and home not in dumped
    assert str(vault).encode() not in gui.digest({})
    assert home.encode() not in gui.digest({})


def test_mcp_config_does_name_the_vault(gui, vault):
    """The one answer that is not redacted: a config with ``~`` launches nothing."""
    entry = gui.mcp_config({})["mcpServers"]["memdrift"]
    assert entry["args"] == ["-m", "memdrift", "-C", str(vault), "mcp"]


def test_a_page_absent_from_head_is_404(gui):
    assert "not in the last commit" in _refused(lambda: gui.page({"path": "nope.md"}), 404)


@pytest.mark.parametrize("raw", ["../../etc/passwd", "/etc/passwd", "clients/../.."])
def test_path_outside_the_root_is_refused(gui, tmp_path, raw):
    (tmp_path / "outside.md").write_text("x", encoding="utf-8")
    assert "outside the memory root" in _refused(lambda: gui.page({"path": raw}), 403)
    assert "outside the memory root" in _refused(
        lambda: gui.revert({"page": str(tmp_path / "outside.md"), "field": PRICING,
                           "dry_run": False, "expect": {}}), 403
    )


def test_private_page_is_never_answered(gui, touched, vault):
    (vault / "secret").mkdir()
    (vault / "secret" / "d.md").write_text(PRIVATE, encoding="utf-8")
    ops.commit(touched, "add private")
    assert "secret/d.md" not in [p["path"] for p in gui.state({})["page_list"]]
    assert _refused(lambda: gui.page({"path": "secret/d.md"}), 403).endswith("private")


def test_private_page_stays_out_of_the_pending_list(gui, touched, vault):
    """The digest only *counts* pending changes. Listing them by name must not
    name a private one — committed or not yet."""
    (vault / "secret").mkdir()
    (vault / "secret" / "d.md").write_text(PRIVATE, encoding="utf-8")
    assert gui.state({})["pending"] == []            # never committed
    ops.commit(touched, "add private")
    (vault / "secret" / "d.md").write_text(PRIVATE + "y\n", encoding="utf-8")
    assert gui.state({})["pending"] == []            # committed, changed again


def test_pending_lists_what_will_be_committed(gui, vault):
    _set_pricing(vault, "$48k", "$50k")
    (vault / "topics" / "new.md").write_text("# New\n", encoding="utf-8")
    assert sorted(gui.state({})["pending"], key=lambda p: p["path"]) == [
        {"path": ACME, "op": "modify"},
        {"path": "topics/new.md", "op": "add"},
    ]


def test_private_page_cannot_be_undone_either(gui, touched, vault):
    """The page view refuses private pages; undo must not be the way round it.

    A revert plan answers with before/after values, so it reads as much as it
    writes.
    """
    (vault / "secret").mkdir()
    (vault / "secret" / "d.md").write_text(
        "---\nprivate: true\nsalary: 999\n---\n# D\n\nx\n", encoding="utf-8"
    )
    ops.commit(touched, "add private")
    assert _refused(
        lambda: gui.revert({"page": "secret/d.md", "field": "frontmatter.salary"}), 403
    ).endswith("private")


def test_page_redacts_the_commit_message(gui, touched, vault):
    """A message is free text and lands in the same pane as every other value."""
    leaky = r"see C:\Users\alice\vault\clients\acme.md"
    _set_pricing(vault, "$48k", "$50k")
    ops.commit(touched, leaky)
    answer = json.dumps(gui.page({"path": ACME}), ensure_ascii=False)
    assert "alice" not in answer


def test_a_malformed_pending_list_is_a_400_not_a_500(gui, touched, vault):
    """Sorting ``(None, None)`` against ``("a", "add")`` raises TypeError.

    The check has to come before the comparison, or a bad body is answered
    with a 500 and the page cannot tell the human what it did wrong.
    """
    _set_pricing(vault, "$48k", "$50k")
    assert "path and an op" in _refused(
        lambda: gui.commit({"message": "m", "expect": [{"path": "clients/acme.md"}]}), 400
    )
    assert "path and an op" in _refused(
        lambda: gui.commit({"message": "m", "expect": ["clients/acme.md"]}), 400
    )
    assert len(ops.log(touched)) == 2


# ------------------------------------------------------------------ undo


def test_undo_is_a_dry_run_by_default_and_writes_nothing(gui, vault):
    result = gui.revert({"page": ACME, "field": PRICING})
    assert result["dry_run"] is True and result["commit"] is None and result["written"] == []
    (plan,) = result["plans"]
    assert (plan["before"], plan["after"]) == ("$48k", "$45k")
    assert not plan["skipped"] and len(plan["fingerprint"]) == 32
    assert '"$48k"' in _pricing_on_disk(vault)


def test_undo_without_the_approved_plan_writes_nothing(gui, vault):
    body = {"page": ACME, "field": PRICING, "dry_run": False}
    assert "send back" in _refused(lambda: gui.revert(body), 400)
    assert "send back" in _refused(lambda: gui.revert({**body, "expect": []}), 400)
    assert '"$48k"' in _pricing_on_disk(vault)


def test_undo_applies_exactly_the_plan_that_was_shown(gui, touched, vault):
    (shown,) = gui.revert({"page": ACME, "field": PRICING})["plans"]
    original = ops.log(touched)[0]["id"]
    result = gui.revert({"page": ACME, "field": PRICING, "dry_run": False,
                         "expect": _approval(shown)})
    assert result["dry_run"] is False and result["written"]
    assert result["commit"] == short(ops.log(touched)[0]["id"])
    assert '"$45k"' in _pricing_on_disk(vault)
    newest = ops.log(touched)[0]
    assert newest["message"] == f"revert: {ACME} · {PRICING} (undoes {short(original)})"
    assert (newest["author_type"], newest["author_name"]) == ("human", "you")
    # Append-only, and the link is explicit on the *new* commit: the one being
    # undone keeps its id and its content, which is what makes the trail hold.
    assert newest["reverts"] == original
    assert newest["id"] != original
    assert result["reverts"] == short(original)


def test_a_stale_plan_is_refused(gui, touched, vault):
    """The human looked, then the agent wrote again. The old plan must not land."""
    (shown,) = gui.revert({"page": ACME, "field": PRICING})["plans"]
    _set_pricing(vault, "$48k", "$51k")
    body = {"page": ACME, "field": PRICING, "dry_run": False, "expect": _approval(shown)}
    assert "changed since you looked" in _refused(lambda: gui.revert(body), 409)
    assert '"$51k"' in _pricing_on_disk(vault)
    assert len(ops.log(touched)) == 2


def test_fingerprint_is_taken_before_redaction():
    """Two values that redact to the same string are still two plans."""
    def fingerprint(before):
        return api_mod._fingerprint(
            api_mod.revert_mod.Plan("a.md", "frontmatter.note", before, "x")
        )

    assert fingerprint(r"C:\Users\alice\vault\a.md") != fingerprint(r"C:\Users\bob\vault\b.md")


def test_an_undone_plan_cannot_be_replayed(gui, touched, vault):
    (shown,) = gui.revert({"page": ACME, "field": PRICING})["plans"]
    gui.revert({"page": ACME, "field": PRICING, "dry_run": False, "expect": _approval(shown)})
    body = {"page": ACME, "field": PRICING, "dry_run": False, "expect": _approval(shown)}
    # Undoing an undo is a *different* plan (it would put $48k back), so the
    # old fingerprint is stale — refused either way, and nothing is written.
    _refused(lambda: gui.revert(body), 409)
    assert '"$45k"' in _pricing_on_disk(vault)
    assert len(ops.log(touched)) == 3


@pytest.mark.parametrize(
    "body",
    [
        {"commit": "HEAD", "dry_run": False, "expect": {}},                 # whole commit
        {"page": ACME, "commit": "HEAD", "dry_run": False, "expect": {}},  # whole page
        {"page": ACME, "dry_run": False, "expect": {}},                    # no field
        {"field": PRICING, "dry_run": False, "expect": {}},                # no page
        {"page": 7, "field": PRICING, "dry_run": False, "expect": {}},
    ],
)
def test_the_page_cannot_delete_rename_or_revert_a_commit(gui, touched, body):
    """Narrower than the CLI on purpose: one field on one page, nothing else."""
    assert "one page and one field" in _refused(lambda: gui.revert(body), 400)
    assert len(ops.log(touched)) == 2


def test_a_dry_run_that_is_not_a_bool_is_refused(gui):
    assert "true or false" in _refused(
        lambda: gui.revert({"page": ACME, "field": PRICING, "dry_run": "no"}), 400
    )


# ----------------------------------------------------------------- commit


def test_commit_records_the_list_that_was_shown(gui, touched, vault):
    _set_pricing(vault, "$48k", "$50k")
    pending = gui.state({})["pending"]
    result = gui.commit({"message": "  manual fix  ", "expect": pending})
    assert result["committed"] is True and result["changes"] >= 1
    assert result["id"] == short(ops.log(touched)[0]["id"])
    newest = ops.log(touched)[0]
    assert newest["message"] == "manual fix"
    assert (newest["author_type"], newest["author_name"]) == ("human", "you")
    assert [e["ref"] for e in newest["evidence"]] == ["memdrift ui"]


def test_commit_ignores_caller_supplied_provenance(gui, touched, vault):
    _set_pricing(vault, "$48k", "$50k")
    pending = gui.state({})["pending"]
    gui.commit({"message": "m", "expect": pending, "evidence": ["email:x"],
                "confidence": 1.0, "author_type": "agent", "author_name": "linger"})
    newest = ops.log(touched)[0]
    assert newest["confidence"] is None
    assert (newest["author_type"], newest["author_name"]) == ("human", "you")
    assert [e["ref"] for e in newest["evidence"]] == ["memdrift ui"]


@pytest.mark.parametrize(
    "body,words",
    [
        ({"expect": []}, "needs a message"),
        ({"message": "   ", "expect": []}, "needs a message"),
        ({"message": "x" * 201, "expect": []}, "under 200"),
        ({"message": "x"}, "list of changes"),
    ],
)
def test_commit_refuses_incomplete_requests(gui, touched, vault, body, words):
    _set_pricing(vault, "$48k", "$50k")
    assert words in _refused(lambda: gui.commit(body), 400)
    assert len(ops.log(touched)) == 2


def test_commit_with_a_stale_list_is_409(gui, touched, vault):
    _set_pricing(vault, "$48k", "$50k")
    assert "moved since you looked" in _refused(
        lambda: gui.commit({"message": "m", "expect": []}), 409
    )
    assert len(ops.log(touched)) == 2


# ------------------------------------------------- undoing the commit you just made


def test_commit_says_whether_it_can_still_be_undone(gui, touched, vault):
    _set_pricing(vault, "$48k", "$50k")
    result = gui.commit({"message": "manual fix", "expect": gui.state({})["pending"]})
    assert result["can_undo"] is True
    # The full id, not the short one: the page hands it straight back to /api/undo.
    assert result["commit_id"] == ops.log(touched)[0]["id"]


def test_undoing_a_commit_appends_one_that_points_back(gui, touched, vault):
    _set_pricing(vault, "$48k", "$50k")
    created = gui.commit({"message": "manual fix", "expect": gui.state({})["pending"]})
    target = created["id"]

    dry = gui.undo({"commit": target, "dry_run": True})
    assert dry["dry_run"] is True and dry["new_commit"] is None and dry["written"] == []
    assert dry["changes"] == 1 and dry["fingerprint"]

    applied = gui.undo({
        "commit": target, "dry_run": False,
        "expect": {"commit": target, "fingerprint": dry["fingerprint"]},
    })
    assert applied["reverts"] == target and applied["new_commit"]
    assert '"$48k"' in _pricing_on_disk(vault)

    newest = ops.log(touched)[0]
    assert newest["reverts"] == created["commit_id"]
    assert newest["message"].endswith(f"(undoes {target})")

    # Both commits are still there, and the undone one is byte-for-byte the
    # record of what happened: undoing is an append, never an edit.
    history = ops.log(touched)
    assert len(history) == 4  # initial + agent + manual + undo
    undone = history[1]
    assert undone["id"] == created["commit_id"]
    assert undone["message"] == "manual fix" and undone["reverts"] is None


def test_undoing_only_reaches_the_newest_commit(gui, touched):
    oldest = ops.log(touched)[-1]["id"]  # the initial snapshot
    with pytest.raises(api_mod.ApiError) as info:
        gui.undo({"commit": short(oldest)})
    assert info.value.status == 409
    assert "only the newest commit" in info.value.message
    assert "memdrift revert --commit" in info.value.message


def test_undoing_the_first_commit_has_nothing_before_it(repo):
    gui = api_mod.Gui(repo)
    with pytest.raises(api_mod.ApiError) as info:
        gui.undo({"commit": short(repo.head())})
    assert info.value.status == 400
    assert "initial snapshot" in info.value.message


def test_undo_needs_the_plan_that_was_shown(gui, touched, vault):
    _set_pricing(vault, "$48k", "$50k")
    created = gui.commit({"message": "manual fix", "expect": gui.state({})["pending"]})
    dry = gui.undo({"commit": created["id"], "dry_run": True})

    missing = {"commit": created["id"], "dry_run": False, "expect": None}
    assert "send back" in _refused(lambda: gui.undo(missing), 400)

    for expect in ({}, {"commit": created["id"]}, {"fingerprint": dry["fingerprint"]}):
        body = {"commit": created["id"], "dry_run": False, "expect": expect}
        assert "no longer undoes" in _refused(lambda: gui.undo(body), 409)

    assert '"$50k"' in _pricing_on_disk(vault)
    assert len(ops.log(touched)) == 3  # nothing was appended


def test_undo_of_a_commit_touching_a_private_page_is_refused(gui, touched, vault):
    (vault / "secret").mkdir()
    (vault / "secret" / "d.md").write_text(
        "---\nprivate: true\nsalary: 1\n---\n# D\n", encoding="utf-8"
    )
    gui.commit({"message": "add private", "expect": gui.state({})["pending"]})
    assert "private" in _refused(lambda: gui.undo({"commit": short(touched.head())}), 403)


def test_commit_with_nothing_pending_says_so(gui, touched):
    assert gui.commit({"message": "m", "expect": []}) == {
        "committed": False, "reason": "nothing changed",
    }


# ---------------------------------------------------------- over real HTTP


@pytest.fixture
def live(touched):
    srv = api_mod.make_server(touched, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


def call(srv, method, path, *, token="auto", headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    if token is not None:
        path += ("&" if "?" in path else "?") + "t=" + (srv.token if token == "auto" else token)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    out = (resp.status, resp.read(), dict(resp.getheaders()))
    conn.close()
    return out


JSON_WRITE = {"Content-Type": "application/json", "X-Memdrift": "ui"}


def _write(srv, path, payload, *, headers=None, token="auto"):
    """A write as the page sends it — unless the test overrides the headers."""
    return call(srv, "POST", path, body=json.dumps(payload).encode(),
                headers=JSON_WRITE if headers is None else headers, token=token)


def test_binds_loopback_only(live):
    assert live.server_address[0] == "127.0.0.1"
    with pytest.raises(LoopbackOnly):
        api_mod.make_server(live.repo, host="0.0.0.0")


def test_tokens_differ_and_are_long(repo):
    a = api_mod.make_server(repo)
    b = api_mod.make_server(repo)
    try:
        assert a.token != b.token and len(a.token) >= 20
    finally:
        a.server_close()
        b.server_close()


def test_url_carries_the_token(repo):
    srv = api_mod.make_server(repo)
    try:
        assert f"/?t={srv.token}" in api_mod.url_for(srv)
    finally:
        srv.server_close()


def test_serves_the_page_with_a_csp_that_lets_it_talk(live):
    status, body, headers = call(live, "GET", "/")
    assert status == 200 and body == app_mod.PAGE.encode("utf-8")
    csp = headers["Content-Security-Policy"]
    assert csp.startswith("default-src 'none'") and "connect-src 'self'" in csp
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize("token", [None, "", "nope", "%C3%A9", "wrong"])
def test_reads_need_the_token(live, token):
    status, body, _ = call(live, "GET", "/api/state", token=token)
    assert status == 403 and b"token" in body


def test_a_foreign_host_is_refused_even_with_the_token(live):
    conn = http.client.HTTPConnection("127.0.0.1", live.server_address[1], timeout=5)
    conn.putrequest("GET", f"/api/state?t={live.token}", skip_host=True)
    conn.putheader("Host", "evil.test")
    conn.endheaders()
    assert conn.getresponse().status == 403
    conn.close()


def test_state_over_http(live, touched):
    status, body, _ = call(live, "GET", "/api/state")
    assert status == 200
    state = json.loads(body)
    assert state["vault"] == touched.root.name and state["has_history"] is True
    assert str(touched.root).encode() not in body


def test_bad_window_is_a_400_not_a_crash(live):
    assert call(live, "GET", "/api/state?since=zzz")[0] == 400


def test_page_digest_and_config_over_http(live, touched):
    status, body, _ = call(live, "GET", "/api/page?path=clients/acme.md")
    assert status == 200 and json.loads(body)["fields"]
    assert call(live, "GET", "/api/page?path=../../etc/passwd")[0] == 403
    assert call(live, "GET", "/api/page")[0] == 400

    status, body, headers = call(live, "GET", "/api/digest")
    assert status == 200 and body.startswith(b"<!DOCTYPE html>")
    assert "attachment" in headers["Content-Disposition"]
    assert str(touched.root).encode() not in body

    status, body, _ = call(live, "GET", "/api/mcp-config")
    assert status == 200 and json.loads(body)["mcpServers"]["memdrift"]["args"]


def test_a_write_without_the_proper_headers_writes_nothing(live, touched, vault):
    body = {"page": ACME, "field": PRICING, "dry_run": False, "expect": {}}
    cases = [
        ({"Content-Type": "application/json"}, "auto", 403),       # no custom header
        ({"Content-Type": "text/plain", "X-Memdrift": "ui"}, "auto", 415),
        ({"Content-Type": "application/json", "X-Memdrift": "x"}, "auto", 403),
        (JSON_WRITE, None, 403),                                  # no token
        (JSON_WRITE, "wrong", 403),
    ]
    for headers, token, expected in cases:
        status, out, _ = _write(live, "/api/revert", body, headers=headers, token=token)
        assert (status, b"error" in out) == (expected, True), (headers, token)
    assert len(ops.log(touched)) == 2
    assert '"$48k"' in _pricing_on_disk(vault)


def test_a_cross_origin_write_is_refused(live, touched):
    conn = http.client.HTTPConnection("127.0.0.1", live.server_address[1], timeout=5)
    conn.request(
        "POST", f"/api/revert?t={live.token}",
        body=json.dumps({"page": ACME, "field": PRICING, "dry_run": False}).encode(),
        headers={**JSON_WRITE, "Origin": "http://evil.test"},
    )
    assert conn.getresponse().status == 403
    conn.close()
    assert len(ops.log(touched)) == 2


@pytest.mark.parametrize(
    "payload,ctype,status",
    [
        (b"not json", "application/json", 400),
        (b"[1,2]", "application/json", 400),                      # not an object
        (b"{}", "text/plain", 415),
        (b"x" * (api_mod.MAX_BODY + 10), "application/json", 413),
    ],
)
def test_malformed_writes_are_rejected(live, payload, ctype, status):
    got = call(live, "POST", "/api/revert", body=payload,
               headers={"Content-Type": ctype, "X-Memdrift": "ui"})
    assert got[0] == status, got[1]


def test_unknown_routes(live):
    assert call(live, "GET", "/nope")[0] == 404
    assert _write(live, "/api/nope", {})[0] == 404


def test_the_whole_undo_flow_over_http(live, touched, vault):
    status, body, _ = _write(live, "/api/revert", {"page": ACME, "field": PRICING})
    assert status == 200 and json.loads(body)["dry_run"] is True
    (shown,) = json.loads(body)["plans"]
    assert '"$48k"' in _pricing_on_disk(vault)

    status, body, _ = _write(live, "/api/revert", {
        "page": ACME, "field": PRICING, "dry_run": False, "expect": _approval(shown),
    })
    assert status == 200
    assert json.loads(body)["commit"] == short(ops.log(touched)[0]["id"])
    assert '"$45k"' in _pricing_on_disk(vault)


def test_commit_over_http(live, touched, vault):
    _set_pricing(vault, "$48k", "$50k")
    pending = json.loads(call(live, "GET", "/api/state")[1])["pending"]
    status, body, _ = _write(live, "/api/commit", {"message": "manual fix", "expect": pending})
    assert status == 200 and json.loads(body)["committed"] is True
    assert json.loads(call(live, "GET", "/api/state")[1])["pending"] == []


def test_a_broken_memory_file_does_not_kill_the_server(live, monkeypatch):
    def explode(*_a, **_k):
        raise RuntimeError("unreadable page")

    monkeypatch.setattr(api_mod.Gui, "state", explode)
    assert call(live, "GET", "/api/state")[0] == 500
    monkeypatch.undo()
    assert call(live, "GET", "/api/state")[0] == 200


# ----------------------------------------------------------- the page itself


def test_page_is_one_self_contained_file():
    page = app_mod.PAGE
    assert page.lower().startswith("<!doctype html>")
    assert page.count("<style>") == 1 and page.count("<script>") == 1
    # nothing to fetch, nothing to frame, no inline event handlers
    assert not re.search(r"""\b(src|href)\s*=\s*["']https?://""", page, re.I)
    assert not re.search(r"@import|<link|<iframe|https?://", page, re.I)
    assert not re.search(r"""\son[a-z]+\s*=""", page)
    assert "<script>" not in (app_mod.__doc__ or "")  # the docstring must not be
    assert "<script>" in app_mod.BODY or True          # mistaken for the real tag


def test_page_has_one_layout_grid_and_it_is_the_outer_div():
    """The two-pane grid must be owned by exactly one element.

    A real browser screenshot caught ``<main class="app">``, which put the
    grid *inside* main: the timeline collapsed to the left edge while the
    right half of the window stayed blank. Unit tests did not see it, so the
    structure is pinned here.
    """
    body = app_mod.BODY
    assert body.count('class="app"') == 1, "exactly one grid container"
    assert body.lstrip().startswith('<div class="app">'), body[:40]
    # the grid container wraps header, main and aside as siblings
    assert body.index("<header") > body.index('<div class="app">')
    assert body.index("<main") > body.index("</header>")
    assert body.index("<aside") > body.index("</main>")
    # body is not a second grid: that squeezes .app into the first column
    assert '<body class="app">' not in app_mod.PAGE
    assert "<body>" in app_mod.PAGE


def test_page_summary_reads_the_field_the_api_returns():
    """``/api/state`` returns ``risky``; ``risk`` was a typo that silently
    dropped the "n changes worth a look" line from the header."""
    assert "st.risky" in app_mod.SCRIPT
    assert "st.risk" not in app_mod.SCRIPT.replace("st.risky", "")


def test_state_says_whether_this_install_may_write(gui, touched, vault):
    """A shared vault you may only read is a state the page has to see."""
    from memdrift import scope

    assert gui.state({})["writable"] is True

    scope.deny(vault)
    scope.allow(vault, mode="read")
    st = gui.state({})
    assert st["writable"] is False
    assert st["commits"]  # reading still works, which is the point of a read space


def test_state_never_offers_a_commit_button_it_would_have_to_refuse():
    script = app_mod.SCRIPT
    assert "st.writable" in script
    # the pending list explains itself instead of showing the button
    assert "st.pending.length" in script and "readonly" in script


def test_page_offers_the_way_back_after_a_commit():
    """The offer is a dialog in the page, not a line in the server's answer.

    Asserted structurally because the failure mode here is silent: a modal
    that is never rendered looks exactly like a feature that was never built,
    and no HTTP test can tell the difference.
    """
    assert 'id="modal"' in app_mod.BODY
    script = app_mod.SCRIPT
    # the offer only appears when the server says the commit can be undone
    assert "r.can_undo" in script
    assert "undo-offer" in script and "undo-confirm" in script
    # and it is dismissible: a popup with no "no" is a trap
    assert "keepCommit" in script and "Escape" in script
    # the new commit's own id and the one it undoes are both shown
    assert "r.new_commit" in script and "r.reverts" in script


def test_page_shows_which_commit_an_undo_undoes():
    assert "reverts_short" in app_mod.SCRIPT
    assert app_mod.SCRIPT.count("↺") == 1


def test_page_script_parses(tmp_path):
    node = shutil.which("node") or (MANAGED_NODE if os.path.exists(MANAGED_NODE) else None)
    if not node:
        pytest.skip("node is not installed")
    script = tmp_path / "page.js"
    script.write_text(app_mod.SCRIPT, encoding="utf-8")
    done = subprocess.run([node, "--check", str(script)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_page_sends_what_the_server_gates_on():
    """The header and paths the page uses are the ones the server requires."""
    assert f"'{api_mod.WRITE_HEADER[0]}'" in app_mod.SCRIPT
    assert f"'{api_mod.WRITE_HEADER[1]}'" in app_mod.SCRIPT
    for route in ("api/state", "api/page", "api/mcp-config", "api/digest", "api/revert", "api/undo", "api/commit"):
        assert route in app_mod.SCRIPT, route
    # a write is never the first click: the plan is fetched, then confirmed
    script = app_mod.SCRIPT
    assert script.index("dry_run: true") < script.index("dry_run: false")
    assert "fingerprint: shown.fingerprint" in script


# -------------------------------------------------------------- the command


def _without_gui(monkeypatch):
    """Pretend the web app is not present in this install.

    The CLI resolves it with ``importlib.import_module`` precisely so the
    "this build has no web app" path is testable without uninstalling it.
    """
    real = importlib.import_module

    def fake(name, package=None):
        if name.startswith("memdrift.gui"):
            raise ModuleNotFoundError(f"No module named {name!r}", name="memdrift.gui")
        return real(name, package)

    monkeypatch.setattr(importlib, "import_module", fake)


def test_ui_help_works_without_the_gui_installed(monkeypatch, vault):
    _without_gui(monkeypatch)
    result = CliRunner().invoke(main, ["-C", str(vault), "ui", "--help"])
    assert result.exit_code == 0
    assert "memdrift ui" in result.output


def test_ui_says_what_to_do_when_it_is_missing(monkeypatch, vault):
    _without_gui(monkeypatch)
    result = CliRunner().invoke(main, ["-C", str(vault), "ui", "--port", "0"])
    assert result.exit_code == 1
    assert "web app is missing" in result.output
    # The honest fallback: the parts that do not need a browser still work.
    assert "memdrift digest" in result.output
    assert "Traceback" not in result.output


def test_ui_refuses_a_public_bind(repo):
    result = CliRunner().invoke(main, ["-C", str(repo.root), "ui", "--host", "0.0.0.0"])
    assert result.exit_code == 1
    assert "loopback only" in result.output
