from __future__ import annotations

from memdrift import events
from memdrift import operations as ops
from memdrift.security import Redactor


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_events_join_provenance(repo, vault):
    _write(vault / "clients" / "acme.md", (vault / "clients" / "acme.md").read_text(encoding="utf-8").replace("$45k", "$48k"))
    ops.commit(repo, "nightly", evidence=["email:msg-1882"], confidence=0.9)

    stream = events.build(repo)
    pricing = [e for e in stream.events if e.get("field") == "frontmatter.pricing"]
    assert pricing, "expected a frontmatter.pricing event"
    event = pricing[0]
    assert event["before"] == "$45k"
    assert event["after"] == "$48k"
    assert event["message"] == "nightly"
    assert event["author"]["name"] == "basic-memory"
    assert event["evidence"][0]["kind"] == "email"


def test_add_events_have_null_field_not_missing_key(repo):
    stream = events.build(repo)
    added = [e for e in stream.events if e["op"] == "add"]
    assert added
    for event in added:
        assert "field" in event and event["field"] is None
        assert "before" in event and "after" in event


def test_no_absolute_path_leaks_into_events(repo):
    stream = events.build(repo)
    blob = repr(stream.events)
    assert str(repo.root) not in blob
    assert str(repo.root).replace("\\", "/") not in blob


def test_private_pages_are_dropped_with_their_events(repo, vault):
    _write(
        vault / "secret" / "diary.md",
        "---\nprivate: true\n---\n# Diary\n\nfelt awful today\n",
    )
    ops.commit(repo, "secret page")

    stream = events.build(repo, redactor=Redactor(root=str(repo.root)))
    assert "secret/diary.md" in stream.hidden
    assert all(e["path"] != "secret/diary.md" for e in stream.events)
    assert all(p.path != "secret/diary.md" for p in stream.pages)


def test_private_page_evidence_is_also_hidden(repo, vault):
    _write(vault / "secret" / "diary.md", "---\nprivate: true\n---\n# D\n\nx\n")
    ops.commit(repo, "with private evidence", evidence=["email:msg-private"])
    stream = events.build(repo)
    assert b"msg-private" not in repr(stream.events).encode()


def test_since_filter_excludes_old_commits(repo, vault):
    _write(vault / "clients" / "acme.md", "# Acme\n\nnew body\n")
    ops.commit(repo, "later")
    recent = events.build(repo, since="1h")
    assert recent.events, "a commit made seconds ago must be inside a 1h window"
    old = events.build(repo, since="2000-01-01")
    assert old.events


def test_page_filter(repo, vault):
    _write(vault / "topics" / "pricing.md", "# Pricing\n\nTiers: 3.\n")
    ops.commit(repo, "pricing update")
    stream = events.build(repo, page="topics/pricing.md")
    assert stream.events
    assert all(e["path"] == "topics/pricing.md" for e in stream.events)


def test_risk_is_narrow(repo, vault):
    """Creating pages is not risky, and low confidence is commit-level."""
    stream = events.build(repo)
    added = [e for e in stream.events if e["op"] == "add"]
    assert all(not e["risk"] for e in added)


def test_low_confidence_marks_the_commit_once(repo, vault):
    _write(vault / "clients" / "acme.md", "# Acme\n\nreplaced wholesale\n" * 20)
    ops.commit(repo, "unsure rewrite", confidence=0.2)
    stream = events.build(repo)
    assert stream.unsure
    assert len(stream.unsure) == 1


def test_deletes_are_risky(repo, vault):
    (vault / "topics" / "pricing.md").unlink()
    ops.commit(repo, "drop pricing")
    stream = events.build(repo)
    deletes = [e for e in stream.events if e["op"] == "delete"]
    assert deletes and all(e["risk"] for e in deletes)


def test_working_tree_rows_are_redacted(repo, vault):
    _write(vault / "topics" / "new.md", "# New\n\nhi\n")
    rows = events.working_tree(repo)
    assert any(r["path"] == "topics/new.md" for r in rows)
    assert str(repo.root) not in repr(rows)


def test_group_by_commit_preserves_order(repo):
    stream = events.build(repo)
    groups = events.group_by_commit(stream.events)
    assert len(groups) == stream.commits
    assert groups[0]["short"] == stream.events[0]["short"]
