"""The hard boundary: which directories this install may touch.

These tests are about refusals, which is unusual on purpose. A permission
check that is only ever exercised on the happy path is indistinguishable from
no check at all, so every rule below is asserted from the refusing side — and
each refusal has to name the command that would grant access, because a "no"
that does not say how to proceed gets worked around by editing the source.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from memdrift import operations as ops
from memdrift import scope
from memdrift.cli import main
from memdrift.repo import Repo


def _home(tmp_path, name="home"):
    path = tmp_path / name
    path.mkdir(exist_ok=True)
    return path


# ------------------------------------------------------------ the default rule


def test_a_fresh_install_reads_only_the_directory_it_was_started_in(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMDRIFT_HOME", str(_home(tmp_path)))
    work = tmp_path / "work"
    work.mkdir()

    scope.check(work, cwd=work)  # itself: fine
    scope.check(work / "clients", cwd=work)  # inside it: fine

    with pytest.raises(scope.OutOfScope):
        scope.check(tmp_path, cwd=work)  # its parent is not "the working directory"
    with pytest.raises(scope.OutOfScope):
        scope.check(tmp_path / "elsewhere", cwd=work)


def test_a_fresh_install_is_read_only(tmp_path, monkeypatch):
    """The whole point of "read the working directory": reading is all it is."""
    monkeypatch.setenv("MEMDRIFT_HOME", str(_home(tmp_path)))
    work = tmp_path / "work"
    work.mkdir()

    with pytest.raises(scope.OutOfScope) as info:
        scope.check(work, write=True, cwd=work)
    message = str(info.value)
    assert "read-only" in message
    assert "memdrift space allow" in message and "--write" in message


def test_the_refusal_names_the_directory_and_the_command(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMDRIFT_HOME", str(_home(tmp_path)))
    work = tmp_path / "work"
    work.mkdir()
    target = tmp_path / "shared" / "team-memory"
    target.mkdir(parents=True)

    with pytest.raises(scope.OutOfScope) as info:
        scope.check(target, write=True, cwd=work)
    message = str(info.value)
    assert str(target.resolve()) in message
    assert f"memdrift space allow {target.resolve()} --write" in message


# ------------------------------------------------------------- the registry


def test_allowing_a_directory_grants_read_and_then_write(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMDRIFT_HOME", str(_home(tmp_path)))
    work = tmp_path / "work"
    work.mkdir()
    shared = tmp_path / "shared"
    shared.mkdir()

    scope.allow(shared, mode="read")
    scope.check(shared, write=False, cwd=work)
    with pytest.raises(scope.OutOfScope):
        scope.check(shared, write=True, cwd=work)

    scope.allow(shared, mode="write")  # upgrade in place, one entry
    scope.check(shared, write=True, cwd=work)
    assert [s.path for s in scope.load()] == [str(shared.resolve())]
    assert [s.mode for s in scope.load()] == ["write"]


def test_deny_takes_the_read_away_too(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMDRIFT_HOME", str(_home(tmp_path)))
    work = tmp_path / "work"
    work.mkdir()
    shared = tmp_path / "shared"
    shared.mkdir()
    scope.allow(shared, mode="write")

    assert scope.deny(shared) is True
    with pytest.raises(scope.OutOfScope):
        scope.check(shared, write=False, cwd=work)
    assert scope.deny(shared) is False  # already gone


def test_a_filesystem_root_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMDRIFT_HOME", str(_home(tmp_path)))
    with pytest.raises(ValueError):
        scope.allow(Path(Path.home().anchor or "/"), mode="write")
    assert scope.load() == []


def test_an_entry_we_cannot_parse_fails_closed(tmp_path, monkeypatch):
    home = _home(tmp_path)
    monkeypatch.setenv("MEMDRIFT_HOME", str(home))
    work = tmp_path / "work"
    work.mkdir()
    (home / scope.REGISTRY_NAME).write_text(
        json.dumps(
            {
                "version": 1,
                "spaces": [
                    {"path": str(work), "mode": "everything"},  # not a mode
                    {"path": 42, "mode": "write"},  # not a path
                    {"mode": "write"},  # no path at all
                    {"path": "", "mode": "write"},  # empty
                ],
            }
        ),
        encoding="utf-8",
    )
    # Nothing usable in the file, and the file exists, so the implicit cwd
    # fallback does not apply either: the answer is "no", not "probably yes".
    with pytest.raises(scope.OutOfScope):
        scope.check(work, cwd=work)


def test_a_registry_shipped_inside_a_vault_does_nothing(tmp_path, monkeypatch):
    """A vault you clone must not be able to widen its own scope.

    The registry lives in the operator's home. A file at
    ``<vault>/.memdrift/spaces.json`` is just a file in the vault — it is
    read as memory content, never as configuration.
    """
    monkeypatch.setenv("MEMDRIFT_HOME", str(_home(tmp_path)))
    vault = tmp_path / "vault"
    (vault / ".memdrift").mkdir(parents=True)
    (vault / ".memdrift" / scope.REGISTRY_NAME).write_text(
        json.dumps({"version": 1, "spaces": [{"path": str(vault), "mode": "write"}]}),
        encoding="utf-8",
    )
    assert scope.registry_path() != vault / ".memdrift" / scope.REGISTRY_NAME
    assert scope.load() == []
    with pytest.raises(scope.OutOfScope):
        scope.check(vault, write=True, cwd=tmp_path / "work")


# -------------------------------------------------------------- entry points


def test_init_declares_the_space_with_write(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMDRIFT_HOME", str(_home(tmp_path)))
    vault = tmp_path / "vault"
    vault.mkdir()

    result = CliRunner().invoke(main, ["init", str(vault)])
    assert result.exit_code == 0, result.output
    assert "space:" in result.output
    assert scope.writable(vault)


def test_init_does_not_claim_a_grant_it_could_not_record(tmp_path, monkeypatch):
    """`init` must not print "(write)" when the registry write failed.

    A locked-down HOME (or a registry path that is not a directory) leaves the
    vault usable but unregistered; the next `commit` is refused. Two messages
    contradicting each other is worse than either one alone, so the CLI checks
    the result of the declaration instead of assuming it.
    """
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("MEMDRIFT_HOME", str(blocker))
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "acme.md").write_text("---\nstatus: active\n---\n# Acme\n", encoding="utf-8")

    result = CliRunner().invoke(main, ["init", str(vault)])
    assert result.exit_code == 0, result.output
    assert "not recorded" in result.output
    assert "this install may now read and write here" not in result.output
    # And the truth is enforced, not just printed: the vault exists, the grant
    # does not, so the next write is refused at the entry point.
    assert not scope.writable(vault)
    refused = CliRunner().invoke(main, ["-C", str(vault), "commit", "-m", "later"])
    assert refused.exit_code == 1
    assert "refused:" in refused.output


def test_cli_refuses_a_vault_outside_every_space(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMDRIFT_HOME", str(_home(tmp_path)))
    outside = tmp_path / "someone-elses"
    outside.mkdir()
    Repo.init(outside)  # registered below, then removed: the vault exists, the grant does not
    scope.deny(outside)

    result = CliRunner().invoke(main, ["-C", str(outside), "log"])
    assert result.exit_code == 1
    assert "outside your allowed spaces" in result.output


def test_cli_commit_refused_on_a_read_only_space(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMDRIFT_HOME", str(_home(tmp_path)))
    vault = tmp_path / "shared"
    vault.mkdir()
    repo = Repo.init(vault)
    scope.deny(vault)
    scope.allow(vault, mode="read")
    (vault / "note.md").write_text("# note\n", encoding="utf-8")
    before = repo.head()

    read = CliRunner().invoke(main, ["-C", str(vault), "status"])
    assert read.exit_code == 0, read.output

    write = CliRunner().invoke(main, ["-C", str(vault), "commit", "-m", "should not land"])
    assert write.exit_code == 1
    assert "read-only" in write.output
    assert repo.head() == before  # nothing was appended behind the refusal


def test_mcp_write_tool_refused_without_a_write_space(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMDRIFT_HOME", str(_home(tmp_path)))
    from memdrift.mcp import Toolbox
    from memdrift.rpc import FORBIDDEN, RpcError

    vault = tmp_path / "shared"
    vault.mkdir()
    repo = Repo.init(vault)  # registers write
    (vault / "a.md").write_text("# a\n", encoding="utf-8")
    ops.commit(repo, "one")
    scope.deny(vault)
    scope.allow(vault, mode="read")

    box = Toolbox(repo)
    assert box.history({})["commits"]  # reads still work

    with pytest.raises(RpcError) as info:
        box.commit({"message": "from an agent"})
    assert info.value.code == FORBIDDEN
    assert "read-only" in info.value.message


def test_gui_refuses_to_write_without_the_space(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMDRIFT_HOME", str(_home(tmp_path)))
    from memdrift.gui import api as api_mod

    vault = tmp_path / "shared"
    vault.mkdir()
    repo = Repo.init(vault)
    (vault / "a.md").write_text("# a\n", encoding="utf-8")
    scope.deny(vault)
    scope.allow(vault, mode="read")

    gui = api_mod.Gui(repo)
    with pytest.raises(api_mod.ApiError) as info:
        gui.commit({"message": "nope", "expect": []})
    assert info.value.status == 403
    assert "read-only" in info.value.message


def test_space_list_explains_the_implicit_default(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMDRIFT_HOME", str(_home(tmp_path)))
    result = CliRunner().invoke(main, ["space", "list"])
    assert result.exit_code == 0, result.output
    assert "no spaces declared yet" in result.output
    assert "space allow" in result.output


def test_space_allow_and_deny_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMDRIFT_HOME", str(_home(tmp_path)))
    shared = tmp_path / "shared"
    shared.mkdir()

    added = CliRunner().invoke(main, ["space", "allow", str(shared), "--write"])
    assert added.exit_code == 0, added.output
    listed = json.loads(CliRunner().invoke(main, ["space", "list", "--json"]).output)
    assert [s["path"] for s in listed["spaces"]] == [str(shared.resolve())]
    assert listed["spaces"][0]["mode"] == "write"

    removed = CliRunner().invoke(main, ["space", "deny", str(shared)])
    assert removed.exit_code == 0, removed.output
    assert scope.load() == []
