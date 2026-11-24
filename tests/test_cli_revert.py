"""The CLI surface of `revert` — the part a human actually types.

`test_revert.py` covers the engine; this file covers the promise a user sees:
the bad value comes back to where it was, the bad commit stays in history, and
`--dry-run` really does write nothing.
"""

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


def _log(vault):
    return json.loads(run("-C", str(vault), "log", "--json").output)


def test_revert_field_restores_the_value(vault):
    """The PRD's unhappy path: a bad extraction, then undo it."""
    run("init", str(vault))
    acme = vault / "clients" / "acme.md"
    original = acme.read_text(encoding="utf-8")
    acme.write_text(original.replace('"$45k"', '"$12k"'), encoding="utf-8")
    run("-C", str(vault), "commit", "-m", "extract: pricing")

    out = run(
        "-C", str(vault), "revert",
        "--field", "frontmatter.pricing",
        "--page", "clients/acme.md",
    ).output
    assert "reverted 1 item(s)" in out
    assert acme.read_text(encoding="utf-8") == original


def test_revert_is_append_only(vault):
    run("init", str(vault))
    acme = vault / "clients" / "acme.md"
    acme.write_text(acme.read_text(encoding="utf-8").replace('"$45k"', '"$12k"'), encoding="utf-8")
    run("-C", str(vault), "commit", "-m", "extract: pricing")
    bad_id = _log(vault)[0]["id"]

    run(
        "-C", str(vault), "revert",
        "--field", "frontmatter.pricing",
        "--page", "clients/acme.md",
    )

    log = _log(vault)
    assert log[0]["message"].startswith("revert:")
    assert log[0]["author_type"] == "human"
    # the bad commit is still there, untouched, still saying what it said
    assert log[1]["id"] == bad_id
    assert log[1]["message"] == "extract: pricing"
    shown = json.loads(run("-C", str(vault), "show", bad_id, "--json").output)
    pricing = next(c for c in shown["changes"] if c.get("field") == "frontmatter.pricing")
    assert pricing["after"] == "$12k"


def test_revert_dry_run_writes_nothing(vault):
    run("init", str(vault))
    acme = vault / "clients" / "acme.md"
    acme.write_text(acme.read_text(encoding="utf-8").replace('"$45k"', '"$12k"'), encoding="utf-8")
    run("-C", str(vault), "commit", "-m", "bad")
    before = acme.read_text(encoding="utf-8")
    head_before = _log(vault)[0]["id"]

    out = run(
        "-C", str(vault), "revert",
        "--field", "frontmatter.pricing",
        "--page", "clients/acme.md",
        "--dry-run",
    ).output
    assert "dry run" in out
    assert "-->" not in out  # sanity: no accidental flag echo
    assert "45k" in out
    assert acme.read_text(encoding="utf-8") == before
    assert _log(vault)[0]["id"] == head_before


def test_revert_typo_explains_itself_and_exits_zero(vault):
    """A typo is not a crash. Say what fields exist; touch nothing."""
    run("init", str(vault))
    out = run(
        "-C", str(vault), "revert",
        "--field", "frontmatter.nope",
        "--page", "clients/acme.md",
        "--dry-run",
    ).output
    assert "skip" in out
    assert "frontmatter.pricing" in out


def test_revert_unknown_commit_is_an_error(vault):
    run("init", str(vault))
    result = CliRunner().invoke(main, ["-C", str(vault), "revert", "--commit", "deadbeef"])
    assert result.exit_code != 0
    assert "unknown commit" in result.output


def test_revert_without_a_target_is_an_error(vault):
    run("init", str(vault))
    result = CliRunner().invoke(main, ["-C", str(vault), "revert"])
    assert result.exit_code != 0
    assert "say what to revert" in result.output


def test_revert_json_shape(vault):
    run("init", str(vault))
    acme = vault / "clients" / "acme.md"
    acme.write_text(acme.read_text(encoding="utf-8").replace('"$45k"', '"$12k"'), encoding="utf-8")
    run("-C", str(vault), "commit", "-m", "bad")

    payload = json.loads(
        run(
            "-C", str(vault), "revert",
            "--field", "frontmatter.pricing",
            "--page", "clients/acme.md",
            "--json",
        ).output
    )
    assert payload["dry_run"] is False
    assert payload["commit"]
    item = payload["plans"][0]
    assert item["field"] == "frontmatter.pricing"
    assert item["before"] == "$12k"
    assert item["after"] == "$45k"
