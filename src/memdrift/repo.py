"""Repository: content-addressed object store + commit chain.

Layout (all inside ``<memory-dir>/.memgit/``)::

    config.toml
    objects/ab/cdef…      zlib-compressed "{kind} {size}\\0{data}", sha256 addressed
    commits/ab/cdef….json one json per commit
    refs/head             current commit id
    index.sqlite          query index, rebuildable at any time

Design rule from the PRD: the ``.md`` files are the only truth. Deleting
``.memgit/`` loses history but never loses memory. Nothing here ever writes
to a memory file.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import zlib
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from .config import CONFIG_FILENAME, DEFAULT_CONFIG, Config

META_DIR = ".memgit"
VERSION = "0.1.0"

#: A commit id is hex, optionally prefixed. Anything else is not a commit, and
#: must not reach the filesystem: ``read_commit`` builds a path out of it, and
#: ``"../../x"`` would resolve outside ``commits/``. Every other input in this
#: project is treated as untrusted; a ref from an MCP client is no different.
COMMIT_ID_RE = re.compile(r"^(?:sha256:)?[0-9a-fA-F]{3,64}$")


class RepoError(RuntimeError):
    """Raised for user-facing repository problems."""


def canonical(obj: Any) -> bytes:
    """Deterministic JSON: same logical object → same bytes → same id."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def object_id(data: bytes, kind: str = "blob") -> str:
    header = f"{kind} {len(data)}\0".encode("utf-8")
    return "sha256:" + hashlib.sha256(header + data).hexdigest()


def now_iso() -> str:
    """Local time with an explicit UTC offset, e.g. 2026-11-22T00:00:12+08:00."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def short(oid: str | None) -> str:
    if not oid:
        return "-"
    return oid.split(":", 1)[-1][:7]


def _is_excluded(rel: str, patterns: list[str]) -> bool:
    """Match an excluded path against gitignore-ish patterns, incl. ``**/name/**``."""
    posix = rel.replace(os.sep, "/")
    for pat in patterns:
        if fnmatch(posix, pat) or PurePosixPath(posix).match(pat):
            return True
        core = pat.strip("*").strip("/")
        if core and not any(ch in core for ch in "*?["):
            if posix == core or posix.startswith(core + "/") or f"/{core}/" in f"/{posix}":
                return True
    return False


class Repo:
    """A memory directory under version control."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.meta = self.root / META_DIR
        self.objects = self.meta / "objects"
        self.commits_dir = self.meta / "commits"
        self.refs = self.meta / "refs"
        self.config = Config.from_file(self.meta / CONFIG_FILENAME)

    # ------------------------------------------------------------------ setup

    @classmethod
    def init(
        cls,
        root: str | Path,
        *,
        author_type: str = "human",
        author_name: str = "you",
    ) -> "Repo":
        root = Path(root).resolve()
        if not root.exists():
            root.mkdir(parents=True, exist_ok=True)
        meta = root / META_DIR
        for sub in ("objects", "commits", "refs", "digests"):
            (meta / sub).mkdir(parents=True, exist_ok=True)
        cfg_path = meta / CONFIG_FILENAME
        if not cfg_path.exists():
            cfg_path.write_text(DEFAULT_CONFIG, encoding="utf-8")

        repo = cls(root)
        # Rewrite the two author defaults chosen at init time, keep the rest.
        if author_type != "human" or author_name != "you":
            repo.config.author_type = author_type
            repo.config.author_name = author_name
            cfg_path.write_text(repo.config.to_toml(), encoding="utf-8")

        # Creating a store is declaring a space: you typed `init` here, so this
        # install may write here. A vault you merely *receive* never goes
        # through this path, which is exactly why it stays read-only until
        # someone runs `memdrift space allow … --write`. Imported here to keep
        # the module graph acyclic: scope imports nothing from repo.
        from . import scope

        try:
            scope.allow(root, mode="write", by=author_name or "you")
        except (OSError, ValueError):
            # A read-only home directory (locked down CI, a container with no
            # writable HOME) must not make `init` fail: the vault is usable,
            # it just is not registered, and the guard will say so.
            pass
        return repo

    @classmethod
    def discover(cls, path: str | Path = ".") -> "Repo":
        here = Path(path).resolve()
        for cand in [here, *here.parents]:
            if (cand / META_DIR).is_dir():
                return cls(cand)
        raise RepoError(
            f"not a memdrift repository: {here}\n"
            f"run `memdrift init {here}` first"
        )

    @property
    def exists(self) -> bool:
        return self.meta.is_dir()

    # ----------------------------------------------------------------- store

    def _object_path(self, oid: str) -> Path:
        hexd = oid.split(":", 1)[-1]
        return self.objects / hexd[:2] / hexd[2:]

    def write_object(self, data: bytes, kind: str = "blob") -> str:
        oid = object_id(data, kind)
        path = self._object_path(oid)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            header = f"{kind} {len(data)}\0".encode("utf-8")
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(zlib.compress(header + data, 6))
            os.replace(tmp, path)
        return oid

    def read_object(self, oid: str) -> tuple[str, bytes]:
        path = self._object_path(oid)
        if not path.exists():
            raise RepoError(f"missing object {oid} — .memgit may be corrupted")
        raw = zlib.decompress(path.read_bytes())
        header, _, data = raw.partition(b"\0")
        return header.split(b" ")[0].decode("utf-8", "replace"), data

    def read_blob_text(self, oid: str) -> str:
        _, data = self.read_object(oid)
        return data.decode("utf-8", "replace")

    # ------------------------------------------------------------------ tree

    def write_tree(self, mapping: dict[str, str]) -> str:
        return self.write_object(canonical(mapping), "tree")

    def read_tree(self, tree_id: str | None) -> dict[str, str]:
        if not tree_id:
            return {}
        _, data = self.read_object(tree_id)
        return json.loads(data)

    # ---------------------------------------------------------------- commit

    def commit_path(self, cid: str) -> Path:
        hexd = cid.split(":", 1)[-1]
        return self.commits_dir / hexd[:2] / f"{hexd[2:]}.json"

    def write_commit(
        self,
        *,
        tree: str,
        parent: str | None,
        changes: list[dict],
        message: str,
        author: dict,
        evidence: list[dict],
        confidence: float | None,
        reverts: str | None = None,
    ) -> dict:
        payload: dict[str, Any] = {
            "parent": parent,
            "tree": tree,
            "ts": now_iso(),
            "author": author,
            "message": message,
            "changes": changes,
            "evidence": evidence,
            "confidence": confidence,
            "memdrift_version": VERSION,
        }
        if reverts:
            # Forward pointer, never a backward edit: undoing commit A writes a
            # new commit B that *says* it undoes A. A keeps its id, its tree,
            # its message and its place in the chain, so "what did the agent
            # actually do that night" stays answerable forever. Only present
            # when it applies, so payloads from before this field existed keep
            # hashing to the same id.
            payload["reverts"] = reverts
        cid = "sha256:" + hashlib.sha256(canonical(payload)).hexdigest()
        payload["id"] = cid

        path = self.commit_path(cid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        self.set_head(cid)
        return payload

    def read_commit(self, cid: str) -> dict:
        if not isinstance(cid, str) or not COMMIT_ID_RE.match(cid):
            raise RepoError(f"not a commit id: {cid!r}")
        path = self.commit_path(cid)
        if not path.exists():
            matches = self._resolve_prefix(cid)
            if len(matches) == 1:
                path = self.commit_path(matches[0])
            elif len(matches) > 1:
                raise RepoError(
                    f"ambiguous commit id {cid!r}: "
                    + ", ".join(short(m) for m in matches[:5])
                )
            else:
                raise RepoError(f"unknown commit: {cid}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _resolve_prefix(self, cid: str) -> list[str]:
        hexd = cid.split(":", 1)[-1]
        if len(hexd) < 3:
            return []
        out = []
        for shard in self.commits_dir.iterdir() if self.commits_dir.exists() else []:
            if not shard.is_dir() or not shard.name.startswith(hexd[:2]):
                continue
            for f in shard.glob("*.json"):
                full = shard.name + f.stem
                if full.startswith(hexd):
                    out.append("sha256:" + full)
        return out

    # ------------------------------------------------------------------ refs

    def head(self) -> str | None:
        path = self.refs / "head"
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8").strip()
        return text or None

    def set_head(self, cid: str) -> None:
        self.refs.mkdir(parents=True, exist_ok=True)
        (self.refs / "head").write_text(cid + "\n", encoding="utf-8")

    def resolve(self, ref: str | None) -> str | None:
        if ref in (None, "", "HEAD"):
            return self.head()
        if ref.upper() == "HEAD":
            return self.head()
        return self.read_commit(ref)["id"]

    def head_tree(self) -> dict[str, str]:
        head = self.head()
        if not head:
            return {}
        return self.read_tree(self.read_commit(head).get("tree"))

    def walk_history(self, start: str | None = None) -> Iterator[dict]:
        """Yield commits from ``start`` (default HEAD) back to the root."""
        cid = start or self.head()
        seen: set[str] = set()
        while cid and cid not in seen:
            seen.add(cid)
            commit = self.read_commit(cid)
            yield commit
            cid = commit.get("parent")

    def count_commits(self) -> int:
        return sum(1 for _ in self.walk_history())

    # -------------------------------------------------------------- snapshot

    def iter_memory_files(self) -> Iterator[str]:
        """Relative posix paths of memory files, honouring include/exclude."""
        root = self.root / self.config.root
        seen: set[str] = set()
        for pattern in self.config.include:
            for path in root.glob(pattern):
                if not path.is_file():
                    continue
                rel = path.relative_to(self.root).as_posix()
                if rel in seen:
                    continue
                if _is_excluded(rel, self.config.exclude):
                    continue
                seen.add(rel)
                yield rel

    def snapshot(self, *, store: bool = True) -> dict[str, str]:
        """Map path → blob id for every memory file right now."""
        tree: dict[str, str] = {}
        for rel in self.iter_memory_files():
            try:
                data = (self.root / rel).read_bytes()
            except OSError:
                continue
            tree[rel] = (
                self.write_object(data, "blob")
                if store
                else object_id(data, "blob")
            )
        return tree
