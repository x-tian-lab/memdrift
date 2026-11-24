from __future__ import annotations

import textwrap

from memdrift.differ import change_ratio, diff_pages, diff_trees
from memdrift.parse import parse_page


def page(text: str, path: str = "clients/acme.md"):
    return parse_page(path, textwrap.dedent(text))


BASE = """\
---
client: Acme
pricing: "$45k"
status: active
---
# Acme

## Pricing

45k for 200 seats.
"""


def test_no_change_returns_nothing():
    assert diff_pages(page(BASE), page(BASE)) == []


def test_frontmatter_field_change_is_named():
    after = BASE.replace('"$45k"', '"$48k"')
    changes = diff_pages(page(BASE), page(after))
    fields = {c.field: c for c in changes}
    assert "frontmatter.pricing" in fields
    change = fields["frontmatter.pricing"]
    assert change.before == "$45k"
    assert change.after == "$48k"
    assert 0 < change.change_ratio <= 1


def test_section_change_is_attributed_to_heading():
    after = BASE.replace("45k for 200 seats.", "48k for 200 seats.")
    changes = diff_pages(page(BASE), page(after))
    fields = {c.field for c in changes}
    assert "section:Pricing" in fields


def test_added_page_reports_sections():
    changes = diff_pages(None, page(BASE))
    assert len(changes) == 1
    assert changes[0].op == "add"
    assert changes[0].sections == ["Acme", "Pricing"]
    assert changes[0].change_ratio == 1.0


def test_deleted_page():
    changes = diff_pages(page(BASE), None)
    assert [c.op for c in changes] == ["delete"]


def test_added_frontmatter_key():
    after = BASE.replace("status: active", "status: active\nstage: negotiation")
    changes = diff_pages(page(BASE), page(after))
    change = next(c for c in changes if c.field == "frontmatter.stage")
    assert change.before is None
    assert change.after == "negotiation"


def test_removed_section():
    after = BASE.split("## Pricing")[0]
    changes = diff_pages(page(BASE), page(after))
    change = next(c for c in changes if c.field == "section:Pricing")
    assert change.after is None


def test_full_rewrite_degrades_to_low_precision_body():
    rewritten = """\
    # Acme

    ## Company

    Enterprise CRM.

    ## Buyers

    Dana, finance.

    ## Timeline

    Q4.
    """
    changes = diff_pages(page(BASE), page(rewritten))
    assert len(changes) == 1
    assert changes[0].field == "body"
    assert changes[0].low_precision is True


def test_whitespace_only_edit_is_not_silent():
    after = BASE + "\n"
    changes = diff_pages(page(BASE), page(after))
    assert changes, "a trailing newline still has to be recorded somewhere"
    assert all(c.change_ratio > 0 for c in changes)


def test_heading_rename_is_one_change_not_delete_plus_add():
    old = """\
    # 周会纪要 11/21

    ## 议题

    1. Acme 续约报价

    ## 决定

    Acme 可以让到 48k。
    """
    new = old.replace("11/21", "11/22")
    changes = diff_pages(page(old, "sessions/weekly.md"), page(new, "sessions/weekly.md"))

    assert len(changes) == 1
    change = changes[0]
    assert change.op == "rename"
    assert change.section_rename is True
    assert change.before == "周会纪要 11/21"
    assert change.after == "周会纪要 11/22"
    assert change.low_precision is False


def test_heading_rename_plus_body_edit_reports_both():
    old = "# 周会纪要 11/21\n\n本周三件事。\n\n## 议题\n\n1. Acme 续约报价\n"
    new = "# 周会纪要 11/22\n\n本周四件事。\n\n## 议题\n\n1. Acme 续约报价\n"
    changes = diff_pages(page(old, "s.md"), page(new, "s.md"))

    ops = {c.op for c in changes if c.section == "周会纪要 11/22"}
    assert "rename" in ops and "modify" in ops


def test_unrelated_new_heading_is_not_a_rename():
    old = "# A\n\n## 议题\n\nsame body\n"
    new = "# A\n\n## 议题\n\nsame body\n\n## 完全不同\n\nnew stuff\n"
    changes = diff_pages(page(old, "s.md"), page(new, "s.md"))
    assert [c.op for c in changes] == ["modify"]
    assert changes[0].field == "section:完全不同"


def test_change_ratio_bounds():
    assert change_ratio("a\nb\n", "a\nb\n") == 0.0
    assert change_ratio("a\n", "z\n") == 1.0
    # 4 lines, 1 replaced → 0.25 of the page
    assert change_ratio("a\nb\nc\nd\n", "a\nb\nc\nX\n") == 0.25


def test_diff_trees_detects_rename_by_content():
    text = BASE
    old_tree = {"contacts/tom.md": "sha256:abc"}
    new_tree = {"contacts/thomas.md": "sha256:abc"}
    changes = diff_trees(old_tree, new_tree, lambda blob: text)
    assert [c.op for c in changes] == ["rename"]
    assert changes[0].rename_from == "contacts/tom.md"
    assert changes[0].path == "contacts/thomas.md"


def test_diff_trees_ignores_identical_blobs():
    tree = {"a.md": "sha256:1"}
    assert diff_trees(tree, dict(tree), lambda b: BASE) == []
