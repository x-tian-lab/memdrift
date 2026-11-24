from __future__ import annotations

from memdrift.parse import flatten_meta, parse_page, split_sections


def test_flatten_nested_frontmatter():
    flat = flatten_meta({"a": {"b": 1, "c": {"d": "x"}}, "tags": ["t1", "t2"]})
    assert flat == {"a.b": 1, "a.c.d": "x", "tags": ["t1", "t2"]}


def test_split_sections_basic():
    preamble, sections = split_sections("intro\n\n## A\n\na body\n\n## B\n\nb body\n")
    assert preamble == "intro"
    assert set(sections) == {"A", "B"}
    assert sections["A"] == "a body"


def test_text_before_first_subheading_belongs_to_the_page_title():
    _, sections = split_sections("# Title\n\nintro\n\n## A\n\na body\n")
    assert sections["Title"] == "intro"
    assert sections["A"] == "a body"


def test_duplicate_headings_are_kept():
    _, sections = split_sections("## Log\n\nfirst\n\n## Log\n\nsecond\n")
    assert sections == {"Log": "first", "Log #2": "second"}


def test_parse_page_lifts_fields():
    text = '---\npricing: "$45k"\nclient: Acme\n---\n# Acme\n\n## Pricing\n\n45k.\n'
    page = parse_page("clients/acme.md", text)
    assert page.fields["pricing"] == "$45k"
    assert page.fields["client"] == "Acme"
    assert page.sections["Pricing"] == "45k."
    assert page.frontmatter_ok


def test_broken_yaml_does_not_raise():
    page = parse_page("x.md", "---\n: : bad yaml : :\n---\n# H\n\nbody\n")
    assert page.frontmatter_ok is False or page.fields == {}
    assert page.raw.startswith("---")


def test_no_frontmatter_page():
    page = parse_page("plain.md", "# Notes\n\nhello\n")
    assert page.fields == {}
    assert page.sections == {"Notes": "hello"}
