"""The guided tour is a deliverable, not a decoration.

It is generated from real command output. These tests do not regenerate it
(that is the build script's job) — they guard the checked-in page against the
two ways it could silently rot: a command changing its output, or a local path
slipping into a page meant for the public.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from pathlib import Path

PAGE = Path(__file__).resolve().parents[1] / "docs" / "onboarding.html"


def _page() -> str:
    assert PAGE.exists(), "run scripts/build_onboarding.py first"
    return PAGE.read_text(encoding="utf-8")


def _data() -> dict:
    text = _page()
    match = re.search(r"const DATA = (\{.*?\});\n", text, re.S)
    assert match, "embedded DATA block is missing"
    return json.loads(match.group(1).replace("<\\/", "</"))


def test_nine_steps_from_zero_to_understanding():
    data = _data()
    assert len(data["steps"]) == 9
    assert "什么都不知道" in data["steps"][0]["nav"]
    assert "全懂" in data["steps"][-1]["nav"]
    for step in data["steps"]:
        assert step["question"]
        assert step["blocks"]
        assert len(step["learn"]) >= 2


def test_glossary_terms_are_all_resolvable():
    data = _data()
    keys = set(data["terms"])
    assert len(keys) >= 8
    used = {
        m.group(1)
        for step in data["steps"]
        for block in step["blocks"]
        for m in re.finditer(r'data-k="([a-z]+)"', block.get("html", ""))
    }
    assert used, "no clickable glossary pills rendered"
    assert used <= keys, f"unresolved glossary keys: {used - keys}"


def test_note_markup_is_rendered_not_printed():
    """Found in a real browser, not by the DOM stub: a note with no glossary
    pill had its <b> escaped and showed up as literal text on the page."""
    for step in _data()["steps"]:
        for block in step["blocks"]:
            # Only authored notes: terminal output legitimately escapes `<`.
            if block.get("kind") != "note":
                continue
            rendered = block.get("html", "")
            for tag in ("<b>", "<code>", "</b>", "</code>"):
                assert html_lib.escape(tag) not in rendered, f"step {step['nav']!r} prints {tag!r}"


def test_page_is_offline_and_self_contained():
    text = _page()
    assert not re.findall(r"(?:src|href)\s*=\s*[\"']https?://[^\"']+", text)
    assert "url(http" not in text
    assert text.count("<script>") == 1


def test_no_machine_specific_paths():
    text = _page()
    for bad in ("2026-11-23-19-32-06", "AppData", "WorkBuddy", "Users\\admin"):
        assert bad not in text, f"leaked: {bad!r}"


def test_real_output_is_embedded_not_placeholders():
    """A broken build should fail loudly, not produce an empty-looking tour."""
    text = _page()
    for needle in (
        "content intact",
        "nightly consolidation",
        "email:msg-1882",
        "$12k",
        "$48k",
        "7 tools",
        "memory.revert",
        "readOnlyHint",
        "dry run",
    ):
        assert needle in text, f"missing real output: {needle!r}"


def test_last_page_names_the_three_gaps():
    """Knowing the boundaries is part of 'fully understanding' the tool."""
    data = _data()
    kinds = [b["kind"] for b in data["steps"][-1]["blocks"]]
    assert "cheat" in kinds and "todo" in kinds

    text = _page()
    for gap in ("1 · 并发写入", "2 · 规则引擎", "3 · 完整的 --exclude", "refs/head"):
        assert gap in text, f"final page forgot a known gap: {gap!r}"
    assert len(data["steps"][-1]["learn"]) >= 2
