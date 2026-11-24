from __future__ import annotations

import shutil

from memdrift import operations as ops
from memdrift.repo import Repo, short


def test_init_creates_meta_dir(vault):
    repo = Repo.init(vault)
    assert (vault / ".memgit" / "config.toml").exists()
    assert (vault / ".memgit" / "objects").is_dir()
    assert repo.config.include == ["**/*.md"]


def test_snapshot_ignores_meta_dir(repo):
    paths = set(repo.iter_memory_files())
    assert "clients/acme.md" in paths
    assert not any(p.startswith(".memgit") for p in paths)


def test_first_commit_is_the_root(repo):
    commit = repo.read_commit(repo.head())
    assert commit["parent"] is None
    assert commit["author"]["type"] == "human"
    assert len(commit["changes"]) == 2  # two pages added


def test_commit_chain_links_parent(repo, vault):
    (vault / "clients" / "acme.md").write_text(
        (vault / "clients" / "acme.md").read_text().replace('"$45k"', '"$48k"'),
        encoding="utf-8",
    )
    before = repo.head()
    commit = ops.commit(repo, "quote updated")
    assert commit["parent"] == before
    assert repo.head() == commit["id"]

    history = [c["id"] for c in repo.walk_history()]
    assert history[0] == commit["id"]
    assert history[-1] == before


def test_nothing_to_commit_returns_none(repo):
    assert ops.commit(repo, "noop") is None


def test_deleting_memgit_keeps_memory_files(repo, vault):
    original = (vault / "clients" / "acme.md").read_text(encoding="utf-8")
    shutil.rmtree(vault / ".memgit")
    assert (vault / "clients" / "acme.md").read_text(encoding="utf-8") == original


def test_meta_dir_survives_losing_objects(repo, vault):
    """The PRD's bottom line: history may die, current content must not."""
    shutil.rmtree(vault / ".memgit" / "objects")
    assert (vault / "clients" / "acme.md").exists()


def test_status_tracks_each_kind_of_change(repo, vault):
    (vault / "clients" / "acme.md").write_text("# changed\n", encoding="utf-8")
    (vault / "topics" / "new.md").write_text("# New\n", encoding="utf-8")
    (vault / "topics" / "pricing.md").rename(vault / "topics" / "pricing-v2.md")

    report = ops.status(repo)
    assert report.added == ["topics/new.md"]
    assert report.modified == ["clients/acme.md"]
    assert report.renamed == [("topics/pricing.md", "topics/pricing-v2.md")]
    assert not report.clean


def test_rename_survives_a_commit(repo, vault):
    (vault / "topics" / "pricing.md").rename(vault / "topics" / "pricing-v2.md")
    commit = ops.commit(repo, "renamed pricing note")
    ops_ = [c for c in commit["changes"] if c["op"] == "rename"]
    assert ops_ and ops_[0]["from"] == "topics/pricing.md"
    assert ops_[0]["path"] == "topics/pricing-v2.md"


def test_blame_points_at_the_field_and_evidence(repo, vault):
    acme = vault / "clients" / "acme.md"
    acme.write_text(acme.read_text().replace('"$45k"', '"$48k"'), encoding="utf-8")
    commit = ops.commit(
        repo,
        "nightly consolidation",
        evidence=["email:msg-1882"],
        confidence=0.82,
    )

    entries = ops.blame(repo, "clients/acme.md")
    by_field = {e["field"]: e for e in entries}
    pricing = by_field["frontmatter.pricing"]
    assert pricing["before"] == "$45k"
    assert pricing["after"] == "$48k"
    assert pricing["commit"] == short(commit["id"])
    assert pricing["evidence"][0]["kind"] == "email"
    assert pricing["evidence"][0]["ref"] == "msg-1882"
    assert pricing["confidence"] == 0.82


def test_blame_covers_sections_too(repo, vault):
    acme = vault / "clients" / "acme.md"
    acme.write_text(
        acme.read_text().replace("45k for 200 seats", "48k for 200 seats"),
        encoding="utf-8",
    )
    ops.commit(repo, "pricing section edited")
    fields = [e["field"] for e in ops.blame(repo, "clients/acme.md")]
    assert "section:Pricing" in fields


def test_log_filters_by_page_and_since(repo, vault):
    (vault / "topics" / "pricing.md").write_text("# Pricing\n\nTiers done.\n", encoding="utf-8")
    ops.commit(repo, "pricing filled in")

    only_pricing = ops.log(repo, page="topics/pricing.md")
    assert [c["message"] for c in only_pricing] == ["pricing filled in", "initial snapshot"]

    recent = ops.log(repo, since="1h")
    assert recent, "a commit made seconds ago must show up under --since 1h"


def test_index_rebuild_matches_walk(repo, vault):
    (vault / "topics" / "pricing.md").write_text("# Pricing\n\nx\n", encoding="utf-8")
    ops.commit(repo, "another change")
    n = ops.rebuild_index(repo)
    assert n == repo.count_commits()


def test_evidence_parsing():
    assert ops.parse_evidence(["email:msg-1"]) == [{"kind": "email", "ref": "msg-1"}]
    assert ops.parse_evidence(["https://x.test/a"]) == [
        {"kind": "url", "ref": "https://x.test/a"}
    ]
    assert ops.parse_evidence(["msg-9"]) == [{"kind": "manual", "ref": "msg-9"}]
