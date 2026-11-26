# memdrift

**Version control for agent memory.** See what your agent changed about you — field by field, with the evidence — and roll it back.

Your agent edits a dozen memory files overnight. You wake up and have no idea what moved. memdrift watches the directory, and tells you *the client's quote went from $45k to $48k, because of this email* — not "line 7 changed".

```bash
memdrift init ~/.basic-memory
memdrift -C ~/.basic-memory watch        # every burst of writes becomes one commit

# next morning
memdrift -C ~/.basic-memory log --since 1d
memdrift -C ~/.basic-memory blame clients/acme.md
```

This is a nightly consolidation, recorded:

![A commit with five structured changes: a rename, a new page, a price going from $45k to $48k, and a status flipping to on-hold](docs/assets/02-commit.png)

And this is asking where the new price came from:

![blame output per field: pricing $45k → $48k, attributed to the nightly commit with confidence 0.82 and evidence email:msg-1882](docs/assets/03-log-blame.png)

## The problem

Agent memory moved to files: `basic-memory`, `iwe`, Linger's wiki, Obsidian vaults, `CLAUDE.md`. That fixed *writing* — you can read and edit what the agent remembers. It did not fix *changing*:

| Gap | What it looks like |
|---|---|
| **Invisible** | Linger rewrites twelve wiki pages at 00:00. You wake up with no idea what moved. |
| **Unauditable** | The agent says "you told me $48k". You cannot check which message that came from. |
| **Irreversible** | One bad extraction poisons the page. Your only tool is a manual delete. |
| **Per-system** | `CLAUDE.md`, Cursor rules and your vault all accumulate facts about you, and disagree. |

`git diff` does not help. It tells you line 42 changed; it cannot tell you *the client's quote went from $45k to $48k*. memdrift stores changes as **structured fields**, not text diffs.

Add `--json` to any command to get the same information as data:

```json
{
  "path": "clients/acme.md",
  "op": "modify",
  "field": "frontmatter.pricing",
  "before": "$45k",
  "after": "$48k",
  "section": null,
  "change_ratio": 0.125
}
```

Renaming a heading is one change, not a delete plus an add:

![memdrift diff showing a single heading rename from 11/21 to 11/22](docs/assets/04-diff.png)

## What it is

A layer that sits **beside** your memory directory and only records how it changed.

| Layer | Who owns it | memdrift |
|---|---|---|
| Fact extraction | Hindsight / mem0 | not our job |
| Storage & retrieval | basic-memory / iwe / any vault | **untouched** |
| Change history | — | **here** |
| Presentation | your product's UI | CLI, digest HTML, local UI |

**Not** a memory system, not a vector store, not a wiki editor, not a replacement for git.

### Where you actually look at it

A terminal is a bad place to read your own biography. Two views, one data source:

**`memdrift digest`** writes a single `.html` — inline CSS, no fonts to download, no analytics, no `http://` anywhere. Double-click it, or mail it. It is safe to send because sending it is *the point*:

![memdrift digest writing ~/vault/digest.html: 2 commits, 10 field changes, nothing destructive](docs/assets/08-digest.png)

**`memdrift serve`** opens a read-only local UI when you want to dig around instead of read a report.

**`memdrift ui`** is the same three views with the two things a report cannot do: undo one field and commit the working tree. It never acts on the first click — the page fetches a plan, shows the before/after, and only then offers a confirm button, which sends back a fingerprint of exactly the plan you were shown. If the file moved in between, the write is refused with a 409 and you review again.

```bash
memdrift -C ~/vault digest --since 1d -o morning.html
memdrift -C ~/vault serve --open
memdrift -C ~/vault ui --open          # writes; own port, own locks
```

All three read the same redacted event stream, so they cannot disagree, and none of them can forget to redact. `serve` stays read-only on purpose: the writable surface is a separate command with a separate server, so a read-only view can never grow a write path by accident.

![memdrift ui: a timeline of commits on the left, the selected field's before/after, evidence and confidence on the right](docs/assets/11-ui.png)

Clicking 撤销这一处 writes nothing. It fetches the plan first and puts the whole thing in front of you — what will change, what it will be restored to, and the fact that history is not rewritten:

![the confirm box: 会新增一个 commit，历史不改写 … 恢复为 $45k](docs/assets/12-ui-undo.png)

### And from inside your editor

Your agent already reads that memory directory. `memdrift mcp` puts the *history* next to it, so the agent can answer "did you change my prices?" with a commit id instead of a guess:

![memdrift mcp --print-config, then a real stdio session listing 7 tools and calling memory.history](docs/assets/10-mcp.png)

Seven tools, named so a model can tell them apart:

| Tool | | What it answers |
|---|---|---|
| `memory.history` | read | what happened recently, optionally for one page |
| `memory.blame` | read | which commit last touched each field, and the evidence behind it |
| `memory.diff` | read | field-level diff between two commits |
| `memory.page` | read | a page as it stood at a given commit |
| `memory.digest` | read | a window of changes as structured events, with risk flags |
| `memory.commit` | **write** | snapshot the working tree, with a required message |
| `memory.revert` | **write** | undo by committing the inverse |

The read tools carry `readOnlyHint: true`, so a client that honours annotations runs them silently and stops to ask before the two writers — no client-side code on your part. The names are dotted (`memory.history`) because that is what reads as one family to a model; some providers reject dots in name fields, so those get the flattened `memory_history` form on the wire only.

Wire it up:

```bash
memdrift -C ~/vault mcp --print-config   # paste the JSON into your client
memdrift -C ~/vault mcp                  # or run the server yourself
```

stdout is the protocol, so the server prints nothing else there — the banner goes to stderr. Both newline-delimited JSON and `Content-Length` framing are accepted, because clients disagree about which is "the" transport.

## Undo, without rewriting history

The failure this project exists for: an agent ingests a bad email, and `pricing: "$45k"` becomes `"$12k"`. Three granularities, all of them **append-only** — a revert is a new commit that undoes an old one, never an edit of the past:

```bash
memdrift -C ~/vault revert --field frontmatter.pricing --page clients/acme.md
memdrift -C ~/vault revert --page clients/acme.md --commit 9f2c1ab
memdrift -C ~/vault revert --commit 9f2c1ab          # undo everything that commit did
```

![revert --dry-run shows the plan, the real run applies it, log shows the bad commit still present](docs/assets/09-revert.png)

Look at the last three lines of that image: `revert:` is commit three, `extract: pricing dropped` is still commit two, unchanged. `blame` still points at the commit that introduced the bad value — it now *also* points at the commit that took it back out. Rewriting would make the log lie, which is the one thing a tool for "what did my agent do to me" cannot afford.

Two details that took a bug each to get right:

- **YAML quoting survives.** Restoring `"$48k"` writes `"$48k"`, not `$48k`. Both parse the same, but only one keeps a one-field revert from showing up as a whole-file diff.
- **Line endings survive.** On Windows, `Path.write_text` turns every `\n` into `\r\n`. Reading and writing with `newline=""` makes a page revert return a file that is **byte-identical** to the snapshot — verified by sha256, not by eye.

`--dry-run` prints the plan and writes nothing. The MCP tool defaults to `dry_run: true` for the same reason.

**Every undo gets its own id, and says whose id it is undoing.** The new commit carries `reverts: sha256:…` and a message ending in `(undoes 9f2c1ab)`; the original keeps its id, its tree and its place in the chain. You never have to guess what "the value came back" was a reaction to:

```
82c7723  revert: clients/globex.md · frontmatter.status (undoes d699e0d)
d699e0d  手动改客户状态：Globex 已取消
```

In the web app, committing from the page opens the way back immediately — that is the point of a page you can write from:

![after committing: 已提交 d699e0d · 1 处变化 / 撤销会再新增一个 commit，d699e0d 仍然留在历史里](docs/assets/13-undo-offer.png)

Undoing asks once more, with the plan spelled out (`恢复为 on-hold`), and then appends rather than replaces:

![the undo confirmation: 会新增一个 commit 把这次改动改回去，d699e0d 本身不删除、不改写](docs/assets/14-undo-confirm.png)

![the result: 已撤销：新 commit 82c7723 记录了 reverts d699e0d，and both commits are in the timeline](docs/assets/15-undo-done.png)

## Spaces: what this install is allowed to touch

You have a personal vault. Your team has a shared one. A memory tool that can write to both, from any directory, with no notion of which is which, is a tool that will eventually write to the wrong one.

So the boundary is hard-coded, not a setting:

| Situation | What is allowed |
|---|---|
| Fresh install, no registry yet | **Read** the directory you run the command in. Nothing else. No writes at all |
| `memdrift init ~/vault` | That directory becomes a **write** space — creating the store *is* the declaration |
| `memdrift space allow ~/shared --read` | Someone else's vault: readable, still impossible to commit to |
| `memdrift space allow ~/shared --write` | You are allowed to change it |
| Anything else | Refused, with the exact command that would allow it |

```bash
memdrift space list
memdrift space allow ~/team-memory --read
memdrift space allow ~/team-memory --write
memdrift space deny ~/team-memory
```

A refusal looks like this — and it has to, because a "no" that does not say how to proceed gets worked around by editing the source:

```
refused: /srv/shared/team-memory is read-only for this install
  you may read: /srv/shared/team-memory
  allow writes:  memdrift space allow /srv/shared/team-memory --write
```

Three things this deliberately does **not** offer: a `--allow-all` flag, an environment variable that turns the check off, and a registry inside the vault. The list lives in `~/.memdrift/spaces.json` — a vault you clone can carry its own `spaces.json` and it will be read as memory content, never as permission. `MEMDRIFT_HOME` moves the file; it cannot empty the rule.

The check runs at every entry point: the CLI (`_open`), `serve` (read), `ui` (write, re-checked on every click so revoking a space takes effect without a restart), and the MCP server — where `memory.commit` and `memory.revert` are refused on a read-only space while `memory.history` keeps answering. A dry-run revert is a read, so it stays available.

## 从零开始：点一遍就懂

If you have never heard of memdrift and just want to find out what it is, don't read the docs — click through this:

**打开 [`docs/onboarding.html`](docs/onboarding.html)**（双击，或在编辑器的预览面板里打开）。

It is a 9-step clickable workflow that follows a user who knows nothing at all:

> 它是什么 → 敢不敢指向我真正在乎的目录 → agent 半夜到底改了什么 → 我不想盯着终端
> → 记错了怎么撤 → 能不能在编辑器里直接问 → 发出去会不会泄露 → 我现在全懂了

左边九步可以乱跳，带下划线的词点开有名词解释，命令块点一下就复制，`←` `→` 翻页，还有个「🎲 随便跳一步」。最后一页是命令速查表和那三件还没做的事。

Every terminal block in it is **real output**: `scripts/build_onboarding.py` builds a vault, commits, reverts, starts `serve`, and performs an actual MCP stdio handshake, then embeds what those processes printed. If a command breaks, the build breaks — no hand-drawn screens. `scripts/verify_onboarding.py` checks the result is offline/self-contained, free of machine paths, and still has all nine steps.

## Quickstart

```bash
pip install -e .
memdrift init ~/notes
memdrift -C ~/notes commit -m "manual fix" --evidence email:msg-1882
memdrift -C ~/notes blame clients/acme.md
```

`init` takes a baseline snapshot of every `.md` file it finds and never touches your files again.

`watch` is the intended mode — start it once, and every burst of agent writes lands as one commit:

![memdrift watch: three files touched within a second become a single commit](docs/assets/07-watch.png)

### Why this is safe to try

Delete `.memgit/` and your memory is exactly as it was — history is lost, content is not. That invariant is tested, and re-checked every time this README's screenshots are regenerated:

![sha256 of acme.md identical before and after deleting .memgit](docs/assets/06-survives-deletion.png)

It is the only reason to point a tool at a directory you care about, so it is the one thing the tool refuses to get wrong.

And it cannot write to a directory you did not declare. On a fresh install the only thing memdrift can do outside `memdrift init` is *read* the directory it was started in — see [Spaces](#spaces-what-this-install-is-allowed-to-touch).

`init` and `status` look like this:

![memdrift init writing a baseline commit, then status listing a rename, a new page and two modified pages](docs/assets/01-init-status.png)

## Commands

| Command | What it does |
|---|---|
| `memdrift init <dir>` | Create `.memgit/`, write `config.toml`, baseline commit |
| `memdrift -C <dir> watch` | Watch for writes, merge a burst into one commit after 5s of quiet |
| `memdrift -C <dir> commit -m <msg> --evidence <ref>` | Manual commit with provenance |
| `memdrift -C <dir> log [--page p] [--since 7d]` | History, newest first |
| `memdrift -C <dir> diff [a] [b]` | Working tree vs HEAD, working tree vs one commit, or two snapshots |
| `memdrift -C <dir> show [commit]` | What one commit changed, with its evidence |
| `memdrift -C <dir> blame <page>` | Per-field: which commit last changed it, with evidence |
| `memdrift -C <dir> status` | Uncommitted changes |
| `memdrift -C <dir> digest [--since 1d] [--no-quote] [-o file]` | Self-contained HTML report, redacted by default |
| `memdrift -C <dir> serve [--port 7787] [--open]` | Read-only local UI on 127.0.0.1, token in the URL |
| `memdrift -C <dir> ui [--port 7788] [--open]` | The same views plus undo-one-field and commit, each behind a plan you approve |
| `memdrift -C <dir> revert [--field f] [--page p] [--commit c] [--dry-run]` | Undo as a new commit; history is never rewritten |
| `memdrift -C <dir> mcp [--print-config] [--no-quote]` | Serve this directory over MCP (stdio) |
| `memdrift space list` | Directories this install may read or write |
| `memdrift space allow <dir> [--write]` | Declare one (read-only unless `--write`) |
| `memdrift space deny <dir>` | Forget one |
| `memdrift -C <dir> index --rebuild` | Rebuild the SQLite index from commit files |

Add `--json` to `log`, `diff`, `show`, `blame`, `status`, `commit`, `revert` for machine output.

## Sharing it without leaking it

A memory directory is a biography. The moment you paste a digest into a group chat you have published it, so the defaults are built around that:

| Risk | Default behaviour |
|---|---|
| **Machine paths** | `C:\Users\alice\vault\a.md` → `~/clients/a.md`. The root is stripped everywhere: change lists, evidence refs, MCP tool output. A path that is *not* under the root keeps only its last two segments, so `AppData/Local/...` never survives. |
| **Pasted paths inside values** | A path in the middle of a sentence is redacted too, not just one at position 0. Values are stored as 72-char previews, so a path cut mid-directory is replaced wholesale rather than half-guessed. |
| **Verbatim quotes** | Truncated to 120 chars with a `…`. `--no-quote` keeps the reference (`email:msg-1882`) and drops the text. |
| **Private pages** | `private: true` in frontmatter removes the page *and every evidence entry pointing at it* — otherwise the leak just moves one hop. Every surface that can be handed a page name (`memory.page`, `memory.blame`, `memory.history`, `memory.diff`, `memory.revert`, `/page/<path>` on `serve`, the undo endpoint on `ui`) refuses one, and a commit message that spells the path out is masked to `(private)`. |
| **Commit messages** | A message is free text, and agents paste paths into free text. It goes through the same redactor as every value, so `C:\Users\alice\…` in a message becomes `~/…` in the digest, the MCP answer and the web UI. |
| **Escaping** | All memory text is HTML-escaped; a page whose title is `<script>` renders as text. |
| **`serve` binding** | Loopback only. `--host 0.0.0.0` is refused by design, not by config. |
| **CSRF / DNS rebinding** | A per-process random token in the URL, plus a `Host` whitelist and an `Origin` match. A page that rebinds `evil.test` to 127.0.0.1 gets 403 even with a valid token. |
| **MCP writes** | The two writer tools are annotated `readOnlyHint: false`; `memory.revert` also declares `destructiveHint` and defaults to `dry_run: true`. |
| **MCP path traversal** | A `page` argument is resolved and rejected if it leaves the memory root — an agent cannot ask for `../../etc/passwd`. |

`--no-redact` exists and prints a warning. Use it only for a file that never leaves the machine.

What is **not** encrypted: `.memgit/` objects are zlib-compressed, not encrypted. Anyone who can read the directory can read the history. If that is your threat model, put the directory on an encrypted volume — pretending to invent a key-management scheme here would be worse.

## How a change is stored

A commit is a snapshot plus **structured** changes:

```json
{
  "id": "sha256:9f2c…",
  "parent": "sha256:1a7b…",
  "ts": "2026-11-22T00:00:12+08:00",
  "author": { "type": "agent", "name": "linger-dreaming" },
  "message": "nightly consolidation",
  "changes": [
    {
      "path": "clients/acme.md",
      "op": "modify",
      "field": "frontmatter.pricing",
      "before": "$45k",
      "after": "$48k",
      "section": null,
      "change_ratio": 0.125
    }
  ],
  "evidence": [
    { "kind": "email", "ref": "msg-1882", "quote": "we can do 48k for 200 seats" }
  ],
  "confidence": 0.82
}
```

Field paths are `frontmatter.<dotted.key>`, `section:<heading>`, or `body`. `op` is one of `add · modify · delete · rename`. Unknown fields are ignored by readers, so the format can grow.

When heading structure collapses (a page gets rewritten wholesale), attribution degrades to `body` and is flagged `low_precision: true` rather than pretending to be precise. Renaming a heading is recorded as one `rename` with the old and new text, not as a delete plus an add.

## Layout

```
<memory-dir>/
  .memgit/
    config.toml
    objects/ab/cdef…      # zlib, sha256-addressed
    commits/ab/cdef….json # one json per commit
    refs/head
    index.sqlite          # rebuildable cache
```

Code: `repo` (objects, refs) · `differ` (field-level diff) · `events` (the one stream) · `security` (redaction) · `scope` (which directories may be touched, and how) · `digest` / `server` (read-only views) · `gui` (`memdrift ui`, the one writable surface) · `revert` (undo) · `rpc` / `mcp` (the agent channel).

## Development

```bash
git clone git@github.com:x-tian-lab/memdrift.git && cd memdrift
python -m venv .venv && .venv/bin/pip install -e ".[docs]"
.venv/bin/pytest -q
.venv/bin/python scripts/render_terminal_shots.py   # regenerates docs/assets
.venv/bin/python scripts/smoke_serve.py             # drives serve over real HTTP
.venv/bin/python scripts/build_onboarding.py        # regenerates docs/onboarding.html
.venv/bin/python scripts/verify_onboarding.py       # checks the tour is real & offline
```

Every screenshot in this README is produced by that last command: it builds a demo vault, runs the real binary, and fails loudly if any command does. There is no hand-edited output anywhere in the images.

## Not done yet

Four things are missing. They are written down here rather than quietly skipped, because each one changes what you can safely assume.

**1. Concurrent writers.** `.memgit/refs/head` is a single file with no lock, so the current model is **one writer, many readers**. Two agents committing at the same instant can lose one of the commits — the second reads HEAD, writes its commit, and overwrites the ref the first just moved. Until there is a lock, `watch` is the intended path: it serialises a burst of writes into one commit. Nothing in the code pretends this is safe; there is no optimistic retry and no merge.

**2. A rules engine, multi-vault aggregation, and cross-system conflict detection (v1.1).** memdrift records *what changed* and *why it says it did*. It does not judge whether a change should have happened, it cannot join the histories of two memory directories, and it will not notice that Hindsight and mem0 disagree about the same fact. Those are the interesting next questions; they are not in v0.1.

**3. Full `--exclude` glob semantics.** The current matcher handles `**/name/**` by directory name — enough to keep `node_modules` and `.venv` out of a snapshot — but it is not gitignore syntax. Nested negations, character classes and anchored patterns do not behave the way a git user would expect.

**4. Spaces are per-machine, not per-person.** `~/.memdrift/spaces.json` says what *this install* may touch; it has no idea who is sitting in front of it. Two people on one account share one list. That is the honest limit of a local single-user tool: it stops an agent from wandering into the wrong directory, it does not stop a colleague at your keyboard. Real multi-user separation needs the OS (separate accounts, file permissions) — that is what those are for.

Encryption is deliberately *not* on this list: it is a decision, not a gap (see `docs/DESIGN.md` §9.6).

## Status

v0.1, week 3 of the plan: `init · watch · commit · log · diff · show · blame · status · digest · serve · ui · revert · mcp` all work on a real directory, and 290 tests pass. Nothing here is stable enough to embed yet.

Requirements: Python 3.11+, macOS / Linux / Windows. Three dependencies: `click`, `python-frontmatter`, `watchdog`. The MCP server is stdlib-only — it does not need the official SDK, and a JSON-RPC layer we can read in one sitting beats a dependency that breaks in someone else's editor.

Local only. No network calls, ever: `serve` binds to loopback, `mcp` speaks over stdin/stdout.

## License

MIT.
