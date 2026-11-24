from __future__ import annotations

import textwrap

import pytest

from memdrift import operations as ops
from memdrift.repo import Repo

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
    return root


@pytest.fixture
def repo(vault):
    r = Repo.init(vault, author_type="agent", author_name="basic-memory")
    ops.commit(r, "initial snapshot", author_type="human", author_name="you")
    return r
