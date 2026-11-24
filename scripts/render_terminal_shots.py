"""Render real CLI output as terminal-style PNGs for the README.

Nothing here invents output: every command is executed against the demo vault
and whatever it prints is what gets drawn. If a command breaks, this script
breaks — that is the point.

    python scripts/render_terminal_shots.py [vault_dir]
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ----------------------------------------------------------------- appearance

BG = (30, 32, 36)
BAR = (44, 47, 52)
DOT_RED = (237, 105, 98)
DOT_YELLOW = (240, 191, 88)
DOT_GREEN = (98, 197, 84)
FG = (222, 226, 232)
FG_DIM = (128, 136, 148)
FG_PROMPT = (130, 200, 145)
ACCENT = {
    "yellow": (229, 192, 123),
    "green": (152, 195, 121),
    "red": (224, 108, 117),
    "cyan": (86, 182, 194),
    "white": (222, 226, 232),
    "bold": (232, 236, 242),
    "dim": (110, 118, 130),
}

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\CascadiaMono.ttf",
    r"C:\Windows\Fonts\CascadiaCode.ttf",
    r"C:\Windows\Fonts\consola.ttf",
    "/System/Library/Fonts/SFNSMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
]
# Latin monospace fonts rarely carry CJK; fall back per-glyph, like a terminal does.
CJK_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]
FONT_SIZE = 15
LINE_HEIGHT = 25
PAD_X = 26
PAD_TOP_BAR = 40
PAD_BOTTOM = 22

ANSI_RE = re.compile(r"\x1b\[([0-9;]*)m")


def _load_first(candidates: list[str], size: int):
    for cand in candidates:
        if os.path.exists(cand):
            try:
                return ImageFont.truetype(cand, size)
            except OSError:
                continue
    return None


def load_fonts(size: int = FONT_SIZE) -> tuple[ImageFont.FreeTypeFont, ImageFont.FreeTypeFont]:
    """(latin, cjk) — the second is only used for characters the first lacks."""
    latin = _load_first(FONT_CANDIDATES, size) or ImageFont.load_default(size=size)
    cjk = _load_first(CJK_FONT_CANDIDATES, size) or latin
    return latin, cjk


def load_font(size: int = FONT_SIZE):
    return load_fonts(size)[0]


def is_wide(ch: str) -> bool:
    """CJK / fullwidth → render with the CJK face.

    Arrow glyphs are included on purpose: Cascadia Mono draws U+2192 as a thin
    hairline that reads as a hyphen at 15px, while the CJK face has a proper one.
    """
    cp = ord(ch)
    return (
        0x2E80 <= cp <= 0x9FFF
        or 0xF900 <= cp <= 0xFAFF
        or 0xFF00 <= cp <= 0xFF60
        or 0x3000 <= cp <= 0x303F
        or 0x2190 <= cp <= 0x21FF  # → ← ↑ ↓
    )


@dataclass
class Segment:
    text: str
    color: tuple[int, int, int]


@dataclass
class Line:
    segments: list[Segment] = field(default_factory=list)


def parse_ansi(text: str, base_color=FG) -> list[Line]:
    """Turn click's ANSI output into coloured spans. Keeps it honest."""
    lines: list[Line] = []
    for raw in text.splitlines():
        line = Line()
        color = base_color
        pos = 0
        for match in ANSI_RE.finditer(raw):
            if match.start() > pos:
                line.segments.append(Segment(raw[pos : match.start()], color))
            codes = [c for c in match.group(1).split(";") if c]
            if not codes or "0" in codes:
                color = base_color
            else:
                for code in codes:
                    if code == "1":
                        color = ACCENT["bold"]
                    elif code == "2":
                        color = ACCENT["dim"]
                    elif code == "33":
                        color = ACCENT["yellow"]
                    elif code == "32":
                        color = ACCENT["green"]
                    elif code == "31":
                        color = ACCENT["red"]
                    elif code == "36":
                        color = ACCENT["cyan"]
                    elif code == "37":
                        color = ACCENT["white"]
            pos = match.end()
        if pos < len(raw):
            line.segments.append(Segment(raw[pos:], color))
        lines.append(line)
    return lines


# ------------------------------------------------------------------- rendering


def text_width(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    return int(draw.textlength(text, font=font))


def draw_runs(
    draw: ImageDraw.ImageDraw,
    x: float,
    y: float,
    text: str,
    color: tuple[int, int, int],
    latin,
    cjk,
) -> float:
    """Draw text with per-glyph font fallback. Returns the end x."""
    if not text:
        return x
    run: list[str] = []
    run_cjk = is_wide(text[0])
    for ch in text:
        wide = is_wide(ch)
        if wide != run_cjk:
            font = cjk if run_cjk else latin
            draw.text((x, y), "".join(run), font=font, fill=color)
            x += draw.textlength("".join(run), font=font)
            run = []
            run_cjk = wide
        run.append(ch)
    font = cjk if run_cjk else latin
    draw.text((x, y), "".join(run), font=font, fill=color)
    return x + draw.textlength("".join(run), font=font)


def measure_text(draw: ImageDraw.ImageDraw, text: str, latin, cjk) -> float:
    total = 0.0
    run: list[str] = []
    run_cjk = is_wide(text[0]) if text else False
    for ch in text:
        wide = is_wide(ch)
        if wide != run_cjk:
            total += draw.textlength("".join(run), font=cjk if run_cjk else latin)
            run = []
            run_cjk = wide
        run.append(ch)
    if run:
        total += draw.textlength("".join(run), font=cjk if run_cjk else latin)
    return total


def wrap_line(line: Line, latin, cjk, max_width: int, draw: ImageDraw.ImageDraw) -> list[Line]:
    """Soft-wrap by measured pixel width, preserving span colours."""
    if not line.segments:
        return [Line()]
    out: list[Line] = []
    current = Line()
    width = 0.0
    for seg in line.segments:
        for ch in seg.text:
            ch_w = measure_text(draw, ch, latin, cjk)
            if width + ch_w > max_width:
                out.append(current)
                current = Line()
                width = 0
            if current.segments and current.segments[-1].color == seg.color:
                current.segments[-1].text += ch
            else:
                current.segments.append(Segment(ch, seg.color))
            width += ch_w
    out.append(current)
    return out


def render(
    blocks: list[tuple[str, str]],
    out_path: Path,
    *,
    title: str = "memdrift — demo",
    width: int = 1080,
) -> None:
    """``blocks`` = [(prompt_command_or_empty, stdout_text), …]"""
    latin, cjk = load_fonts()
    probe = Image.new("RGB", (10, 10))
    measurer = ImageDraw.Draw(probe)
    max_text_width = width - PAD_X * 2

    rendered: list[Line] = []
    for command, output in blocks:
        if command:
            for i, part in enumerate(command.split("\n")):
                prefix = "❯ " if i == 0 else "  "
                cmd_line = Line([
                    Segment(prefix, ACCENT["green"]),
                    Segment(part, ACCENT["bold"]),
                ])
                rendered.extend(wrap_line(cmd_line, latin, cjk, max_text_width, measurer))
        if output:
            for line in parse_ansi(output):
                rendered.extend(wrap_line(line, latin, cjk, max_text_width, measurer))
        if command or output:
            rendered.append(Line())

    if rendered and not rendered[-1].segments:
        rendered.pop()

    # cap the canvas so one long line cannot turn into a 4000px-tall image
    if len(rendered) > 46:
        hidden = len(rendered) - 45
        rendered = rendered[:45] + [Line([Segment(f"… {hidden} more line(s)", ACCENT["dim"])])]

    height = PAD_TOP_BAR + 18 + len(rendered) * LINE_HEIGHT + PAD_BOTTOM
    img = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(img)

    # title bar
    draw.rectangle([0, 0, width, PAD_TOP_BAR - 10], fill=BAR)
    for i, color in enumerate((DOT_RED, DOT_YELLOW, DOT_GREEN)):
        cx = 22 + i * 22
        draw.ellipse([cx - 6, 15 - 6, cx + 6, 15 + 6], fill=color)
    title_font = load_font(13)
    tw = text_width(draw, title, title_font)
    draw.text(
        ((width - tw) / 2, 7), title, font=title_font, fill=FG_DIM
    )

    y = PAD_TOP_BAR + 8
    for line in rendered:
        x = PAD_X
        for seg in line.segments:
            x = draw_runs(draw, x, y, seg.text, seg.color, latin, cjk)
        y += LINE_HEIGHT

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    print(f"wrote {out_path}  ({width}×{height})")


# ---------------------------------------------------------------------- driver


def redact(text: str, vault: Path) -> str:
    """Screenshots go in a public README: no local absolute paths."""
    # `mcp --print-config` emits the interpreter as an absolute path. It is
    # genuinely machine-specific, so show the portable form a reader would
    # type instead of the workspace we happen to build in.
    text = re.sub(
        r"[A-Za-z]:[\\/][^\s\"']*[\\/]python(\.exe)?", "python", text
    )
    # The vault path also appears inside JSON strings, where every separator
    # is doubled (``-C "C:\\\\Users\\\\..."``). Match those first: plain
    # `str(vault)` cannot, and the escaped form would slip through.
    escaped = str(vault).replace("\\", "\\\\")
    for pattern in {
        escaped,
        str(vault).replace("\\", "\\\\\\\\"),
        str(vault).replace("\\", "/"),
    }:
        text = text.replace(pattern, "~/vault")
    for variant in {str(vault), str(vault).replace("\\", "/"), "/" + str(vault).replace("\\", "/")}:
        text = text.replace(variant, "~/vault")
    text = text.replace(str(vault.parent), "~")
    # any windows drive prefix left over, e.g. C:\Users\<someone>\...
    text = re.sub(r"[A-Za-z]:\\Users\\[^\\]+\\", "~/", text)
    # and unix ones, e.g. /home/<someone>/...
    text = re.sub(r"/(home|Users)/[^/]+/", "~/", text)
    return text


def run(args: list[str], cwd: Path | None = None) -> str:
    env = dict(
        os.environ,
        COLUMNS="110",
        CLICOLOR_FORCE="1",
        FORCE_COLOR="1",
        TERM="xterm-256color",
        PYTHONIOENCODING="utf-8",
    )
    proc = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"command failed ({proc.returncode}): {' '.join(args)}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    return proc.stdout


def capture_watch(memdrift: Path, vault: Path, repo_root: Path, seconds: float = 1.5) -> str:
    """Run the real watcher, touch three files in one burst, collect its output."""
    env = dict(
        os.environ,
        CLICOLOR_FORCE="1",
        FORCE_COLOR="1",
        TERM="xterm-256color",
        PYTHONIOENCODING="utf-8",
    )
    proc = subprocess.Popen(
        [str(memdrift), "-C", str(vault), "watch", "--debounce", str(seconds)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    try:
        time.sleep(2.5)  # let the observer arm
        targets = [
            (vault / "clients" / "globex.md", lambda s: s + "\n## 下周\n\n周三复盘。\n"),
            (vault / "topics" / "pricing.md",
             lambda s: s.replace("年付 9 折。", "年付 85 折。")),
            (vault / "sessions" / "weekly.md",
             lambda s: s + "\n## 跟进\n\nAcme 报价已确认。\n"),
        ]
        for path, mutate in targets:
            path.write_text(mutate(path.read_text(encoding="utf-8")), encoding="utf-8")
            time.sleep(0.3)
        time.sleep(seconds + 3.5)
    finally:
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
    if "committed" not in out:
        raise SystemExit(f"watcher did not commit — output was:\n{out}")
    return out


def capture_mcp(memdrift: Path, vault: Path) -> tuple[str, str]:
    """Talk to the real MCP server over stdio and render *its* answers.

    Not a mocked tool table: the list of tools and the revert plan below are
    parsed from actual JSON-RPC responses. If a tool disappears or changes its
    approval hint, the screenshot changes with it.
    """
    proc = subprocess.Popen(
        [str(memdrift), "-C", str(vault), "mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    counter = {"id": 0}

    def ask(method: str, params: dict | None = None) -> dict:
        counter["id"] += 1
        message: dict = {"jsonrpc": "2.0", "id": counter["id"], "method": method}
        if params is not None:
            message["params"] = params
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        if not line:
            raise SystemExit(
                "mcp server closed the pipe:\n" + (proc.stderr.read() or "")
            )
        return json.loads(line)

    try:
        ask(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "shots", "version": "1"},
            },
        )
        tools = ask("tools/list")["result"]["tools"]
        history = ask(
            "tools/call",
            {"name": "memory.history", "arguments": {"limit": 3}},
        )["result"]["structuredContent"]
    finally:
        proc.stdin.close()
        proc.wait(timeout=15)

    if len(tools) != 7:
        raise SystemExit(f"expected 7 MCP tools, got {len(tools)}")

    listing = [
        "tools/list → 7 tools · readOnlyHint decides whether a client asks first",
        "",
    ]
    for tool in tools:
        ann = tool["annotations"]
        tag = "read" if ann["readOnlyHint"] else "WRITE"
        if ann.get("destructiveHint"):
            tag += " · destructive"
        listing.append(f"  {tool['name']:<18} {tag}")

    plan = [
        "  › tools/call memory.history {limit: 3}",
        "",
    ]
    for item in history["commits"]:
        plan.append(f"  {item['id']}  {item['message']}")
    return "\n".join(listing), "\n".join(plan)


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    vault = Path(sys.argv[1]) if len(sys.argv) > 1 else repo_root.parent / "demo-vault"
    memdrift = Path(sys.executable).parent / "memdrift.exe"
    if not memdrift.exists():
        memdrift = Path(sys.executable).parent / "memdrift"

    assets = repo_root / "docs" / "assets"

    # fresh vault so screenshots never show a half-used repository
    subprocess.run(
        [sys.executable, str(repo_root / "scripts" / "make_demo_vault.py"), str(vault)],
        check=True,
        capture_output=True,
    )
    init_out = run([str(memdrift), "init", str(vault)])

    sys.path.insert(0, str(repo_root / "scripts"))
    from make_demo_vault import apply_nightly_agent_run

    apply_nightly_agent_run(str(vault))
    status_out = run([str(memdrift), "-C", str(vault), "status"])
    commit_out = run(
        [
            str(memdrift), "-C", str(vault), "commit",
            "-m", "nightly consolidation",
            "--evidence", "email:msg-1882",
            "--confidence", "0.82",
            "--author-name", "linger-dreaming",
        ]
    )
    log_out = run([str(memdrift), "-C", str(vault), "log"])
    blame_out = run([str(memdrift), "-C", str(vault), "blame", "clients/acme.md"])
    show_out = run([str(memdrift), "-C", str(vault), "show"])

    # a human fixes a misremembered date, then looks at what is pending
    weekly = vault / "sessions" / "weekly.md"
    weekly.write_text(
        weekly.read_text(encoding="utf-8").replace("11/21", "11/22"),
        encoding="utf-8",
    )
    diff_out = run([str(memdrift), "-C", str(vault), "diff"])

    render(
        [
            (f'memdrift init ~/vault', redact(init_out, vault)),
            (f'memdrift -C ~/vault status', redact(status_out, vault)),
        ],
        assets / "01-init-status.png",
        title="memdrift — init & status",
    )
    render(
        [
            (
                'memdrift commit -m "nightly consolidation" \\\n'
                '  --evidence email:msg-1882 --confidence 0.82',
                redact(commit_out, vault),
            )
        ],
        assets / "02-commit.png",
        title="memdrift — the agent's change, recorded",
    )
    render(
        [
            ("memdrift -C ~/vault log", redact(log_out, vault)),
            ("memdrift -C ~/vault blame clients/acme.md", redact(blame_out, vault)),
        ],
        assets / "03-log-blame.png",
        title="memdrift — where did that come from",
    )
    render(
        [("memdrift -C ~/vault show", redact(show_out, vault))],
        assets / "05-show.png",
        title="memdrift — one commit, five changes",
    )
    render(
        [("memdrift -C ~/vault diff", redact(diff_out, vault))],
        assets / "04-diff.png",
        title="memdrift — uncommitted, field by field",
    )

    # digest: a real file, really written, from the same vault
    digest_out = run(
        [
            str(memdrift), "-C", str(vault), "digest",
            "--since", "7d",
            "--title", "memdrift · nightly consolidation",
            "-o", str(vault / "digest.html"),
        ],
        cwd=repo_root,
    )
    digest_html = vault / "digest.html"
    if not digest_html.exists():
        raise SystemExit("digest did not produce a file")
    html = digest_html.read_text(encoding="utf-8")
    for needle in (str(vault), str(vault).replace("\\", "/")):
        if needle in html:
            raise SystemExit("digest leaked an absolute path")
    if "http://" in html or "https://" in html:
        raise SystemExit("digest is not self-contained (contains a URL)")
    render(
        [("memdrift -C ~/vault digest --since 7d", redact(digest_out, vault))],
        assets / "08-digest.png",
        title="memdrift — one file, safe to send",
    )

    # revert: a bad extraction, then undo it. History stays append-only.
    # Commit the pending human fix first so the screenshots stay readable.
    run([str(memdrift), "-C", str(vault), "commit", "-m", "human fix: date"])
    acme = vault / "clients" / "acme.md"
    acme.write_text(
        acme.read_text(encoding="utf-8", newline="").replace(
            'pricing: "$48k"', 'pricing: "$12k"'
        ),
        encoding="utf-8",
        newline="",
    )
    run(
        [
            str(memdrift), "-C", str(vault), "commit",
            "-m", "extract: pricing dropped",
        ]
    )
    revert_dry = run(
        [
            str(memdrift), "-C", str(vault), "revert",
            "--field", "frontmatter.pricing",
            "--page", "clients/acme.md",
            "--dry-run",
        ]
    )
    revert_real = run(
        [
            str(memdrift), "-C", str(vault), "revert",
            "--field", "frontmatter.pricing",
            "--page", "clients/acme.md",
        ]
    )
    log_after = run([str(memdrift), "-C", str(vault), "log", "--limit", "3"])
    if "nothing written" not in revert_dry and "dry run" not in revert_dry:
        raise SystemExit("revert --dry-run did not report a dry run")
    if "reverted" not in revert_real:
        raise SystemExit("revert did not apply")
    render(
        [
            (
                "memdrift -C ~/vault revert --field frontmatter.pricing \\\n"
                "  --page clients/acme.md --dry-run",
                redact(revert_dry, vault),
            ),
            (
                "memdrift -C ~/vault revert --field frontmatter.pricing \\\n"
                "  --page clients/acme.md",
                redact(revert_real, vault),
            ),
            ("memdrift -C ~/vault log --limit 3", redact(log_after, vault)),
        ],
        assets / "09-revert.png",
        title="memdrift — undo without rewriting history",
    )

    # MCP: talk to the real stdio server, render its real answers.
    mcp_config = run([str(memdrift), "-C", str(vault), "mcp", "--print-config"])
    mcp_tools, mcp_history = capture_mcp(memdrift, vault)
    # The config block is nine lines of JSON that say one thing. Collapse it
    # to the two lines a reader needs; the full form is in the README.
    short_config = (
        '{\n  "command": "python",\n'
        '  "args": ["-m", "memdrift", "-C", "~/vault", "mcp"]\n}'
    )
    if '"memdrift"' not in mcp_config:
        raise SystemExit("mcp --print-config did not emit a server block")
    render(
        [
            ("memdrift -C ~/vault mcp --print-config", short_config),
            ("stdio · initialize / tools/list", redact(mcp_tools, vault)),
            ("stdio · tools/call", redact(mcp_history, vault)),
        ],
        assets / "10-mcp.png",
        title="memdrift — the same memory, from any MCP client",
    )

    # watch: run it for real, poke three files inside one debounce window
    watch_out = capture_watch(memdrift, vault, repo_root)
    render(
        [("memdrift -C ~/vault watch --debounce 1.5", redact(watch_out, vault))],
        assets / "07-watch.png",
        title="memdrift — a burst of writes, one commit",
    )

    # The trust story, proven on this same vault: history is a cache,
    # the .md file is the memory. Must run last — it deletes .memgit.
    proof_out = run(
        [
            sys.executable,
            "-c",
            "import hashlib, os, shutil, sys\n"
            "vault = sys.argv[1]\n"
            "f = os.path.join(vault, 'clients', 'acme.md')\n"
            "before = hashlib.sha256(open(f, 'rb').read()).hexdigest()\n"
            "print('sha256 acme.md          ' + before)\n"
            "shutil.rmtree(os.path.join(vault, '.memgit'))\n"
            "print('rm -rf ~/vault/.memgit')\n"
            "after = hashlib.sha256(open(f, 'rb').read()).hexdigest()\n"
            "print('sha256 acme.md          ' + after)\n"
            "print('\\x1b[32mcontent intact\\x1b[0m' if before == after else 'CONTENT LOST')",
            str(vault),
        ]
    )
    render(
        [("", proof_out)],
        assets / "06-survives-deletion.png",
        title="memdrift — delete the history, keep the memory",
    )


if __name__ == "__main__":
    main()
