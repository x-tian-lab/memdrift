from __future__ import annotations

import textwrap

import pytest

from memdrift import operations as ops
from memdrift import scope
from memdrift.repo import Repo


@pytest.fixture(autouse=True)
def isolated_scope(tmp_path, monkeypatch):
    """Every test gets its own space registry.

    Without this, a test run would read (and write) the developer's real
    ``~/.memdrift/spaces.json``. With it, "which directories may this install
    touch" is a per-test fact rather than an ambient one — and a test that
    wants to *prove* a refusal can set ``MEMDRIFT_HOME`` itself and override.
    """
    monkeypatch.setenv("MEMDRIFT_HOME", str(tmp_path / "_scope_home"))
    return tmp_path / "_scope_home"


ACME_V1 = textwrap.dedent(
    """\
    ---
    client: Acme
    pricing: "$45k"
    status: active
    tags: [enterprise, q4]
    contacts:
      owner: dana
      billing: finance@acme.test
    ---
    # Acme

    ## Pricing

    45k for 200 seats, annual.

    ## Notes

    Renewal in Q4. Intro came from Dana.
    """
)

ACME_V2 = textwrap.dedent(
    """\
    ---
    client: Acme
    pricing: "$48k"
    status: active
    tags: [enterprise, q4]
    contacts:
      owner: dana
      billing: finance@acme.test
    ---
    # Acme

    ## Pricing

    48k for 200 seats, annual.

    ## Notes

    Renewal in Q4. Intro came from Dana.
    """
)


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    (root / "clients").mkdir(parents=True)
    (root / "topics").mkdir(parents=True)
    (root / "clients" / "acme.md").write_text(ACME_V1, encoding="utf-8")
    (root / "topics" / "pricing.md").write_text("# Pricing\n\nTiers TBD.\n", encoding="utf-8")
    # The same declaration `memdrift init` makes. Tests that drive the CLI, the
    # MCP subprocess or the web app are *this install* acting on this vault, so
    # the vault has to be inside a space — otherwise the scope guard refuses
    # before the thing under test ever runs.
    scope.allow(root, mode="write")
    return root


@pytest.fixture
def repo(vault):
    r = Repo.init(vault, author_type="agent", author_name="basic-memory")
    ops.commit(r, "initial snapshot", author_type="human", author_name="you")
    return r
