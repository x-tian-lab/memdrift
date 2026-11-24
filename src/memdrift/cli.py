"""memdrift CLI.

Thin shell. Anything with a decision in it lives in operations.py so the MCP
server and the digest can reuse it without importing click.

Directory handling follows git: pass ``-C <dir>``, or run from inside the
memory directory. Only ``init`` takes a positional path.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import click

from . import operations as ops
from .differ import Change
from .repo import VERSION, Repo, RepoError, short

CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"], "max_content_width": 100}


# --------------------------------------------------------------------- helpers


def _open(directory: str) -> Repo:
    try:
        return Repo.discover(directory)
    except RepoError as exc:
        raise click.ClickException(str(exc)) from exc


def _fmt_ts(ts: str | None) -> str:
    if not ts:
        return "-"
    try:
        return datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ts[:16]


def _dump(obj: Any) -> None:
    click.echo(json.dumps(obj, indent=2, ensure_ascii=False))


def _author_fields(item: dict) -> tuple[str, str]:
    """Works for both a commit json (author: {...}) and an index row (flat cols)."""
    author = item.get("author")
    if isinstance(author, dict):
        return str(author.get("type") or "?"), str(author.get("name") or "?")
    return (
        str(item.get("author_type") or "?"),
        str(item.get("author_name") or "?"),
    )


def _author(item: dict) -> str:
    kind, name = _author_fields(item)
    return f"{kind}:{name}"


def _short_field(field: str | None) -> str:
    if not field:
        return ""
    for prefix in ("frontmatter.", "section:"):
        if field.startswith(prefix):
            return field[len(prefix) :]
    return field


def _clip(value: Any, limit: int = 42) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _arrow(before: Any, after: Any, limit: int = 42) -> str:
    """'$45k → $48k'; a one-sided change just states the new value."""
    b, a = _clip(before, limit), _clip(after, limit)
    if b and a:
        return f"{b} → {a}"
    if a:
        return f"→ {a}"
    if b:
        return f"← {b}"
    return ""


def _inline_change(raw: dict) -> str:
    """One change, rendered without its path (the path is already a header)."""
    op = raw.get("op", "modify")
    label = click.style(_short_field(raw.get("field")), bold=True)

    if op == "rename":
        if raw.get("section_rename"):
            return f"{label}  {click.style('heading renamed', dim=True)} {raw.get('before')} → {raw.get('after')}"
        return click.style(f"renamed from {raw.get('from')}", fg="cyan")
    if op == "add":
        n = len(raw.get("sections") or [])
        return click.style(f"new page · {n} section(s)", fg="green")
    if op == "delete":
        return click.style("page deleted", fg="red")

    arrow = _arrow(raw.get("before"), raw.get("after"))
    flag = click.style(" [low-precision]", fg="yellow") if raw.get("low_precision") else ""
    if not arrow:
        arrow = click.style("cleared", dim=True)
    return f"{label}  {arrow}{flag}"


def _print_changes(changes: list[Any]) -> None:
    current = None
    for change in changes:
        raw = change.to_dict() if hasattr(change, "to_dict") else change
        if raw["path"] != current:
            current = raw["path"]
            color = {"add": "green", "delete": "red", "rename": "cyan"}.get(
                raw.get("op"), "white"
            )
            click.echo(click.style(current, fg=color, bold=True))
        click.echo("  " + _inline_change(raw))


# ------------------------------------------------------------------------ CLI


@click.group(context_settings=CONTEXT_SETTINGS)
@click.version_option(VERSION, prog_name="memdrift")
@click.option(
    "-C",
    "--dir",
    "directory",
    default=".",
    type=click.Path(file_okay=False),
    help="Memory directory (defaults to the current one).",
)
@click.pass_context
def main(ctx: click.Context, directory: str) -> None:
    """Version control for agent memory.

    \b
    memdrift init ~/.basic-memory
    memdrift -C ~/.basic-memory log --since 7d
    memdrift -C ~/.basic-memory blame clients/acme.md
    """
    ctx.ensure_object(dict)
    ctx.obj["directory"] = directory


@main.command()
@click.argument("directory", default=".", type=click.Path(file_okay=False))
@click.option(
    "--author-type",
    type=click.Choice(["agent", "human"]),
    default="agent",
    show_default=True,
    help="Who writes to this memory dir by default.",
)
@click.option("--author-name", default=None, help="e.g. basic-memory, linger-dreaming")
@click.option("--no-commit", is_flag=True, help="Only create .memgit/, no baseline commit.")
def init(directory: str, author_type: str, author_name: str | None, no_commit: bool) -> None:
    """Initialize version control for a memory directory."""
    root = Path(directory).resolve()
    if (root / ".memgit").is_dir():
        click.echo(f"{click.style('already initialized', fg='yellow')}: {root}")
        return

    repo = Repo.init(root, author_type=author_type, author_name=author_name or "memdrift")
    click.echo(f"{click.style('initialized', fg='green')} {root}")

    files = list(repo.iter_memory_files())
    if no_commit or not files:
        click.echo(click.style(f"no baseline commit ({len(files)} memory file(s))", dim=True))
        return

    result = ops.commit(repo, "initial snapshot", author_type="human", author_name="you")
    if result:
        click.echo(
            f"{click.style('baseline', fg='green')} {short(result['id'])} · "
            f"{len(files)} file(s)"
        )


@main.command()
@click.option("-m", "--message", required=True, help="Why this change happened.")
@click.option(
    "--evidence",
    multiple=True,
    metavar="KIND:REF",
    help="email:msg-1882 / file:note.md / https://… (repeatable)",
)
@click.option("--confidence", type=float, default=None, help="0.0–1.0, the agent's own certainty.")
@click.option("--author-name", default=None)
@click.option("--author-type", type=click.Choice(["agent", "human"]), default=None)
@click.option("--json", "as_json", is_flag=True)
@click.pass_obj
def commit(
    obj: dict,
    message: str,
    evidence: tuple[str, ...],
    confidence: float | None,
    author_name: str | None,
    author_type: str | None,
    as_json: bool,
) -> None:
    """Snapshot the directory and record a commit."""
    repo = _open(obj["directory"])
    result = ops.commit(
        repo,
        message,
        evidence=list(evidence),
        author_type=author_type,
        author_name=author_name,
        confidence=confidence,
    )
    if result is None:
        click.echo(click.style("nothing to commit, memory unchanged", dim=True))
        return
    if as_json:
        _dump(result)
        return

    count = len(result["changes"])
    click.echo(
        f"{click.style(short(result['id']), fg='yellow')} "
        f"{result['message']} · {count} change(s)"
    )
    shown = result["changes"][:20]
    current = None
    for raw in shown:
        if raw["path"] != current:
            current = raw["path"]
            click.echo("  " + click.style(current, bold=True))
        click.echo("    " + _inline_change(raw))
    if count > 20:
        click.echo(click.style(f"  … {count - 20} more", dim=True))


@main.command(name="log")
@click.option("--page", default=None, help="Only commits touching this memory page.")
@click.option("--since", default=None, help="7d / 36h / 2026-11-20")
@click.option("--limit", type=int, default=None)
@click.option("--json", "as_json", is_flag=True)
@click.pass_obj
def log_cmd(obj: dict, page: str | None, since: str | None, limit: int | None, as_json: bool) -> None:
    """History, newest first."""
    repo = _open(obj["directory"])
    try:
        commits = ops.log(repo, page=page, since=since, limit=limit)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    if as_json:
        _dump(commits)
        return
    if not commits:
        click.echo(click.style("no commits yet", dim=True))
        return

    for item in commits:
        author = _author(item)
        agent = _author_fields(item)[0] == "agent"
        n = len(item.get("changes") or [])
        click.echo(
            f"{click.style(short(item['id']), fg='yellow')} "
            f"{_fmt_ts(item.get('ts'))}  "
            f"{click.style(author, fg='cyan' if agent else 'white')}  "
            f"{item.get('message', '')}  {click.style(f'{n} change(s)', dim=True)}"
        )


@main.command(name="diff")
@click.argument("left", required=False)
@click.argument("right", required=False)
@click.option("--json", "as_json", is_flag=True)
@click.pass_obj
def diff_cmd(obj: dict, left: str | None, right: str | None, as_json: bool) -> None:
    """Structured diff.

    \b
    memdrift diff              working tree vs HEAD
    memdrift diff <commit>     working tree vs that commit
    memdrift diff <a> <b>      what changed between two snapshots
    """
    if right and not left:
        raise click.ClickException("give the older snapshot first: memdrift diff <a> <b>")

    repo = _open(obj["directory"])
    try:
        changes = ops.diff_commits(repo, left, right)
    except RepoError as exc:
        raise click.ClickException(str(exc)) from exc

    if as_json:
        _dump([c.to_dict() for c in changes])
        return
    if not changes:
        click.echo(click.style("no changes", dim=True))
        return
    _print_changes(changes)


@main.command(name="show")
@click.argument("commit", required=False, default="HEAD")
@click.option("--json", "as_json", is_flag=True)
@click.pass_obj
def show_cmd(obj: dict, commit: str, as_json: bool) -> None:
    """What one commit changed: message, author, confidence, evidence."""
    repo = _open(obj["directory"])
    try:
        cid = repo.resolve(commit)
        if not cid:
            raise click.ClickException("no commits yet")
        payload = repo.read_commit(cid)
    except RepoError as exc:
        raise click.ClickException(str(exc)) from exc

    if as_json:
        _dump(payload)
        return

    click.echo(
        f"{click.style(short(payload['id']), fg='yellow')} "
        f"{click.style(payload.get('message', ''), bold=True)}"
    )
    conf = payload.get("confidence")
    conf_txt = f" · conf {conf:.2f}" if isinstance(conf, (int, float)) else ""
    click.echo(
        click.style(
            f"{_fmt_ts(payload.get('ts'))} · {_author(payload)}"
            + (f" · parent {short(payload.get('parent'))}" if payload.get("parent") else " · root")
            + conf_txt,
            dim=True,
        )
    )
    evidence = payload.get("evidence") or []
    if evidence:
        click.echo(click.style("evidence", fg="cyan"))
        for ev in evidence:
            quote = f'  "{ev["quote"]}"' if ev.get("quote") else ""
            when = f"  {ev['ts']}" if ev.get("ts") else ""
            click.echo(f"  <- {ev.get('kind')}:{ev.get('ref')}{when}{quote}")

    changes = [Change.from_dict(c) for c in payload.get("changes") or []]
    if changes:
        click.echo()
        _print_changes(changes)


@main.command()
@click.argument("page")
@click.option("--json", "as_json", is_flag=True)
@click.pass_obj
def blame(obj: dict, page: str, as_json: bool) -> None:
    """For each field of a page: which commit last changed it.

    This is the answer to "where did you get that from".
    """
    repo = _open(obj["directory"])
    target = page.replace("\\", "/")
    while target.startswith("./"):
        target = target[2:]
    known = set(repo.iter_memory_files())

    if target not in known:
        matches = [p for p in known if p.endswith("/" + target) or Path(p).name == target]
        if len(matches) == 1:
            target = matches[0]
        elif not matches:
            raise click.ClickException(f"no such page: {page}")
        else:
            raise click.ClickException(
                "ambiguous page, pick one:\n  " + "\n  ".join(sorted(matches))
            )

    entries = ops.blame(repo, target)
    if as_json:
        _dump({"path": target, "fields": entries})
        return
    if not entries:
        click.echo(click.style(f"{target}: no recorded changes", dim=True))
        return

    click.echo(f"{click.style(target, bold=True)}  {len(entries)} field(s)\n")
    for entry in entries:
        click.echo(f"  {click.style(_short_field(entry['field']), bold=True)}")
        before, after = entry.get("before"), entry.get("after")
        arrow = _arrow(before, after, 48)
        if arrow:
            click.echo(f"    {arrow}")
        source = [
            click.style(entry["commit"], fg="yellow"),
            _fmt_ts(entry.get("ts")),
            str(entry.get("author") or "?"),
        ]
        conf = entry.get("confidence")
        if isinstance(conf, (int, float)):
            source.append(f"conf {conf:.2f}")
        evidence = entry.get("evidence") or []
        if evidence:
            first = evidence[0]
            source.append(click.style(f"<- {first.get('kind')}:{first.get('ref')}", fg="cyan"))
        click.echo("    " + click.style(" · ", fg="black", dim=True).join(source))


@main.command()
@click.option("--since", default=None, help="Window, e.g. 1d / 36h / 2026-11-20.")
@click.option("--until", default=None, help="Upper bound, same formats as --since.")
@click.option("--page", default=None, help="Only this path.")
@click.option("-o", "--out", default=None, help="Output file (default: memdrift-digest-<date>.html).")
@click.option("--title", default=None, help="Heading of the generated page.")
@click.option(
    "--no-quote",
    is_flag=True,
    help="Drop verbatim evidence quotes (keeps the reference, hides the text).",
)
@click.option(
    "--no-redact",
    is_flag=True,
    help="DANGER: keep absolute paths. Only for a digest that never leaves this machine.",
)
@click.pass_obj
def digest(
    obj: dict,
    since: str | None,
    until: str | None,
    page: str | None,
    out: str | None,
    title: str | None,
    no_quote: bool,
    no_redact: bool,
) -> None:
    """Write a single self-contained HTML report you can send to someone."""
    from . import digest as digest_mod
    from . import events as events_mod
    from .security import Redactor

    repo = _open(obj["directory"])
    if no_redact:
        click.secho(
            "warning: --no-redact keeps absolute paths and usernames in the output. "
            "Do not send this file to anyone.",
            fg="red",
            err=True,
        )
    redactor = Redactor(root=str(repo.root), keep_quotes=not no_quote)
    if no_redact:
        redactor.enabled = False
    try:
        stream = events_mod.build(repo, since=since, until=until, page=page, redactor=redactor)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    target = Path(out) if out else Path.cwd() / digest_mod.default_filename()
    digest_mod.write_digest(
        stream,
        target,
        title=title or f"memdrift · {repo.root.name}",
    )
    risky = len(stream.risky)
    pending = len(events_mod.working_tree(repo))
    click.echo(
        f"{click.style('wrote', fg='green')} {target}  "
        + click.style(
            f"{stream.commits} commit(s) · {len(stream.events)} field change(s)", fg="black", dim=True
        )
    )
    if risky:
        click.echo(
            "  "
            + click.style(f"{risky} change(s) need review", fg="red")
            + click.style(" — deletes or wholesale rewrites", fg="black", dim=True)
        )
    elif stream.events:
        click.echo("  " + click.style("nothing destructive in this window", fg="black", dim=True))
    if pending:
        click.echo(
            "  " + click.style(f"{pending} uncommitted change(s) not included", fg="yellow")
        )
    if stream.hidden:
        click.echo(
            click.style(
                f"  {len(stream.hidden)} private page(s) excluded, with their evidence",
                fg="black",
                dim=True,
            )
        )


@main.command()
@click.option("--port", type=int, default=7787, help="Port on 127.0.0.1.")
@click.option("--host", default="127.0.0.1", show_default=True, help="Loopback only.")
@click.option("--open", "open_browser", is_flag=True, help="Open the URL in a browser.")
@click.option("--verbose", is_flag=True, help="Log each request.")
@click.pass_obj
def serve(obj: dict, port: int, host: str, open_browser: bool, verbose: bool) -> None:
    """Serve a read-only local UI (loopback only, token in the URL)."""
    import threading
    import webbrowser

    from . import server as server_mod

    repo = _open(obj["directory"])
    if port == 0:
        port = 7787
    try:
        server_mod.check_host(host)
    except server_mod.LoopbackOnly as exc:
        raise click.ClickException(str(exc)) from exc

    ready = threading.Event()
    holder: dict = {}

    def run() -> None:
        try:
            srv = server_mod.make_server(repo, host=host, port=port, verbose=verbose)
        except OSError as exc:
            holder["error"] = exc
            ready.set()
            return
        holder["server"] = srv
        ready.set()
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            srv.server_close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    ready.wait(5)

    if "error" in holder:
        raise click.ClickException(f"cannot listen on {host}:{port}: {holder['error']}")

    srv = holder["server"]
    url = server_mod.url_for(srv)
    click.echo(f"{click.style('serving', fg='green')} {url}")
    click.echo(
        click.style(
            "  loopback only · the token in the URL is required · Ctrl+C to stop",
            fg="black",
            dim=True,
        )
    )
    if open_browser:
        webbrowser.open(url)
    try:
        while thread.is_alive():
            thread.join(0.5)
    except KeyboardInterrupt:
        srv.shutdown()


@main.command()
@click.option("--json", "as_json", is_flag=True)
@click.pass_obj
def status(obj: dict, as_json: bool) -> None:
    """Uncommitted changes relative to the last commit."""
    repo = _open(obj["directory"])
    report = ops.status(repo)
    if as_json:
        _dump(
            {
                "clean": report.clean,
                "added": report.added,
                "modified": report.modified,
                "deleted": report.deleted,
                "renamed": [{"from": a, "to": b} for a, b in report.renamed],
            }
        )
        return
    if report.clean:
        click.echo(click.style("clean — memory matches the last commit", fg="green"))
        return
    for old, new in report.renamed:
        click.echo(f"  {click.style('renamed', fg='cyan')} {old} → {new}")
    for path in report.added:
        click.echo(f"  {click.style('+', fg='green')} {path}")
    for path in report.modified:
        click.echo(f"  {click.style('~', fg='yellow')} {path}")
    for path in report.deleted:
        click.echo(f"  {click.style('-', fg='red')} {path}")


@main.command()
@click.option("--field", "field_name", default=None, help="Field to restore, e.g. frontmatter.pricing.")
@click.option("--page", default=None, help="Page to restore (needs --field, or --commit).")
@click.option("--commit", "commit_ref", default=None, help="Restore to this commit / undo this commit.")
@click.option("--dry-run", is_flag=True, help="Show the plan; write nothing.")
@click.option("--author", default=None, help="Name to record on the revert commit.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_obj
def revert(
    obj: dict,
    field_name: str | None,
    page: str | None,
    commit_ref: str | None,
    dry_run: bool,
    author: str | None,
    as_json: bool,
) -> None:
    """Undo a change by committing its inverse. History is never rewritten.

    \b
    memdrift revert --field frontmatter.pricing --page clients/acme.md
    memdrift revert --page clients/acme.md --commit 9f2c1ab
    memdrift revert --commit 9f2c1ab
    """
    from . import revert as revert_mod

    repo = _open(obj["directory"])
    try:
        plans = revert_mod.plan(repo, field_name=field_name, page=page, commit_ref=commit_ref)
        result = revert_mod.execute(repo, plans, dry_run=dry_run, author_name=author)
    except RepoError as exc:
        raise click.ClickException(str(exc)) from exc

    if as_json:
        _dump(
            {
                "dry_run": dry_run,
                "plans": [
                    {
                        "path": p.path,
                        "field": p.field,
                        "before": p.before,
                        "after": p.after,
                        "note": p.note,
                        "skipped": p.skipped,
                        "reason": p.reason,
                    }
                    for p in plans
                ],
                "commit": result.commit["id"] if result.commit else None,
                "written": result.written,
            }
        )
        return

    for item in plans:
        if item.skipped:
            click.echo("  " + click.style(revert_mod.describe(item), fg="yellow"))
        else:
            click.echo("  " + revert_mod.describe(item))

    if all(p.skipped for p in plans):
        return
    if dry_run:
        click.echo(click.style("dry run — nothing written", fg="black", dim=True))
        return
    if not result.written:
        click.echo(click.style("nothing changed on disk", fg="yellow"))
        return
    click.echo(
        click.style(f"reverted {len(result.written)} item(s)", fg="green")
        + (" as " + short(result.commit["id"]) if result.commit else "")
    )


@main.command()
@click.option("--no-quote", is_flag=True, help="Never return verbatim evidence quotes.")
@click.option("--print-config", is_flag=True, help="Print the JSON block to paste into a client.")
@click.pass_obj
def mcp(obj: dict, no_quote: bool, print_config: bool) -> None:
    """Serve this memory dir over MCP (stdio), for Cursor / Claude / Zed."""
    import json as _json

    from .mcp import build_server

    # Not `_open`: it refuses a directory without `.memgit/`, and a client that
    # launches this at startup must not die on a vault the human has not
    # initialised yet. `init` stays a deliberate human act; the server just
    # answers every read with "nothing yet" until it happens.
    repo = Repo(Path(obj["directory"]).expanduser().resolve())

    if print_config:
        # The command a client needs depends on how memdrift was installed.
        # `sys.executable -m memdrift` works for a pipx/venv install *and* a
        # source checkout, which a bare `memdrift` on PATH does not always.
        import sys

        entry = {
            "mcpServers": {
                "memdrift": {
                    "command": sys.executable,
                    "args": ["-m", "memdrift", "-C", str(repo.root), "mcp"],
                }
            }
        }
        click.echo(_json.dumps(entry, indent=2, ensure_ascii=False))
        return

    server = build_server(repo, keep_quotes=not no_quote)
    click.echo(
        click.style("memdrift mcp", fg="green")
        + click.style(f" · {repo.root} · stdio · Ctrl+C to stop", fg="black", dim=True),
        err=True,
    )
    # stdout is the wire: anything human-readable goes to stderr or corrupts
    # the stream. click.echo above is already routed to stderr for that reason.
    raise SystemExit(server.serve())


@main.command(name="index")
@click.option("--rebuild", is_flag=True, help="Rebuild index.sqlite from commit files.")
@click.pass_obj
def index_cmd(obj: dict, rebuild: bool) -> None:
    """Inspect or rebuild the query index."""
    repo = _open(obj["directory"])
    if not rebuild:
        index = ops.open_index(repo)
        try:
            click.echo(f"indexed commits: {index.count_commits()}")
        finally:
            index.close()
        return
    n = ops.rebuild_index(repo)
    click.echo(f"{click.style('rebuilt', fg='green')} {n} commit(s) indexed")


@main.command()
@click.option("--debounce", type=float, default=None, help="Seconds to wait before committing.")
@click.option("--once", is_flag=True, help="Commit current state and exit (no watching).")
@click.pass_obj
def watch(obj: dict, debounce: float | None, once: bool) -> None:
    """Watch a memory directory and commit every burst of writes."""
    from .watcher import run as run_watcher

    repo = _open(obj["directory"])
    run_watcher(repo, debounce=debounce, once=once)
