"""Which directories this install may touch, and in what mode.

The rule this module exists to enforce, in one sentence: **a plain install can
read the directory it was started in, and nothing else; writing anywhere —
committing, undoing, running the web app — needs a space that was declared on
purpose.**

That is deliberately not a setting:

* the list lives in the operator's home (``~/.memdrift/spaces.json``), never
  inside a vault. A vault you clone or receive must not be able to widen its
  own scope by shipping a file;
* there is no "allow everything" flag and no environment variable that turns
  the check off. To reach another directory you name it, and naming it is
  visible in ``memdrift space list``;
* an entry we cannot parse is dropped, which fails closed;
* the filesystem root and bare drive roots are refused, because allowing one
  is the same as allowing everything.

``memdrift init`` is the one command that declares a space as writable, since
creating the store *is* the declaration. Everything else has to be added with
``memdrift space allow <path> --write``.
"""

from __future__ import annotations

import getpass
import json
import os
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

MODES = ("read", "write")
REGISTRY_NAME = "spaces.json"
VERSION = 1


class OutOfScope(RuntimeError):
    """The repository is outside every space this install may touch.

    Carries the message the human needs to read, because a refusal that does
    not say how to proceed gets worked around by turning the check off.
    """


def _home() -> Path:
    """Where this install keeps its state.

    ``MEMDRIFT_HOME`` exists for tests and for people who keep their config
    outside ``$HOME``. It moves *the list*, it never disables it.
    """
    raw = os.environ.get("MEMDRIFT_HOME")
    if raw:
        return Path(raw).expanduser().resolve()
    return Path.home() / ".memdrift"


def registry_path() -> Path:
    return _home() / REGISTRY_NAME


def _norm(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _is_root(path: Path) -> bool:
    """``/`` or ``C:\\`` — allowing one of these allows the whole machine."""
    try:
        return path.parent == path
    except OSError:  # a drive that is not mounted is not a root we care about
        return True


def _who() -> str:
    try:
        return getpass.getuser()
    except Exception:  # no HOME, no passwd entry, locked-down container
        return "unknown"


def _stamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True)
class Space:
    """One allowed directory. ``write`` implies ``read``."""

    path: str
    mode: str = "read"
    added_by: str = ""
    added_at: str = ""

    @property
    def root(self) -> Path:
        return Path(self.path)

    def covers(self, target: str | Path) -> bool:
        """Is *target* this space, or inside it?"""
        try:
            here = _norm(target)
        except (OSError, ValueError):
            return False
        return here == self.root or self.root in here.parents

    def allows_write(self) -> bool:
        return self.mode == "write"

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "mode": self.mode,
            "added_by": self.added_by,
            "added_at": self.added_at,
        }


def _parse(raw: object) -> Space | None:
    """One registry entry, or None when it cannot be trusted.

    Dropping a bad entry denies access to that path; keeping it would grant
    access on the strength of a typo. Deny is the safe failure.
    """
    if not isinstance(raw, dict):
        return None
    path, mode = raw.get("path"), raw.get("mode", "read")
    if not isinstance(path, str) or not path or mode not in MODES:
        return None
    try:
        resolved = _norm(path)
    except (OSError, ValueError):
        return None
    if _is_root(resolved):
        return None
    return Space(
        path=str(resolved),
        mode=mode,
        added_by=str(raw.get("added_by") or ""),
        added_at=str(raw.get("added_at") or ""),
    )


def load() -> list[Space]:
    """The declared spaces, newest last. Missing file → empty list."""
    path = registry_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    out = []
    for raw in data.get("spaces") or []:
        space = _parse(raw)
        if space and not any(s.path == space.path for s in out):
            out.append(space)
    return out


def _save(spaces: list[Space]) -> None:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": VERSION,
        "spaces": [s.to_dict() for s in spaces],
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:  # Windows: advisory at best, and not worth failing on
        pass
    os.replace(tmp, path)


def allow(path: str | Path, *, mode: str = "read", by: str | None = None) -> Space:
    """Declare a directory. Idempotent; re-allowing upgrades read → write."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, not {mode!r}")
    target = _norm(path)
    if not target.is_dir():
        raise ValueError(f"not a directory: {target}")
    if _is_root(target):
        # `/` would cover every repository on the machine; that is not a space,
        # that is turning the check off with extra steps.
        raise ValueError("refusing to allow a filesystem root — name the directory")

    spaces = load()
    for index, existing in enumerate(spaces):
        if existing.path != str(target):
            continue
        if existing.mode == mode:
            return existing
        upgraded = replace(existing, mode=mode, added_at=_stamp())
        spaces[index] = upgraded
        _save(spaces)
        return upgraded
    space = Space(path=str(target), mode=mode, added_by=by or _who(), added_at=_stamp())
    spaces.append(space)
    _save(spaces)
    return space


def deny(path: str | Path) -> bool:
    """Forget a directory. Returns whether anything was removed."""
    target = str(_norm(path))
    spaces = load()
    kept = [s for s in spaces if s.path != target]
    if len(kept) == len(spaces):
        return False
    _save(kept)
    return True


def covering(target: str | Path) -> list[Space]:
    """Spaces that cover *target*, widest reach last."""
    return [s for s in load() if s.covers(target)]


def writable(target: str | Path) -> bool:
    return any(s.allows_write() and s.covers(target) for s in load())


def _implicit(cwd: Path | None) -> list[Space]:
    """What is readable when no registry exists yet.

    A fresh install gets exactly the directory it was started in — the one
    the human typed the command in. Not its parent, not its children's
    parents: the working directory, resolved.
    """
    here = _norm(cwd or Path.cwd())
    return [Space(path=str(here), mode="read", added_by="(implicit)", added_at="")]


def spaces_for(cwd: str | Path | None = None) -> list[Space]:
    """Declared spaces, or the implicit working directory when there is none.

    The implicit rule applies only while *no registry exists at all*. Once the
    file is there, the file is the truth — including when it is empty, because
    "deny everything" has to be expressible. Falling back to the working
    directory after someone emptied the list would silently undo that.
    """
    declared = load()
    if declared or registry_path().exists():
        return declared
    return _implicit(Path(cwd) if cwd else None)


def check(target: str | Path, *, write: bool = False, cwd: str | Path | None = None) -> None:
    """Raise :class:`OutOfScope` unless *target* may be read (or written)."""
    spaces = spaces_for(cwd)
    covered = [s for s in spaces if s.covers(target)]
    if not covered:
        raise OutOfScope(_outside(target, spaces, write))
    if write and not any(s.allows_write() for s in covered):
        raise OutOfScope(_read_only(target, covered))


def allows(target: str | Path, *, write: bool = False, cwd: str | Path | None = None) -> bool:
    try:
        check(target, write=write, cwd=cwd)
    except OutOfScope:
        return False
    return True


def _outside(target: str | Path, spaces: list[Space], write: bool) -> str:
    shown = ", ".join(s.path for s in spaces[:3]) or "(none)"
    more = "" if len(spaces) <= 3 else f" +{len(spaces) - 3} more"
    return (
        f"refused: {_norm(target)} is outside your allowed spaces\n"
        f"  allowed: {shown}{more}\n"
        f"  add it:  memdrift space allow {_norm(target)}"
        + (" --write" if write else "")
    )


def _read_only(target: str | Path, covered: list[Space]) -> str:
    names = ", ".join(s.path for s in covered)
    return (
        f"refused: {_norm(target)} is read-only for this install\n"
        f"  you may read: {names}\n"
        f"  allow writes:  memdrift space allow {_norm(target)} --write"
    )
