from __future__ import annotations

import hashlib

import pytest

from memdrift import operations as ops
from memdrift import revert
from memdrift.repo import RepoError


def _replace(path, old, new):
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------- unit: editing


def test_set_scalar_keeps_double_quotes():
    text = '---\npricing: "$48k"\n---\n# A\n'
    out = revert.apply_field(text, "frontmatter.pricing", "$45k")
    assert 'pricing: "$45k"' in out


def test_set_scalar_keeps_single_quotes():
    text = "---\nname: 'Dana'\n---\n# A\n"
    out = revert.apply_field(text, "frontmatter.name", "Tom")
    assert "name: 'Tom'" in out


def test_set_scalar_unquoted_stays_unquoted():
    text = "---\nstatus: on-hold\n---\n# A\n"
    out = revert.apply_field(text, "frontmatter.status", "active")
    assert "status: active" in out


def test_set_scalar_adds_missing_key():
    text = "---\na: 1\n---\n# A\n"
    out = revert.apply_field(text, "frontmatter.b", "2")
    assert "b: 2" in out
    assert "a: 1" in out


def test_delete_key_removes_it():
    text = '---\na: 1\npricing: "$45k"\n---\n# A\n'
    out = revert.apply_field(text, "frontmatter.pricing", None)
    assert "pricing" not in out
    assert "a: 1" in out


def test_nested_key_is_set_in_place():
    text = "---\ncontacts:\n  owner: dana\n  billing: f@x.test\n---\n# A\n"
    out = revert.apply_field(text, "frontmatter.contacts.owner", "tom")
    assert "owner: tom" in out
    assert "billing: f@x.test" in out


def test_section_is_replaced():
    text = "# A\n\n## Notes\n\nnew text\n\n## Other\n\nkeep\n"
    out = revert.apply_field(text, "section:Notes", "old text")
    assert "old text" in out
    assert "new text" not in out
    assert "keep" in out


def test_unknown_field_raises():
    with pytest.raises(RepoError):
        revert.apply_field("x", "nonsense", 1)


def test_line_endings_survive_a_revert(tmp_path):
    """A revert must not turn \\n into \\r\\n (the bug that proved this test)."""
    path = tmp_path / "a.md"
    path.write_bytes(b'---\np: "$1"\n---\n# A\n\nbody\n')
    before = _sha(path)
    text = path.read_text(encoding="utf-8", newline="")
    out = revert.apply_field(text, "frontmatter.p", "$2")
    path.write_text(out, encoding="utf-8", newline="")
    assert b"\r\n" not in path.read_bytes()
    path.write_text(text, encoding="utf-8", newline="")
    assert _sha(path) == before


# ------------------------------------------------------------ plans & execute


@pytest.fixture
def history(repo, vault):
    """baseline → nightly (price + notes change)."""
    base = repo.head()
    _replace(vault / "clients" / "acme.md", "$45k", "$48k")
    _replace(vault / "clients" / "acme.md", "Renewal in Q4.", "Renewal in Q4, confirmed 48k.")
    ops.commit(repo, "nightly")
    return base


def test_plan_field_reports_before_and_after(repo, vault, history):
    plan = revert.plan_field(repo, "clients/acme.md", "frontmatter.pricing", None)
    assert not plan.skipped
    assert plan.before == "$48k"
    assert plan.after == "$45k"


def test_field_revert_changes_only_that_field(repo, vault, history):
    plan = revert.plan_field(repo, "clients/acme.md", "frontmatter.pricing", None)
    revert.execute(repo, [plan])
    text = (vault / "clients" / "acme.md").read_text(encoding="utf-8")
    assert "$45k" in text
    assert "confirmed 48k" in text, "the other change must survive"


def test_revert_creates_a_new_commit_and_keeps_history(repo, vault, history):
    before_log = [c["id"] for c in repo.walk_history()]
    plan = revert.plan_field(repo, "clients/acme.md", "frontmatter.pricing", None)
    result = revert.execute(repo, [plan])
    assert result.commit is not None
    after_log = [c["id"] for c in repo.walk_history()]
    assert before_log == after_log[1:], "history must be appended to, never rewritten"
    assert after_log[0] == result.commit["id"]


def test_revert_commit_message_names_the_field(repo, vault, history):
    plan = revert.plan_field(repo, "clients/acme.md", "frontmatter.pricing", None)
    result = revert.execute(repo, [plan])
    assert "revert" in result.commit["message"]
    assert "frontmatter.pricing" in result.commit["message"]


def test_dry_run_writes_nothing(repo, vault, history):
    plan = revert.plan_field(repo, "clients/acme.md", "frontmatter.pricing", None)
    before = _sha(vault / "clients" / "acme.md")
    head = repo.head()
    result = revert.execute(repo, [plan], dry_run=True)
    assert result.commit is None
    assert _sha(vault / "clients" / "acme.md") == before
    assert repo.head() == head


def test_page_revert_restores_byte_identical_file(repo, vault):
    """The claim worth testing: undo a whole page and the bytes come back."""
    base = repo.head()  # the fixture already committed this state
    original = _sha(vault / "clients" / "acme.md")

    _replace(vault / "clients" / "acme.md", "$45k", "$48k")
    _replace(vault / "clients" / "acme.md", "Renewal in Q4.", "Renewal in Q4, confirmed 48k.")
    ops.commit(repo, "nightly")

    revert.execute(repo, revert.plan_page(repo, "clients/acme.md", base))
    assert _sha(vault / "clients" / "acme.md") == original


def test_page_revert_reports_nothing_when_identical(repo, vault, history):
    plans = revert.plan_page(repo, "clients/acme.md", repo.head())
    assert plans[0].skipped


def test_commit_revert_undoes_every_change(repo, vault):
    before = {p.name: _sha(p) for p in (vault / "clients").iterdir()}
    _replace(vault / "clients" / "acme.md", "$45k", "$48k")
    (vault / "clients" / "new.md").write_text("# New\n\nhi\n", encoding="utf-8")
    ops.commit(repo, "nightly")

    result = revert.execute(repo, revert.plan_commit(repo, repo.head()))
    assert result.commit is not None
    assert not (vault / "clients" / "new.md").exists(), "a page the commit created is removed"
    assert _sha(vault / "clients" / "acme.md") == before["acme.md"]


def test_cannot_revert_the_first_commit(repo):
    with pytest.raises(RepoError, match="nothing before"):
        revert.plan_commit(repo, repo.head())


def test_unknown_commit_raises_not_silently_skips(repo, vault, history):
    with pytest.raises(RepoError, match="unknown commit"):
        revert.plan_page(repo, "clients/acme.md", "deadbeef")


def test_unknown_field_degrades_to_skip_with_reason(repo, vault, history):
    plan = revert.plan_field(repo, "clients/acme.md", "frontmatter.nope", None)
    assert plan.skipped
    assert "no commit" in plan.reason


def test_revert_of_a_revert_returns_to_the_new_value(repo, vault, history):
    plan = revert.plan_field(repo, "clients/acme.md", "frontmatter.pricing", None)
    revert.execute(repo, [plan])
    assert "$45k" in (vault / "clients" / "acme.md").read_text(encoding="utf-8")

    plan2 = revert.plan_field(repo, "clients/acme.md", "frontmatter.pricing", None)
    revert.execute(repo, [plan2])
    assert "$48k" in (vault / "clients" / "acme.md").read_text(encoding="utf-8")


def test_revert_is_recorded_as_a_normal_change(repo, vault, history):
    from memdrift import events

    plan = revert.plan_field(repo, "clients/acme.md", "frontmatter.pricing", None)
    revert.execute(repo, [plan])
    stream = events.build(repo)
    newest = stream.events[0]
    assert newest["field"] == "frontmatter.pricing"
    assert newest["after"] == "$45k"
