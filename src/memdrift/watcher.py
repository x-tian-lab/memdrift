"""Watch a memory directory and commit every burst of writes.

The PRD's requirement is "5s debounce merges bursty writes". A nightly agent
like Linger rewrites 30 pages in two seconds; we want one commit, not thirty.
"""

from __future__ import annotations

import threading
from pathlib import Path

import click
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from . import operations as ops
from .repo import META_DIR, Repo, short


class _Debouncer:
    """Trailing-edge timer: every new event restarts the countdown."""

    def __init__(self, repo: Repo, seconds: float) -> None:
        self.repo = repo
        self.seconds = max(0.2, seconds)
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self.pending: set[str] = set()
        self.stop = threading.Event()

    def poke(self, path: str) -> None:
        with self._lock:
            self.pending.add(path)
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(self.seconds, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        with self._lock:
            touched = len(self.pending)
            self.pending.clear()
            self._timer = None
        if self.stop.is_set():
            return
        result = ops.commit(
            self.repo,
            f"auto: {touched} file(s) touched",
            author_type=self.repo.config.author_type,
            author_name=self.repo.config.author_name,
        )
        if result:
            click.echo(
                f"{click.style('committed', fg='green')} {short(result['id'])} · "
                f"{len(result['changes'])} change(s) from {touched} touched file(s)"
            )
        else:
            click.echo(click.style(f"settled, no net change ({touched} touched)", dim=True))

    def flush(self) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
        if self.pending:
            self._fire()


class _Handler(FileSystemEventHandler):
    def __init__(self, debouncer: _Debouncer, root: Path) -> None:
        self.debouncer = debouncer
        self.root = root

    def _interesting(self, raw_path: str) -> str | None:
        try:
            rel = Path(raw_path).resolve().relative_to(self.root).as_posix()
        except (ValueError, OSError):
            return None
        if rel.startswith(META_DIR + "/") or rel == META_DIR:
            return None
        return rel

    def on_any_event(self, event) -> None:
        if event.event_type in ("opened", "closed_no_write"):
            return
        for raw in (getattr(event, "src_path", None), getattr(event, "dest_path", None)):
            if not raw:
                continue
            rel = self._interesting(raw)
            if rel:
                self.debouncer.poke(rel)


def run(repo: Repo, debounce: float | None = None, once: bool = False) -> None:
    seconds = debounce if debounce is not None else repo.config.debounce_ms / 1000.0

    if once:
        result = ops.commit(
            repo,
            "manual snapshot",
            author_type=repo.config.author_type,
            author_name=repo.config.author_name,
        )
        if result:
            click.echo(
                f"{click.style('committed', fg='green')} {short(result['id'])} · "
                f"{len(result['changes'])} change(s)"
            )
        else:
            click.echo(click.style("nothing to commit", dim=True))
        return

    debouncer = _Debouncer(repo, seconds)
    handler = _Handler(debouncer, repo.root)
    observer = Observer()
    observer.schedule(handler, str(repo.root), recursive=True)
    observer.start()

    click.echo(
        f"{click.style('watching', fg='green')} {repo.root}  "
        + click.style(f"(debounce {seconds:g}s, Ctrl+C to stop)", dim=True)
    )
    try:
        while True:
            observer.join(timeout=0.5)
    except KeyboardInterrupt:
        click.echo(click.style("\nstopping…", dim=True))
    finally:
        observer.stop()
        observer.join(timeout=5)
        debouncer.stop.set()
        debouncer.flush()
