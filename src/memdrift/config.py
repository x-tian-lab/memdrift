"""Configuration loading for a memdrift repository.

The config lives at ``<memory-dir>/.memgit/config.toml`` and is deliberately
plain TOML so a human can read and edit it without our help.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_FILENAME = "config.toml"

DEFAULT_CONFIG = """\
# memdrift config — safe to edit by hand.
# Delete .memgit/ entirely and your memory files stay exactly as they are.

[memory]
root = "."
include = ["**/*.md"]
exclude = [".memgit/**", "**/node_modules/**", "**/.git/**"]

[commit]
debounce_ms = 5000
author_type = "agent"     # agent | human
author_name = "memdrift"

[digest]
timezone = "Asia/Shanghai"
risk_thresholds = { confidence = 0.6, change_ratio = 0.5 }
"""


@dataclass
class Config:
    root: str = "."
    include: list[str] = field(default_factory=lambda: ["**/*.md"])
    exclude: list[str] = field(
        default_factory=lambda: [".memgit/**", "**/node_modules/**", "**/.git/**"]
    )
    debounce_ms: int = 5000
    author_type: str = "agent"
    author_name: str = "memdrift"
    timezone: str = "Asia/Shanghai"
    risk_confidence: float = 0.6
    risk_change_ratio: float = 0.5

    @classmethod
    def from_file(cls, path: Path) -> "Config":
        raw = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

        memory = raw.get("memory", {})
        commit = raw.get("commit", {})
        digest = raw.get("digest", {})
        risks = digest.get("risk_thresholds", {})

        return cls(
            root=memory.get("root", "."),
            include=list(memory.get("include", ["**/*.md"])),
            exclude=list(
                memory.get("exclude", [".memgit/**", "**/node_modules/**", "**/.git/**"])
            ),
            debounce_ms=int(commit.get("debounce_ms", 5000)),
            author_type=str(commit.get("author_type", "agent")),
            author_name=str(commit.get("author_name", "memdrift")),
            timezone=str(digest.get("timezone", "Asia/Shanghai")),
            risk_confidence=float(risks.get("confidence", 0.6)),
            risk_change_ratio=float(risks.get("change_ratio", 0.5)),
        )

    def to_toml(self) -> str:
        """Serialize back to TOML (small, hand-rolled — no extra dependency)."""
        include = ", ".join(f'"{p}"' for p in self.include)
        exclude = ", ".join(f'"{p}"' for p in self.exclude)
        return (
            f'[memory]\nroot = "{self.root}"\ninclude = [{include}]\n'
            f"exclude = [{exclude}]\n\n"
            f"[commit]\ndebounce_ms = {self.debounce_ms}\n"
            f'author_type = "{self.author_type}"\n'
            f'author_name = "{self.author_name}"\n\n'
            f"[digest]\ntimezone = \"{self.timezone}\"\n"
            f"risk_thresholds = {{ confidence = {self.risk_confidence}, "
            f"change_ratio = {self.risk_change_ratio} }}\n"
        )
