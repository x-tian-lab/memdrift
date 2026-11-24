"""One file. No network. No build step. No font downloads.

``memdrift digest`` writes a single self-contained ``.html`` you can double-click,
mail, or drop in a chat. Everything the reader needs is inline: CSS, the tiny bit
of JS for filtering, and the data. That is a constraint, not a limitation — a
thing you send to a colleague must not phone home, because "phoning home" for a
memory digest means telling a third party who your clients are.

Two visual layers, one data source:

* a **day page** — every commit, one card each, changed fields side by side
* a **page index** — which memory pages exist and what moved in them

The HTML uses only system fonts. A webfont would be a network request.
"""

from __future__ import annotations

import html
import json
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

from .events import EventStream, group_by_commit, human_time
from .repo import VERSION

#: Color is meaning, not decoration: red = the agent touched something risky.
RISK_COLOR = "#b4232c"
PATH_COLOR = "#7a5c2e"

CSS = """
:root {
  --ink: #16181d; --muted: #5f6672; --faint: #8b929e;
  --line: #e3e6ea; --bg: #ffffff; --wash: #f6f7f9;
  --risky: #b4232c; --risky-wash: #fdf3f4;
  --ok: #1d6b3f; --accent: #2b4a8b;
  --mono: ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas, monospace;
  --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", Roboto, sans-serif;
}
@media (prefers-color-scheme: dark) {
  :root {
    --ink: #e8eaee; --muted: #a3aab7; --faint: #767d8a;
    --line: #2b3038; --bg: #14171c; --wash: #1b1f25;
    --risky: #ff7b84; --risky-wash: #2a1a1c;
    --ok: #63c48d; --accent: #8aa8e6;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 48px 24px 96px; background: var(--bg); color: var(--ink);
  font-family: var(--sans); font-size: 15px; line-height: 1.55;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 880px; margin: 0 auto; }
header { border-bottom: 1px solid var(--line); padding-bottom: 28px; margin-bottom: 8px; }
h1 { font-size: 27px; margin: 0 0 6px; letter-spacing: -0.02em; font-weight: 650; }
.sub { color: var(--muted); font-size: 14px; margin: 0; }
.sub code { font-family: var(--mono); font-size: 13px; background: var(--wash); padding: 1px 5px; border-radius: 3px; }
.stats { display: flex; flex-wrap: wrap; gap: 28px; margin: 24px 0 0; }
.stat .n { font-size: 22px; font-weight: 650; font-variant-numeric: tabular-nums; }
.stat .k { color: var(--faint); font-size: 11px; text-transform: uppercase; letter-spacing: 0.07em; }
.stat.risky .n { color: var(--risky); }

.toolbar { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin: 28px 0 4px; }
.chip {
  font: inherit; font-size: 13px; padding: 5px 12px; border-radius: 999px; cursor: pointer;
  border: 1px solid var(--line); background: var(--bg); color: var(--muted);
}
.chip[aria-pressed="true"] { background: var(--ink); border-color: var(--ink); color: var(--bg); }
.chip:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

h2 { font-size: 12px; text-transform: uppercase; letter-spacing: 0.09em; color: var(--faint);
     margin: 40px 0 14px; font-weight: 600; }
.commit { border: 1px solid var(--line); border-radius: 10px; margin-bottom: 14px; overflow: hidden; }
.commit.hot { border-color: var(--risky); background: var(--risky-wash); }
.chead {
  display: flex; gap: 12px; align-items: baseline; padding: 13px 16px;
  background: var(--wash); border-bottom: 1px solid var(--line); flex-wrap: wrap;
}
.commit.hot .chead { background: transparent; }
.hash { font-family: var(--mono); font-size: 12px; color: var(--faint); }
.msg { font-weight: 600; font-size: 14px; }
.who { color: var(--muted); font-size: 12.5px; }
.when { color: var(--faint); font-size: 12.5px; margin-left: auto; white-space: nowrap; }
.rows { padding: 4px 0; }
.row { display: grid; grid-template-columns: 220px 1fr; gap: 16px; padding: 9px 16px; align-items: start; }
.row + .row { border-top: 1px solid var(--line); }
.row:hover { background: var(--wash); }
.pt { font-family: var(--mono); font-size: 12.5px; color: var(--muted); word-break: break-all; }
.fd { font-family: var(--mono); font-size: 12px; color: var(--accent); }
.val { font-size: 13.5px; word-break: break-word; }
.old { color: var(--faint); text-decoration: line-through; text-decoration-thickness: 1px; }
.arw { color: var(--faint); margin: 0 7px; }
.new { font-weight: 550; }
.tag {
  display: inline-block; font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.05em;
  padding: 1px 6px; border-radius: 3px; vertical-align: 1px; margin-left: 7px; font-weight: 600;
}
.tag.warn { background: var(--risky); color: #fff; }
.tag.low { background: var(--wash); color: var(--faint); border: 1px solid var(--line); }
.tag.op { background: transparent; color: var(--muted); border: 1px solid var(--line); }
.ev { padding: 0 16px 10px 36px; margin-top: -2px; font-size: 12.5px; color: var(--muted); }
.ev b { font-weight: 600; color: var(--ink); font-family: var(--mono); font-size: 12px; }
.ev .q { display: block; margin-top: 3px; padding-left: 9px; border-left: 2px solid var(--line); font-style: italic; }
.pages { list-style: none; padding: 0; margin: 0; }
.pages li { display: flex; gap: 12px; padding: 7px 0; border-bottom: 1px solid var(--line); font-size: 13.5px; }
.pages .p { font-family: var(--mono); font-size: 12.5px; color: var(--muted); flex: 1; }
.pages .t { font-weight: 550; }
footer { margin-top: 48px; padding-top: 20px; border-top: 1px solid var(--line);
         color: var(--faint); font-size: 12px; }
footer code { font-family: var(--mono); }
.private-note { color: var(--faint); font-size: 12.5px; margin-top: 10px; }
.empty { color: var(--faint); padding: 32px 0; text-align: center; }
[hidden] { display: none !important; }
@media (max-width: 620px) {
  .row { grid-template-columns: 1fr; gap: 4px; }
  .when { margin-left: 0; width: 100%; }
}
"""

JS = """
(function () {
  var chips = document.querySelectorAll('.chip[data-filter]');
  var on = 'all';
  function apply() {
    document.querySelectorAll('.commit').forEach(function (c) {
      var risk = c.dataset.risk === '1';
      c.hidden = on === 'risk' ? !risk : false;
    });
    document.querySelectorAll('.row').forEach(function (r) {
      r.hidden = false;
    });
    if (on === 'field') {
      document.querySelectorAll('.row').forEach(function (r) {
        if (r.dataset.kind !== 'field') r.hidden = true;
      });
    }
    if (on === 'risk') {
      document.querySelectorAll('.row').forEach(function (r) {
        if (r.dataset.risk !== '1') r.hidden = true;
      });
    }
  }
  chips.forEach(function (chip) {
    chip.addEventListener('click', function () {
      on = chip.dataset.filter;
      chips.forEach(function (c) { c.setAttribute('aria-pressed', String(c === chip)); });
      apply();
    });
  });
})();
"""


def _e(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _value(value) -> str:
    if value is None:
        return '<span class="old">∅</span>'
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value)


def _op_label(op: str) -> str:
    return {"add": "created", "delete": "deleted", "rename": "renamed", "modify": ""}.get(op, op)


def _row_html(event: dict) -> str:
    kind = "field" if event.get("field") else "op"
    path = _e(event.get("path"))
    field = event.get("field")
    op = event.get("op") or "modify"
    tags: list[str] = []
    if event.get("risk"):
        tags.append('<span class="tag warn">review</span>')
    if event.get("low_precision"):
        tags.append('<span class="tag low">low precision</span>')
    if op != "modify":
        tags.append(f'<span class="tag op">{_e(_op_label(op))}</span>')

    if field:
        label = f'<div class="fd">{_e(field)}</div>'
        body = (
            f'<span class="old">{_e(_value(event.get("before")))}</span>'
            f'<span class="arw">→</span>'
            f'<span class="new">{_e(_value(event.get("after")))}</span>'
        )
    else:
        label = ""
        if op == "rename":
            body = (
                f'<span class="old">{_e(event.get("from") or "")}</span>'
                f'<span class="arw">→</span><span class="new">{path}</span>'
            )
        elif op == "add":
            refs = event.get("sections") or []
            extra = f' · {len(refs)} section(s)' if refs else ""
            body = f'<span class="new">page created</span><span class="who">{_e(extra)}</span>'
        elif op == "delete":
            body = '<span class="old">page deleted</span>'
        else:
            body = '<span class="new">changed</span>'

    left = f'<div><div class="pt">{path}</div>{label}</div>'
    out = [f'<div class="row" data-kind="{kind}" data-risk="{1 if event.get("risk") else 0}">']
    out.append(left)
    out.append(f'<div class="val">{body} {"".join(tags)}</div>')
    out.append("</div>")
    return "".join(out)


def _evidence_html(items: Iterable[dict]) -> str:
    parts: list[str] = []
    for item in items or []:
        kind = item.get("kind") or "ref"
        ref = item.get("ref") or ""
        quote = item.get("quote")
        piece = f"<b>{_e(kind)}</b> {_e(ref)}"
        if quote:
            piece += f'<span class="q">“{_e(quote)}”</span>'
        elif item.get("quote_omitted"):
            piece += '<span class="q">quote omitted</span>'
        parts.append(piece)
    if not parts:
        return ""
    return f'<div class="ev">why: {" · ".join(parts)}</div>'


def render_digest(stream: EventStream, *, title: str = "memdrift digest", generated: datetime | None = None) -> str:
    generated = generated or datetime.now().astimezone()
    groups = group_by_commit(stream.events)
    risky = stream.risky
    changed_paths = {e.get("path") for e in stream.events if e.get("path")}

    stats = [
        ("commits", str(stream.commits), False),
        ("pages touched", str(len(changed_paths)), False),
        ("field changes", str(len(stream.events)), False),
        ("need review", str(len(risky)), True),
    ]
    stat_html = "".join(
        f'<div class="stat{" risky" if flag else ""}"><div class="n">{n}</div><div class="k">{k}</div></div>'
        for k, n, flag in stats
    )

    cards: list[str] = []
    for group in groups:
        hot = any(e.get("risk") for e in group["events"])
        rows = "".join(_row_html(e) for e in group["events"])
        evidence = _evidence_html(group["events"][0].get("evidence"))
        author = group.get("author") or {}
        unsure = group.get("id") in stream.unsure
        conf = group["events"][0].get("confidence")
        unsure_tag = ""
        if unsure:
            label = f"agent unsure · confidence {conf:.2f}" if isinstance(conf, (int, float)) else "agent unsure"
            unsure_tag = f'<span class="tag low" title="The agent reported low confidence for this whole commit">{_e(label)}</span>'
        cards.append(
            f'<section class="commit{" hot" if hot else ""}" data-risk="{1 if hot else 0}">'
            f'<div class="chead">'
            f'<span class="hash">{_e(group.get("short"))}</span>'
            f'<span class="msg">{_e(group.get("message") or "(no message)")}</span>'
            f'<span class="who">{_e(author.get("name"))} · {_e(author.get("type"))}</span>'
            f'{unsure_tag}'
            f'<span class="when" title="{_e(group.get("ts"))}">{_e(human_time(group.get("ts"), generated))}</span>'
            f"</div><div class=\"rows\">{rows}</div>{evidence}</section>"
        )
    if not cards:
        cards.append('<p class="empty">No changes recorded in this window.</p>')

    page_items = "".join(
        f'<li><span class="p">{_e(p.path)}</span><span class="t">{_e(p.title)}</span></li>'
        for p in stream.pages
    )
    hidden_note = ""
    if stream.hidden:
        hidden_note = (
            f'<p class="private-note">{len(stream.hidden)} page(s) marked '
            f'<code>private: true</code> are excluded from this digest, '
            f'and so is any evidence pointing at them.</p>'
        )

    window = f'{stream.since or "all"} → {stream.until or "now"}'
    payload = json.dumps(
        {"events": len(stream.events), "risky": len(risky), "commits": stream.commits},
        separators=(",", ":"),
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="generator" content="memdrift {_e(VERSION)}">
<meta name="referrer" content="no-referrer">
<title>{_e(title)}</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>{_e(title)}</h1>
    <p class="sub">Generated {_e(generated.strftime("%Y-%m-%d %H:%M %z"))} · window <code>{_e(window)}</code></p>
    <div class="stats">{stat_html}</div>
  </header>

  <div class="toolbar" role="group" aria-label="Filter">
    <button class="chip" data-filter="all" aria-pressed="true">Everything</button>
    <button class="chip" data-filter="field" aria-pressed="false">Field changes only</button>
    <button class="chip" data-filter="risk" aria-pressed="false">Needs review</button>
  </div>

  <h2>Changes</h2>
  {"".join(cards)}

  <h2>Pages tracked ({len(stream.pages)})</h2>
  <ul class="pages">{page_items}</ul>
  {hidden_note}

  <footer>
    <code>memdrift digest</code> · every line above came from a commit file under
    <code>.memgit/commits/</code>; nothing is inferred. Delete <code>.memgit/</code>
    and your memory files are byte-for-byte unchanged.
    <script type="application/json" id="memdrift-summary">{payload}</script>
  </footer>
</div>
<script>{JS}</script>
</body>
</html>
"""


def write_digest(
    stream: EventStream,
    out_path: str | Path,
    *,
    title: str = "memdrift digest",
    generated: datetime | None = None,
) -> Path:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_digest(stream, title=title, generated=generated), encoding="utf-8")
    return path


def default_filename(day: date | None = None) -> str:
    return f"memdrift-digest-{(day or date.today()).isoformat()}.html"
