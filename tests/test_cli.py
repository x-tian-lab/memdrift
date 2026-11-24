from __future__ import annotations

import json

from click.testing import CliRunner

from memdrift.cli import main


def run(*args, expect=0):
    result = CliRunner().invoke(main, list(args))
    if result.exit_code != expect:
        raise AssertionError(
            f"exit {result.exit_code} (expected {expect})\n{result.output}\n{result.exception!r}"
        )
    return result


def test_init_then_status(vault):
    out = run("init", str(vault)).output
    assert "initialized" in out
    assert "baseline" in out
    assert "clean" in run("-C", str(vault), "status").output


def test_init_is_idempotent(vault):
    run("init", str(vault))
    assert "already initialized" in run("init", str(vault)).output


def test_commit_log_diff_blame_roundtrip(vault):
    run("init", str(vault))
    acme = vault / "clients" / "acme.md"
    acme.write_text(acme.read_text().replace('"$45k"', '"$48k"'), encoding="utf-8")

    assert "clients/acme.md" in run("-C", str(vault), "status").output
    out = run(
        "-C", str(vault), "commit",
        "-m", "quote updated",
        "--evidence", "email:msg-1882",
        "--confidence", "0.82",
    ).output
    assert "quote updated" in out
    assert "48k" in out

    log = json.loads(run("-C", str(vault), "log", "--json").output)
    assert log[0]["message"] == "quote updated"
    assert log[0]["evidence"][0]["ref"] == "msg-1882"

    diff = json.loads(run("-C", str(vault), "show", log[0]["id"], "--json").output)
    pricing = next(c for c in diff["changes"] if c.get("field") == "frontmatter.pricing")
    assert pricing["before"] == "$45k"
    assert pricing["after"] == "$48k"

    blame = json.loads(run("-C", str(vault), "blame", "clients/acme.md", "--json").output)
    fields = {f["field"]: f for f in blame["fields"]}
    assert fields["frontmatter.pricing"]["after"] == "$48k"
    assert fields["frontmatter.pricing"]["evidence"][0]["kind"] == "email"


def test_blame_accepts_a_basename(vault):
    run("init", str(vault))
    blame = json.loads(run("-C", str(vault), "blame", "acme.md", "--json").output)
    assert blame["path"] == "clients/acme.md"


def test_blame_unknown_page_fails_cleanly(vault):
    run("init", str(vault))
    result = run("-C", str(vault), "blame", "nope.md", expect=1)
    assert "no such page" in result.output


def test_log_since_rejects_garbage(vault):
    run("init", str(vault))
    result = run("-C", str(vault), "log", "--since", "yesterday-ish", expect=1)
    assert "bad --since" in result.output


def test_diff_without_commits_shows_everything_new(vault):
    from memdrift.repo import Repo

    Repo.init(vault)
    out = json.loads(run("-C", str(vault), "diff", "--json").output)
    assert {c["path"] for c in out} == {"clients/acme.md", "topics/pricing.md"}
    assert all(c["op"] == "add" for c in out)


def test_diff_shows_uncommitted_edits_like_git(vault):
    run("init", str(vault))
    acme = vault / "clients" / "acme.md"
    acme.write_text(acme.read_text().replace("active", "churned"), encoding="utf-8")

    changes = json.loads(run("-C", str(vault), "diff", "--json").output)
    assert any(c.get("field") == "frontmatter.status" for c in changes)

    # clean tree after committing
    run("-C", str(vault), "commit", "-m", "status changed")
    assert json.loads(run("-C", str(vault), "diff", "--json").output) == []


def test_show_prints_evidence_and_changes(vault):
    run("init", str(vault))
    acme = vault / "clients" / "acme.md"
    acme.write_text(acme.read_text().replace('"$45k"', '"$48k"'), encoding="utf-8")
    run("-C", str(vault), "commit", "-m", "quote", "--evidence", "email:msg-1",
        "--confidence", "0.9")

    out = run("-C", str(vault), "show").output
    assert "quote" in out
    assert "email:msg-1" in out
    assert "conf 0.90" in out
    assert "$45k → $48k" in out


def test_not_a_repo_message(tmp_path):
    result = run("-C", str(tmp_path), "status", expect=1)
    assert "not a memdrift repository" in result.output


def test_nothing_to_commit_message(vault):
    run("init", str(vault))
    assert "nothing to commit" in run("-C", str(vault), "commit", "-m", "noop").output


def test_watch_once(vault):
    run("init", str(vault))
    acme = vault / "clients" / "acme.md"
    acme.write_text(acme.read_text() + "\n\n## New\n\nadded later\n", encoding="utf-8")
    out = run("-C", str(vault), "watch", "--once").output
    assert "committed" in out
