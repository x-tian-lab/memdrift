"""Fail loudly if the onboarding page is not what we promised.

Run after build_onboarding.py. Checks the four things that matter:
  1. The embedded DATA parses and has all nine steps.
  2. Nothing is loaded from the network (offline, self-contained).
  3. No machine-specific absolute path leaked into a public page.
  4. The terminal blocks still contain the *real* command output, not
     placeholders — a broken build should not silently produce an empty tour.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PAGE = Path(__file__).resolve().parents[1] / "docs" / "onboarding.html"

NEEDLES = (
    "content intact",
    "sha256 a.md",
    "nightly consolidation",
    "email:msg-1882",
    "frontmatter.pricing",
    "$12k",
    "$48k",
    "7 tools",
    "memory.revert",
    "readOnlyHint",
    "dry run",
    "refs/head",
)

LEAKS = (
    "2026-11-23-19-32-06",
    "AppData",
    "WorkBuddy",
    "Users\\admin",
    "Users/admin",
)


def main() -> int:
    text = PAGE.read_text(encoding="utf-8")
    errors: list[str] = []

    match = re.search(r"const DATA = (\{.*?\});\n", text, re.S)
    if not match:
        errors.append("no embedded DATA block")
        data = {}
    else:
        try:
            data = json.loads(match.group(1).replace("<\\/", "</"))
        except json.JSONDecodeError as exc:
            errors.append(f"DATA is not valid JSON: {exc}")
            data = {}

    steps = data.get("steps", [])
    if len(steps) != 9:
        errors.append(f"expected 9 steps, found {len(steps)}")
    for index, step in enumerate(steps):
        for key in ("nav", "eyebrow", "question", "hint", "blocks", "learn"):
            if not step.get(key):
                errors.append(f"step {index + 1} missing {key}")
    if len(data.get("terms", {})) < 8:
        errors.append("glossary is missing entries")

    external = re.findall(r"(?:src|href)\s*=\s*[\"']https?://[^\"']+", text)
    if external:
        errors.append(f"external resource(s): {external[:3]}")
    if "url(http" in text:
        errors.append("CSS loads a remote url()")

    for bad in LEAKS:
        if bad in text:
            errors.append(f"leaked local path fragment: {bad!r}")

    for needle in NEEDLES:
        if needle not in text:
            errors.append(f"missing real output: {needle!r}")

    if text.count("<script>") != 1 or text.count("</script>") != 1:
        errors.append("unexpected <script> structure")
    if "const DATA" not in text or "function render()" not in text:
        errors.append("page JS is incomplete")

    if errors:
        for line in errors:
            print("FAIL", line)
        return 1
    print(f"OK  {PAGE.name}: {len(steps)} steps, {len(data['terms'])} glossary terms, "
          f"offline, redacted, real output present")
    return 0


if __name__ == "__main__":
    sys.exit(main())
