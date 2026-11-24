"""SQLite query index.

The commit files are the source of truth; this index is a cache. It can be
deleted or rebuilt at any time (``memdrift index --rebuild``), which is what
makes the PRD's "corruption is survivable" promise real.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS commits (
    id           TEXT PRIMARY KEY,
    parent       TEXT,
    ts           TEXT NOT NULL,
    ts_epoch     REAL NOT NULL,
    author_type  TEXT,
    author_name  TEXT,
    message      TEXT,
    confidence   REAL,
    evidence     TEXT,
    tree         TEXT,
    seq          INTEGER
);
CREATE TABLE IF NOT EXISTS changes (
    commit_id      TEXT NOT NULL,
    path           TEXT NOT NULL,
    op             TEXT NOT NULL,
    field          TEXT,
    before_t       TEXT,
    after_t        TEXT,
    section        TEXT,
    change_ratio   REAL,
    low_precision  INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_changes_path ON changes(path);
CREATE INDEX IF NOT EXISTS ix_changes_commit ON changes(commit_id);
CREATE INDEX IF NOT EXISTS ix_commits_ts ON commits(ts_epoch);
"""

SINCE_RE = re.compile(r"^(\d+)\s*([dhmw])$", re.IGNORECASE)
UNIT_SECONDS = {"h": 3600, "d": 86400, "w": 604800, "m": 2592000}

SCHEMA_VERSION = 2


def parse_since(text: str | None) -> float | None:
    """'7d' / '36h' / '2w' / '2026-11-20' → epoch seconds."""
    if not text:
        return None
    match = SINCE_RE.match(text.strip())
    if match:
        amount, unit = int(match.group(1)), match.group(2).lower()
        return (datetime.now() - timedelta(seconds=amount * UNIT_SECONDS[unit])).timestamp()
    try:
        parsed = datetime.fromisoformat(text.strip())
    except ValueError as exc:
        raise ValueError(f"bad --since value {text!r} (try 7d, 36h, 2026-11-20)") from exc
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.timestamp()


def _epoch(ts: str) -> float:
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.timestamp()


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


class Index:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            # The index is only a cache: drop and let ops.log() rebuild it.
            self.conn.executescript(
                "DROP TABLE IF EXISTS changes; DROP TABLE IF EXISTS commits;"
            )
            self.conn.executescript(SCHEMA)
            self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Index":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ----------------------------------------------------------------- write

    def _next_seq(self) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM commits").fetchone()
        return int(row["n"])

    def add_commit(self, commit: dict) -> None:
        author = commit.get("author") or {}
        existing = self.conn.execute(
            "SELECT seq FROM commits WHERE id = ?", (commit["id"],)
        ).fetchone()
        seq = int(existing["seq"]) if existing else self._next_seq()
        cur = self.conn
        cur.execute(
            "INSERT OR REPLACE INTO commits "
            "(id, parent, ts, ts_epoch, author_type, author_name, message, "
            " confidence, evidence, tree, seq) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                commit["id"],
                commit.get("parent"),
                commit.get("ts", ""),
                _epoch(commit.get("ts", "")),
                author.get("type"),
                author.get("name"),
                commit.get("message", ""),
                commit.get("confidence"),
                json.dumps(commit.get("evidence") or [], ensure_ascii=False),
                commit.get("tree"),
                seq,
            ),
        )
        cur.execute("DELETE FROM changes WHERE commit_id = ?", (commit["id"],))
        for change in commit.get("changes") or []:
            cur.execute(
                "INSERT INTO changes VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    commit["id"],
                    change.get("path", ""),
                    change.get("op", "modify"),
                    change.get("field"),
                    _text(change.get("before")),
                    _text(change.get("after")),
                    change.get("section"),
                    float(change.get("change_ratio") or 0.0),
                    int(bool(change.get("low_precision"))),
                ),
            )
        cur.commit()

    def rebuild(self, commits: Iterable[dict]) -> int:
        self.conn.execute("DELETE FROM commits")
        self.conn.execute("DELETE FROM changes")
        # oldest first so `seq` grows with history, not with hash order
        ordered = list(commits)[::-1]
        count = 0
        for commit in ordered:
            self.add_commit(commit)
            count += 1
        return count

    # ------------------------------------------------------------------ read

    def log(
        self,
        *,
        page: str | None = None,
        since: float | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        params: list[Any] = []
        sql = "SELECT DISTINCT c.* FROM commits c"
        if page:
            sql += " JOIN changes ch ON ch.commit_id = c.id WHERE ch.path = ?"
            params.append(page)
        else:
            sql += " WHERE 1=1"
        if since is not None:
            sql += " AND c.ts_epoch >= ?"
            params.append(since)
        sql += " ORDER BY c.ts_epoch DESC, c.seq DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)

        rows = [dict(r) for r in self.conn.execute(sql, params)]
        for row in rows:
            row["evidence"] = json.loads(row.get("evidence") or "[]")
            row["changes"] = self.changes_of(row["id"], page=page)
        return rows

    def changes_of(self, commit_id: str, *, page: str | None = None) -> list[dict]:
        sql = "SELECT * FROM changes WHERE commit_id = ?"
        params: list[Any] = [commit_id]
        if page:
            sql += " AND path = ?"
            params.append(page)
        sql += " ORDER BY path, field"
        out = []
        for row in self.conn.execute(sql, params):
            item = dict(row)
            item["change_ratio"] = item.get("change_ratio") or 0.0
            item["low_precision"] = bool(item.get("low_precision"))
            out.append(item)
        return out

    def count_commits(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM commits").fetchone()
        return int(row["n"]) if row else 0

    def has_commit(self, cid: str | None) -> bool:
        if not cid:
            return False
        row = self.conn.execute("SELECT 1 FROM commits WHERE id = ?", (cid,)).fetchone()
        return row is not None

    def page_history(self, path: str) -> list[dict]:
        sql = (
            "SELECT c.id, c.ts, c.ts_epoch, c.author_name, c.author_type, c.message "
            "FROM commits c JOIN changes ch ON ch.commit_id = c.id "
            "WHERE ch.path = ? ORDER BY c.ts_epoch DESC, c.seq DESC"
        )
        return [dict(r) for r in self.conn.execute(sql, (path,))]
