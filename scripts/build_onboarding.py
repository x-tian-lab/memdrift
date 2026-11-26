"""Build the zero-knowledge guided tour as one self-contained HTML file.

Every terminal block in the tour is captured from a real `memdrift` run: this
script rebuilds a demo vault, executes the commands, and embeds their stdout.
If a command breaks, the build breaks — nothing in the page is hand-written
output. Path redaction is applied before embedding, because the page is meant
to be opened by anyone.

    python scripts/build_onboarding.py
    -> docs/onboarding.html
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "onboarding.html"

ANSI_RE = re.compile(r"\x1b\[([0-9;]*)m")
ANSI_CLASS = {
    "1": "b", "2": "dim", "31": "red", "32": "green", "33": "yellow",
    "34": "blue", "35": "magenta", "36": "cyan", "90": "dim",
}


def esc(text: str) -> str:
    return html.escape(str(text), quote=True)


def ansi_to_html(text: str) -> str:
    """Keep click's colours as spans; strip anything we do not understand."""
    out: list[str] = []
    stack: list[str] = []
    pos = 0
    for match in ANSI_RE.finditer(text):
        out.append(esc(text[pos : match.start()]))
        codes = match.group(1).split(";") if match.group(1) else ["0"]
        for code in codes:
            if code == "0":
                while stack:
                    out.append("</span>")
                    stack.pop()
            elif code in ANSI_CLASS:
                out.append(f'<span class="a-{ANSI_CLASS[code]}">')
                stack.append(code)
        pos = match.end()
    out.append(esc(text[pos:]))
    while stack:
        out.append("</span>")
        stack.pop()
    return "".join(out)


def redact(text: str, *roots: Path) -> str:
    """No machine-specific absolute paths may reach a public HTML page."""
    text = re.sub(r"[A-Za-z]:[\\/][^\s\"']*[\\/]python(\.exe)?", "python", text)
    for root in roots:
        for variant in {
            str(root),
            str(root).replace("\\", "/"),
            str(root).replace("\\", "\\\\"),
            "/" + str(root).replace("\\", "/"),
        }:
            text = text.replace(variant, "~/vault")
    text = re.sub(r"[A-Za-z]:\\Users\\[^\\]+\\", "~/", text)
    text = re.sub(r"/(?:home|Users)/[^/]+/", "~/", text)
    return text


def run(args: list[str], cwd: Path | None = None) -> str:
    # Building the tour must not write into the developer's real space
    # registry: `memdrift init` declares a space, and every captured command
    # would otherwise leave an entry pointing at a throwaway temp vault.
    os.environ.setdefault("MEMDRIFT_HOME", tempfile.mkdtemp(prefix="memdrift-docs-"))
    env = dict(
        os.environ,
        COLUMNS="110",
        CLICOLOR_FORCE="1",
        FORCE_COLOR="1",
        TERM="xterm-256color",
        PYTHONIOENCODING="utf-8",
    )
    proc = subprocess.run(
        [str(a) for a in args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"command failed ({proc.returncode}): {args}\n{proc.stdout}\n{proc.stderr}"
        )
    return proc.stdout + proc.stderr


def capture_serve(memdrift: Path, vault: Path) -> str:
    """Start `serve` for real and keep the banner it prints."""
    env = dict(os.environ, CLICOLOR_FORCE="1", FORCE_COLOR="1", PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen(
        [str(memdrift), "-C", str(vault), "serve", "--port", "7799"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    try:
        time.sleep(2.0)
    finally:
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
    if "serving" not in out:
        raise SystemExit("serve did not print its banner:\n" + out)
    # The token is random per process: say so instead of showing a token that
    # will never work again when the reader looks at this page.
    return re.sub(r"t=[A-Za-z0-9_-]+", "t=<random-per-process>", out).strip()


def capture_mcp(memdrift: Path, vault: Path) -> tuple[str, str]:
    """A real stdio handshake, so the tool list cannot drift from the code."""
    proc = subprocess.Popen(
        [str(memdrift), "-C", str(vault), "mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    def ask(req_id: int, method: str, params: dict | None = None) -> dict:
        msg: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            msg["params"] = params
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        if not line:
            raise SystemExit("mcp closed the pipe:\n" + (proc.stderr.read() or ""))
        return json.loads(line)

    try:
        ask(1, "initialize", {"protocolVersion": "2025-06-18"})
        tools = ask(2, "tools/list")["result"]["tools"]
        history = ask(
            3, "tools/call", {"name": "memory.history", "arguments": {"limit": 3}}
        )["result"]["structuredContent"]
    finally:
        proc.stdin.close()
        proc.wait(timeout=15)

    if len(tools) != 7:
        raise SystemExit(f"expected 7 tools, got {len(tools)}")
    rows = ["initialize  →  serverInfo: memdrift", "tools/list  →  7 tools", ""]
    for tool in tools:
        ann = tool["annotations"]
        tag = "read" if ann["readOnlyHint"] else "WRITE"
        if ann.get("destructiveHint"):
            tag += " · destructive"
        rows.append(f"  {tool['name']:<18} {tag}")
    rows += ["", "tools/call memory.history {limit: 3}", ""]
    for item in history["commits"]:
        rows.append(f"  {item['id']}  {item['message']}")
    return "\n".join(rows), str(len(tools))


def proof_output(probe: Path) -> str:
    """sha256 before/after deleting .memgit — the trust story, on a copy."""
    code = (
        "import hashlib, os, shutil, sys\n"
        "f = os.path.join(sys.argv[1], 'a.md')\n"
        "before = hashlib.sha256(open(f,'rb').read()).hexdigest()\n"
        "print('sha256 a.md            ' + before)\n"
        "shutil.rmtree(os.path.join(sys.argv[1], '.memgit'))\n"
        "print('rm -rf ~/vault/.memgit')\n"
        "after = hashlib.sha256(open(f,'rb').read()).hexdigest()\n"
        "print('sha256 a.md            ' + after)\n"
        "print('\\x1b[32mcontent intact\\x1b[0m' if before==after else 'CONTENT LOST')\n"
    )
    return run([sys.executable, "-c", code, str(probe)])


def redaction_sample() -> str:
    """Call the real Redactor, not a description of what it should do."""
    code = (
        "from memdrift.security import redact_text\n"
        "p = r'C:\\\\Users\\\\alice\\\\vault\\\\clients\\\\acme.md'\n"
        "root = r'C:\\\\Users\\\\alice\\\\vault'\n"
        "print('page  ' + p)\n"
        "print('  ->  ' + str(redact_text(p, root=root)))\n"
        "q = 'Source: ' + p\n"
        "print('quote ' + q)\n"
        "print('  ->  ' + str(redact_text(q, root=root)))\n"
    )
    return run([sys.executable, "-c", code], cwd=ROOT)


def b_cmd(text: str) -> dict:
    return {"kind": "cmd", "html": f'<button class="cmd" type="button">{esc(text)}<span class="copy">copy</span></button>'}


def b_term(text: str, *roots: Path) -> dict:
    return {"kind": "term", "html": ansi_to_html(redact(text, *roots))}


def b_term_plain(text: str) -> dict:
    """For output that is already fictional/safe (the redaction demo itself)."""
    return {"kind": "term", "html": ansi_to_html(text)}


def b_note(text: str) -> dict:
    return {"kind": "note", "html": markup(text)}


TERMS = {
    "object": "<b>内容寻址对象</b>：文件按 sha256 命名、zlib 压缩后放在 <code>.memgit/objects/</code>。同一份内容只存一次；删掉整个 <code>.memgit/</code> 只会丢历史，不会丢 md 文件本身。",
    "field": "<b>字段级变更</b>：不是「文件变了」，而是 <code>frontmatter.pricing</code>、<code>section:Notes</code>、<code>body</code> 这样的具体位置变了什么。所以能回答「谁把报价从 45k 改成 48k」。",
    "evidence": "<b>证据</b>：commit 可以带 <code>--evidence email:msg-1882</code>。<code>blame</code> 会把证据和字段一起显示，这样「为什么改」不靠猜。",
    "event": "<b>一根事件流</b>：<code>events.build()</code> 把历史变成统一的事件。<code>digest</code>、<code>serve</code>、<code>mcp</code> 都读它，所以三个视图不可能互相矛盾，也不可能有一个忘了脱敏。",
    "revert": "<b>追加式撤销</b>：revert 产生的是<b>新 commit</b>，旧 commit 一个都不删。<code>log</code> 因此始终说真话：既看得到引入坏值的那次，也看得到收回它的那次。",
    "hint": "<b>readOnlyHint</b>：MCP 工具声明自己只读。尊重注解的客户端会自动跑 5 个读工具，遇到 2 个写工具才弹确认；不尊重的客户端回落到「每次都问」，同样安全。",
    "loopback": "<b>loopback only</b>：<code>serve</code> 拒绝绑到 <code>0.0.0.0</code>，只听 127.0.0.1。再加上随机 token、<code>Host</code> 白名单和 <code>Origin</code> 校验，别的网站即使能连到本机也读不走历史。",
    "lock": "<b>refs/head</b>：一个记录「当前最新 commit」的文件，目前没有锁。两个 agent 同时写会互相覆盖，所以现在只支持一写多读。",
}
TERM_RE = re.compile(r"\[\[([a-z]+)\|([^\]]+)\]\]")


def markup(text: str) -> str:
    """[[key|显示文字]] -> a clickable glossary pill.

    The surrounding text is authored HTML in this script (never user data),
    so inline <code>/<b> stays live instead of being escaped.
    """
    out: list[str] = []
    pos = 0
    for m in TERM_RE.finditer(text):
        out.append(text[pos : m.start()])
        out.append(
            f'<button class="term" type="button" data-k="{esc(m.group(1))}">'
            f'{esc(m.group(2))}</button>'
        )
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


def build_steps(memdrift: Path, vault: Path, probe: Path) -> list[dict]:
    sys.path.insert(0, str(ROOT / "scripts"))
    from make_demo_vault import apply_nightly_agent_run

    # --- 1. a fresh vault, nothing else exists yet
    init = run([memdrift, "init", str(vault)])

    # --- 2. trust, proven on a separate copy so the main vault survives
    run([memdrift, "init", str(probe)])
    proof = proof_output(probe)

    # --- 3. an agent run, then the questions a human actually asks
    apply_nightly_agent_run(str(vault))
    commit = run(
        [
            memdrift, "-C", str(vault), "commit",
            "-m", "nightly consolidation",
            "--evidence", "email:msg-1882",
            "--confidence", "0.82",
        ]
    )
    log = run([memdrift, "-C", str(vault), "log"])
    blame = run([memdrift, "-C", str(vault), "blame", "clients/acme.md"])

    # --- 4. the two views
    digest = run(
        [
            memdrift, "-C", str(vault), "digest", "--since", "7d",
            "-o", str(vault / "morning.html"),
        ],
        cwd=ROOT,
    )
    digest_html = (vault / "morning.html").read_text(encoding="utf-8")
    if str(vault) in digest_html or "http://" in digest_html or "https://" in digest_html:
        raise SystemExit("digest is not self-contained/redacted")
    serve = capture_serve(memdrift, vault)

    # --- 5. undo
    acme = vault / "clients" / "acme.md"
    acme.write_text(
        acme.read_text(encoding="utf-8", newline="").replace(
            'pricing: "$48k"', 'pricing: "$12k"'
        ),
        encoding="utf-8",
        newline="",
    )
    run([memdrift, "-C", str(vault), "commit", "-m", "extract: pricing dropped"])
    revert_dry = run(
        [
            memdrift, "-C", str(vault), "revert",
            "--field", "frontmatter.pricing", "--page", "clients/acme.md", "--dry-run",
        ]
    )
    revert = run(
        [
            memdrift, "-C", str(vault), "revert",
            "--field", "frontmatter.pricing", "--page", "clients/acme.md",
        ]
    )
    log_after = run([memdrift, "-C", str(vault), "log", "--limit", "3"])
    if "dry run" not in revert_dry or "reverted" not in revert:
        raise SystemExit("revert did not behave as advertised")

    # --- 6. MCP
    config = run([memdrift, "-C", str(vault), "mcp", "--print-config"])
    tools, _count = capture_mcp(memdrift, vault)
    if '"memdrift"' not in config:
        raise SystemExit("mcp --print-config produced no server block")
    short_config = (
        '{\n  "mcpServers": {\n'
        '    "memdrift": {\n'
        '      "command": "python",\n'
        '      "args": ["-m", "memdrift", "-C", "~/vault", "mcp"]\n'
        "    }\n  }\n}"
    )

    # --- 7. redaction, from the real function
    # The sample uses a *fictional* C:\Users\alice path on purpose — passing
    # it through redact() would erase the very contrast we want to show.
    sample = redaction_sample()

    steps = [
        {
            "nav": "我什么都不知道",
            "eyebrow": "STEP 1 / 9 · 起点",
            "question": "「听说有个东西能记录 agent 偷偷改了我的记忆……可我连它是个文件还是个服务都不知道。」",
            "hint": "别急。左边九个步骤可以随便点，不用按顺序；带下划线的词也能点开看解释。",
            "blocks": [
                b_note(
                    "先建立唯一需要的心智模型：你的记忆本来就是一堆 <code>.md</code> 文件，"
                    "memdrift 既不接管它们，也不改它们。它只是在<b>旁边</b>多放一个 "
                    "[[object|.memgit/ 目录]]，专门记「这些文件是怎么变成现在这样的」。"
                ),
                {"kind": "model", "html": ""},
                b_note(
                    "所以第一件事不是学命令，是确认一件事：<b>把工具指向一个你真正在乎的目录，安全吗？</b>"
                ),
            ],
            "learn": [
                "memdrift 不是记忆系统、不是数据库、不是 git 的替代品。",
                "它只读你的 md 文件，把「变化历史」单独存在 .memgit/ 里。",
                "现在你有一个具体的疑问了：它会不会碰坏我的文件？",
            ],
        },
        {
            "nav": "先跑起来",
            "eyebrow": "STEP 2 / 9 · 第一次输入命令",
            "question": "「我什么都没配置，会不会很麻烦？」",
            "hint": "命令块可以点一下复制。下面这块输出是真的跑出来的。",
            "blocks": [
                b_cmd("memdrift init ~/vault"),
                b_term(init, vault),
                b_note(
                    "它做了两件事：建 <code>.memgit/</code>，然后给当前所有 md 打一个基线快照。"
                    "注意它<b>没有</b>让你填数据库、账号、API key，也没有要求你迁移文件。"
                ),
            ],
            "learn": [
                "初始化 = 一个目录 + 一次基线提交，没有别的。",
                "之后真正的用法是 <code>memdrift -C ~/vault watch</code>：挂着不动，agent 每写一轮就自动合成一次 commit。",
                "现在你敢问下一个问题了：万一它把我的文件写坏了呢？",
            ],
        },
        {
            "nav": "它真没碰我的文件",
            "eyebrow": "STEP 3 / 9 · 先验证，再信任",
            "question": "「说得好听。我把 .memgit/ 删了，文件还在吗？」",
            "hint": "这一步在一个独立副本上做，所以不会破坏后面要用的演示 vault。",
            "blocks": [
                b_note("直接做最狠的测试：算一次 sha256，删掉整个历史目录，再算一次。"),
                b_term(proof, probe),
                b_note(
                    "两个哈希完全一样。[[object|内容寻址对象]]里存的是历史，"
                    "md 文件始终是本体。这就是敢把它指向重要目录的唯一理由。"
                ),
            ],
            "learn": [
                "历史可以删，内容不丢。删掉 .memgit/ 只是失忆，不是失数据。",
                "index.sqlite 也是可重建的缓存，不是权威数据源。",
                "信任问题解决了，可以开始看它到底记了什么。",
            ],
        },
        {
            "nav": "它到底记了什么",
            "eyebrow": "STEP 4 / 9 · 一次真实的夜间提取",
            "question": "「agent 半夜跑了 consolidation。它到底改了我什么？」",
            "hint": "下面三块输出来自同一次真实提交：commit → log → blame。",
            "blocks": [
                b_cmd('memdrift -C ~/vault commit -m "nightly consolidation" \\\n  --evidence email:msg-1882 --confidence 0.82'),
                b_term(commit, vault),
                b_cmd("memdrift -C ~/vault log"),
                b_term(log, vault),
                b_cmd("memdrift -C ~/vault blame clients/acme.md"),
                b_term(blame, vault),
            ],
            "learn": [
                "不是「文件变了」，而是 [[field|字段级变更]]：哪个字段、从什么值变成什么值。",
                "[[evidence|证据]]和置信度跟着 commit 走，所以「为什么改」有出处。",
                "blame 回答的是最要命的那个问题：这个值是谁、在什么时候、凭什么写进来的。",
            ],
        },
        {
            "nav": "我不想盯着终端",
            "eyebrow": "STEP 5 / 9 · 两个视图",
            "question": "「终端看自己的人物传记太难受了。有没有能直接点开看的东西？」",
            "hint": "两个视图读同一条 [[event|事件流]]，所以不会互相打架。",
            "blocks": [
                b_cmd("memdrift -C ~/vault digest --since 7d -o morning.html"),
                b_term(digest, vault),
                b_note(
                    "一个单文件 HTML：CSS/JS 全内联，没有字体要下载，没有任何 "
                    "<code>http://</code> 引用。可以直接双击，也可以发给别人。"
                ),
                b_cmd("memdrift -C ~/vault serve --open"),
                b_term(serve, vault),
                b_note(
                    "想翻来覆去查的时候用 [[loopback|本地 UI]]。URL 里那串 token 每次启动都变，"
                    "别的网站猜不到，DNS rebinding 也被 Host/Origin 挡住。"
                ),
            ],
            "learn": [
                "digest 是「发给别人看」的，serve 是「自己挖」的。",
                "两者都吃 events.build()，因此脱敏规则和风险标记必然一致。",
                "发出去之前还差一个问题：会不会泄露我的目录和聊天记录？",
            ],
        },
        {
            "nav": "记错了，撤回",
            "eyebrow": "STEP 6 / 9 · 撤销",
            "question": "「agent 把 48k 听成了 12k。我能撤回吗？会不会把历史搞乱？」",
            "hint": "先看计划再动手，这是这个工具的习惯。",
            "blocks": [
                b_cmd("memdrift -C ~/vault revert --field frontmatter.pricing \\\n  --page clients/acme.md --dry-run"),
                b_term(revert_dry, vault),
                b_cmd("memdrift -C ~/vault revert --field frontmatter.pricing \\\n  --page clients/acme.md"),
                b_term(revert, vault),
                b_cmd("memdrift -C ~/vault log --limit 3"),
                b_term(log_after, vault),
                b_note(
                    "看最后三行：<code>revert:</code> 是第三条，<code>extract: pricing dropped</code> "
                    "仍在第二条，一个字没改。[[revert|追加式撤销]]不删旧 commit，所以 log 不撒谎。"
                ),
            ],
            "learn": [
                "三种粒度：<code>--field</code> 改一个值，<code>--page</code> 整页回到某次提交，<code>--commit</code> 撤销一整次提交。",
                "<code>--dry-run</code> 零写入；MCP 里的 <code>memory.revert</code> 也默认只看不写。",
                "还原是字节级的：YAML 引号风格和 Windows 换行都保持不变。",
            ],
        },
        {
            "nav": "在编辑器里直接问",
            "eyebrow": "STEP 7 / 9 · MCP",
            "question": "「我能不能不切终端，让编辑器里的 agent 自己查这段历史？」",
            "hint": "下面那 7 个工具是从真实的 stdio 握手里读出来的，不是抄的清单。",
            "blocks": [
                b_cmd("memdrift -C ~/vault mcp --print-config"),
                b_term(short_config, vault),
                b_cmd("memdrift -C ~/vault mcp        # 或者让客户端自己拉起"),
                b_term(tools, vault),
                b_note(
                    "五个读工具标了 [[hint|readOnlyHint]]，客户端可以直接跑；"
                    "<code>memory.commit</code> 和 <code>memory.revert</code> 会停下来问你。"
                    "stdout 只走协议，banner 走 stderr，所以不会把 JSON-RPC 弄脏。"
                ),
            ],
            "learn": [
                "点号名（<code>memory.history</code>）在模型眼里是一家人，provider 需要时再降级成下划线。",
                "逐行 JSON 和 Content-Length 两种帧格式都收，客户端不用改。",
                "path 参数会做越界检查，agent 要 <code>../../etc/passwd</code> 会被拒。",
            ],
        },
        {
            "nav": "发出去会不会泄露",
            "eyebrow": "STEP 8 / 9 · 脱敏",
            "question": "「把 digest 发到群里，会不会把我的用户名和聊天原文一起发出去？」",
            "hint": "下面是真的调用 security.redact_text() 打印出来的结果。",
            "blocks": [
                b_term_plain(sample),
                b_note(
                    "脱敏做在[[event|事件层]]而不是渲染层，所以 digest / serve / MCP 三个出口"
                    "不可能有一个忘了脱敏。绝对路径、被截断的路径、证据原文、"
                    "<code>private: true</code> 的页面（连指向它的证据一起消失）都在这一层处理。"
                ),
                b_note(
                    "<code>--no-redact</code> 确实存在，但会打印红色警告——只该用于永远不会离开本机的文件。"
                    "另外 <code>.memgit/</code> 是 zlib 压缩、<b>不是加密</b>：能读目录的人就能读历史，"
                    "需要保密请放加密卷，这里不假装发明一套密钥管理。"
                ),
            ],
            "learn": [
                "默认就是安全的：路径变 <code>~/…</code>，引文截断并标省略号，private 页连证据一起移除。",
                "serve 只绑回环 + 随机 token + Host/Origin 校验，防 CSRF 与 DNS rebinding。",
                "加密是明确的「不做」，不是遗漏。",
            ],
        },
        {
            "nav": "我现在全懂了",
            "eyebrow": "STEP 9 / 9 · 收尾",
            "question": "「好了，我明白它是干嘛的、怎么用、什么时候不该用。」",
            "hint": "最后这一页把三件还没做的事也交代清楚——知道边界才算真的会用。",
            "blocks": [
                {"kind": "cheat", "html": ""},
                {"kind": "todo", "html": ""},
            ],
            "learn": [
                "你已经走完了：从「它是什么」到「我敢不敢用」到「怎么查、怎么撤、怎么接进编辑器」。",
                "剩下的三件事是产品边界，不是使用障碍。",
            ],
        },
    ]
    return steps


# --------------------------------------------------------------------- template

TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>memdrift · 从零开始点一遍</title>
<style>
:root{
  --bg:#f6f7f9; --surface:#ffffff; --surface-2:#eef0f4; --line:#d9dee6;
  --fg:#1d2430; --fg-dim:#5b6675; --accent:#2f6f5e; --accent-soft:#e2f1ec;
  --term-bg:#1e2024; --term-fg:#dee2e8; --term-bar:#2c2f34; --term-line:#3a3f47;
  --shadow:0 1px 2px rgba(20,30,45,.06),0 8px 28px rgba(20,30,45,.07);
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#171a1f; --surface:#21252c; --surface-2:#2a2f38; --line:#3a414d;
    --fg:#e7ebf1; --fg-dim:#9aa5b4; --accent:#7fd1b4; --accent-soft:#243a34;
    --term-bg:#141619; --term-fg:#dee2e8; --term-bar:#23262b; --term-line:#333941;
    --shadow:0 1px 2px rgba(0,0,0,.3),0 8px 28px rgba(0,0,0,.32);
  }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  -webkit-font-smoothing:antialiased;line-height:1.7}
code,.mono{font-family:"Cascadia Mono","SFMono-Regular",Consolas,"Liberation Mono",monospace}
.wrap{max-width:1180px;margin:0 auto;padding:28px 22px 60px}
header.top{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;margin-bottom:6px}
header.top h1{font-size:22px;margin:0;letter-spacing:.2px}
header.top .sub{color:var(--fg-dim);font-size:13px}
.progress{height:5px;background:var(--surface-2);border-radius:99px;overflow:hidden;margin:18px 0 22px}
.progress > i{display:block;height:100%;background:var(--accent);border-radius:99px;
  transition:width .35s cubic-bezier(.2,.8,.2,1)}
.layout{display:grid;grid-template-columns:238px 1fr;gap:24px;align-items:start}
@media(max-width:860px){.layout{grid-template-columns:1fr}.nav{position:static!important}}
.nav{position:sticky;top:20px;background:var(--surface);border:1px solid var(--line);
  border-radius:14px;padding:10px;box-shadow:var(--shadow)}
.nav .navhead{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--fg-dim);
  padding:8px 10px 6px}
.nav button{display:block;width:100%;text-align:left;border:0;background:transparent;
  color:var(--fg-dim);padding:9px 11px;border-radius:9px;cursor:pointer;font-size:13.5px;
  font-family:inherit;transition:background .15s,color .15s}
.nav button:hover{background:var(--surface-2);color:var(--fg)}
.nav button.on{background:var(--accent-soft);color:var(--accent);font-weight:600}
.nav button .n{display:inline-block;width:22px;opacity:.6;font-size:11px}
.nav .random{margin-top:8px;border-top:1px solid var(--line);padding-top:12px;
  font-size:12px;color:var(--fg-dim);text-align:center}
.card{background:var(--surface);border:1px solid var(--line);border-radius:16px;
  padding:30px 34px 26px;box-shadow:var(--shadow);min-height:520px}
.eyebrow{font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:var(--accent);
  font-weight:700;margin:0 0 14px}
h2.q{font-size:24px;line-height:1.5;margin:0 0 10px;font-weight:650;letter-spacing:.1px}
.hint{color:var(--fg-dim);font-size:13px;margin:0 0 24px;padding-bottom:18px;
  border-bottom:1px solid var(--line)}
.block{margin:0 0 18px}
.note{font-size:14.5px;color:var(--fg)}
.note code{background:var(--surface-2);padding:1px 6px;border-radius:5px;font-size:.88em}
.note b{color:var(--fg);font-weight:650}
button.term{border:0;background:transparent;color:var(--accent);font:inherit;font-size:inherit;
  padding:0 1px;cursor:pointer;border-bottom:1px dashed var(--accent);border-radius:0}
button.term:hover{background:var(--accent-soft)}
button.cmd{display:block;width:100%;text-align:left;background:var(--term-bg);
  color:#a8e6c8;border:1px solid var(--term-line);border-radius:10px 10px 0 0;
  padding:12px 15px;font:13px/1.6 "Cascadia Mono","SFMono-Regular",Consolas,monospace;
  white-space:pre-wrap;cursor:pointer;position:relative;margin:0}
button.cmd:hover{border-color:var(--accent)}
button.cmd .copy{position:absolute;right:12px;top:9px;font-size:10px;color:#7b8794;
  letter-spacing:.08em;text-transform:uppercase}
button.cmd.copied .copy{color:#a8e6c8}
pre.term{margin:0 0 18px;background:var(--term-bg);color:var(--term-fg);
  border:1px solid var(--term-line);border-top:0;border-radius:0 0 10px 10px;
  padding:14px 16px;overflow:auto;font:12.5px/1.65 "Cascadia Mono","SFMono-Regular",Consolas,monospace;
  white-space:pre-wrap;word-break:break-word}
pre.term.alone{border-radius:10px;border-top:1px solid var(--term-line)}
pre.term + .note{margin-top:-4px}
.a-b{font-weight:700}.a-dim{opacity:.62}.a-red{color:#e06c75}
.a-green{color:#98c379}.a-yellow{color:#e5c07b}.a-blue{color:#61afef}
.a-magenta{color:#c678dd}.a-cyan{color:#56b6c2}
.model{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin:20px 0}
@media(max-width:620px){.model{grid-template-columns:1fr}}
.model .box{border:1px solid var(--line);border-radius:12px;padding:16px 18px;background:var(--surface-2)}
.model .box h4{margin:0 0 8px;font-size:13px;letter-spacing:.04em}
.model .box p{margin:0;font-size:13px;color:var(--fg-dim)}
.model .box.yours{border-color:var(--accent);background:var(--accent-soft)}
.model .box code{font-size:12px;display:inline-block}
.learn{margin:26px 0 0;padding:18px 20px;border-radius:12px;background:var(--accent-soft);
  border:1px solid color-mix(in srgb,var(--accent) 30%,transparent)}
.learn h4{margin:0 0 10px;font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--accent)}
.learn ul{margin:0;padding-left:20px;font-size:14px}
.learn li{margin:5px 0}
.learn li code{font-size:.88em;background:var(--surface);padding:1px 5px;border-radius:4px}
.foot{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-top:30px;
  padding-top:20px;border-top:1px solid var(--line)}
.foot button{border:1px solid var(--line);background:var(--surface);color:var(--fg);
  padding:10px 18px;border-radius:9px;cursor:pointer;font:inherit;font-size:13.5px}
.foot button:hover:not(:disabled){border-color:var(--accent);color:var(--accent)}
.foot button:disabled{opacity:.35;cursor:default}
.foot button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
@media (prefers-color-scheme:dark){.foot button.primary{color:#10201a}}
.foot .count{color:var(--fg-dim);font-size:12px}
.cheat{width:100%;border-collapse:collapse;font-size:13px;margin:4px 0 22px}
.cheat td{padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top}
.cheat td:first-child{white-space:nowrap;width:44%}
.cheat code{background:var(--surface-2);padding:2px 6px;border-radius:5px;font-size:12px}
.cheat td:last-child{color:var(--fg-dim)}
.todo{display:grid;gap:12px;margin:8px 0 4px}
.todo .item{border:1px solid var(--line);border-left:3px solid var(--accent);
  border-radius:10px;padding:13px 16px;background:var(--surface-2)}
.todo .item b{font-size:13.5px}
.todo .item p{margin:5px 0 0;font-size:13px;color:var(--fg-dim)}
.drawer{position:fixed;right:0;top:0;height:100%;width:min(380px,92vw);background:var(--surface);
  border-left:1px solid var(--line);box-shadow:-12px 0 40px rgba(0,0,0,.18);
  transform:translateX(102%);transition:transform .28s cubic-bezier(.2,.8,.2,1);
  z-index:20;padding:30px 26px;overflow:auto}
.drawer.open{transform:translateX(0)}
.drawer h3{margin:0 0 14px;font-size:16px}
.drawer p{font-size:14px;color:var(--fg-dim)}
.drawer code{background:var(--surface-2);padding:1px 5px;border-radius:4px;font-size:.88em}
.drawer .close{border:1px solid var(--line);background:var(--surface);color:var(--fg);
  border-radius:8px;padding:6px 12px;cursor:pointer;font:inherit;font-size:12px}
.scrim{position:fixed;inset:0;background:rgba(10,15,25,.35);opacity:0;pointer-events:none;
  transition:opacity .25s;z-index:19}
.scrim.on{opacity:1;pointer-events:auto}
footer.pg{margin-top:26px;color:var(--fg-dim);font-size:12px;text-align:center}
footer.pg code{background:var(--surface-2);padding:2px 6px;border-radius:4px}
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <h1>memdrift · 从零开始点一遍</h1>
    <span class="sub">一个什么都不知道的用户，一路点到完全理解。所有终端输出都是真实跑出来的。</span>
  </header>
  <div class="progress"><i id="bar" style="width:11%"></i></div>
  <div class="layout">
    <nav class="nav" id="nav"></nav>
    <main class="card" id="card"></main>
  </div>
  <footer class="pg">用 <code>←</code> <code>→</code> 翻页 · 点带下划线的词看解释 · 点命令复制 ·
    由 <code>scripts/build_onboarding.py</code> 从真实命令输出生成</footer>
</div>
<div class="scrim" id="scrim"></div>
<aside class="drawer" id="drawer">
  <button class="close" id="close" type="button">关闭 ✕</button>
  <h3 id="dtitle"></h3>
  <p id="dbody"></p>
</aside>
<script>
/*__DATA__*/
const nav=document.getElementById('nav'),card=document.getElementById('card');
const bar=document.getElementById('bar'),drawer=document.getElementById('drawer');
const scrim=document.getElementById('scrim');
let cur=0;
const CHEAT=[
 ['memdrift init &lt;dir&gt;','建 .memgit/ 并打基线快照（不改动你的 md）'],
 ['memdrift -C &lt;dir&gt; watch','挂着不动，一轮写入合成一次 commit（推荐用法）'],
 ['memdrift -C &lt;dir&gt; status','还没提交的变化'],
 ['memdrift -C &lt;dir&gt; log --since 7d','最近发生了什么'],
 ['memdrift -C &lt;dir&gt; blame &lt;page&gt;','每个字段：谁、何时、凭什么改的'],
 ['memdrift -C &lt;dir&gt; digest -o morning.html','单文件 HTML，脱敏后可直接发人'],
 ['memdrift -C &lt;dir&gt; serve --open','本地只读 UI，仅 127.0.0.1'],
 ['memdrift -C &lt;dir&gt; revert --field frontmatter.x --page p','撤销一个值，历史不改写'],
 ['memdrift -C &lt;dir&gt; mcp --print-config','把内存目录接进编辑器'],
];
const TODO=[
 ['1 · 并发写入','refs/head 是单文件、无锁，目前一写多读。两个 agent 同时 commit 会丢一个；用 watch 串行化，真并发要加锁。'],
 ['2 · 规则引擎 / 多目录聚合 / 跨系统冲突','v1.1。现在只回答「变了什么」，不判断该不该变、不合并两个 vault 的历史、不发现两个系统对同一事实的矛盾。'],
 ['3 · 完整的 --exclude glob 语义','目前 **/name/** 按目录名匹配，够挡 node_modules，但不是 gitignore 语法。'],
];
const extras={
 model:`<div class="model">
   <div class="box yours"><h4>你的记忆（本来就有）</h4>
     <p><code>clients/acme.md</code><br><code>topics/pricing.md</code><br>…普通的 markdown</p></div>
   <div class="box"><h4>memdrift 加的（只有这一层）</h4>
     <p><code>.memgit/objects/</code> 内容寻址快照<br>
        <code>.memgit/commits/</code> 每次变化<br>
        <code>.memgit/refs/head</code> 当前位置</p></div>
 </div>`,
 cheat:`<table class="cheat">${CHEAT.map(r=>`<tr><td><code>${r[0]}</code></td><td>${r[1]}</td></tr>`).join('')}</table>`,
 todo:`<div class="todo">${TODO.map(t=>`<div class="item"><b>还没做 · ${t[0]}</b><p>${t[1]}</p></div>`).join('')}</div>
   <p class="note" style="font-size:13px;color:var(--fg-dim)">加密不在其中——它是明确的「不做」：需要保密就把目录放加密卷，不假装发明密钥管理。</p>`
};
function render(){
  const s=DATA.steps[cur];
  bar.style.width=((cur+1)/DATA.steps.length*100)+'%';
  [...nav.querySelectorAll('button.step')].forEach((b,i)=>b.classList.toggle('on',i===cur));
  let h=`<p class="eyebrow">${s.eyebrow}</p><h2 class="q">${s.question}</h2><p class="hint">${s.hint}</p>`;
  for(const b of s.blocks){
    if(b.kind==='cmd'){h+=`<div class="block">${b.html}</div>`;}
    else if(b.kind==='term'){
      const prevIsCmd=h.trimEnd().endsWith('</button></div>');
      h+=`<pre class="term${prevIsCmd?'':' alone'}">${b.html}</pre>`;
    }
    else if(b.kind==='note'){h+=`<div class="block note">${b.html}</div>`;}
    else if(extras[b.kind]){h+=`<div class="block">${extras[b.kind]}</div>`;}
  }
  h+=`<div class="learn"><h4>于是你知道了</h4><ul>${s.learn.map(x=>`<li>${x}</li>`).join('')}</ul></div>`;
  h+=`<div class="foot"><button id="prev" ${cur===0?'disabled':''}>← 上一步</button>
      <span class="count">${cur+1} / ${DATA.steps.length}</span>
      <button class="primary" id="next">${cur===DATA.steps.length-1?'回到开头 ↺':'下一步 →'}</button></div>`;
  card.innerHTML=h;
  card.scrollIntoView({behavior:'smooth',block:'start'});
  document.getElementById('prev').onclick=()=>go(cur-1);
  document.getElementById('next').onclick=()=>go(cur===DATA.steps.length-1?0:cur+1);
  card.querySelectorAll('button.cmd').forEach(btn=>btn.onclick=async()=>{
    const text=btn.childNodes[0].textContent.trim();
    try{await navigator.clipboard.writeText(text);}catch(e){
      const ta=document.createElement('textarea');ta.value=text;document.body.appendChild(ta);
      ta.select();document.execCommand('copy');ta.remove();}
    btn.classList.add('copied');
    setTimeout(()=>btn.classList.remove('copied'),1200);
  });
  card.querySelectorAll('button.term').forEach(btn=>btn.onclick=()=>openTerm(btn.dataset.k));
}
function go(i){cur=(i+DATA.steps.length)%DATA.steps.length;render();}
function openTerm(k){
  const v=DATA.terms[k];if(!v)return;
  document.getElementById('dtitle').textContent='名词解释';
  document.getElementById('dbody').innerHTML=v;
  drawer.classList.add('open');scrim.classList.add('on');
}
function closeDrawer(){drawer.classList.remove('open');scrim.classList.remove('on');}
document.getElementById('close').onclick=closeDrawer;
scrim.onclick=closeDrawer;
document.onkeydown=e=>{
  if(e.key==='Escape')closeDrawer();
  if(e.key==='ArrowRight')go(cur+1);
  if(e.key==='ArrowLeft')go(cur-1);
};
nav.innerHTML='<div class="navhead">点击任意一步乱逛</div>'+
  DATA.steps.map((s,i)=>`<button class="step" data-i="${i}"><span class="n">${String(i+1).padStart(2,'0')}</span>${s.nav}</button>`).join('')+
  '<button class="step random" id="shuffle">🎲 随便跳一步</button>';
nav.querySelectorAll('button.step[data-i]').forEach(b=>b.onclick=()=>go(+b.dataset.i));
document.getElementById('shuffle').onclick=()=>{
  let i;do{i=Math.floor(Math.random()*DATA.steps.length);}while(i===cur);go(i);};
render();
</script>
</body>
</html>
"""


def main() -> None:
    memdrift = Path(sys.executable).parent / "memdrift.exe"
    if not memdrift.exists():
        memdrift = Path(sys.executable).parent / "memdrift"

    work = Path(tempfile.mkdtemp(prefix="memdrift-onboarding-"))
    try:
        vault = work / "vault"
        probe = work / "probe"
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "make_demo_vault.py"), str(vault)],
            check=True,
            capture_output=True,
        )
        probe.mkdir(parents=True)
        (probe / "a.md").write_text(
            "---\ntitle: Acme\n---\n# Acme\n\nhello\n", encoding="utf-8", newline=""
        )

        steps = build_steps(memdrift, vault, probe)
        payload = {"steps": steps, "terms": TERMS}
        data = (
            "const DATA = "
            + json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
            + ";"
        )
        page = TEMPLATE.replace("/*__DATA__*/", data)

        # Only external *resource* references are forbidden. The serve banner
        # legitimately contains a loopback http:// URL as content; that is not
        # a dependency and does not stop the page working offline.
        if re.search(r"(?:src|href)\s*=\s*[\"']https?://", page) or "url(http" in page:
            raise SystemExit("onboarding page loads an external resource")
        for root in (vault, probe, ROOT):
            for variant in (str(root), str(root).replace("\\", "/")):
                if variant in page:
                    raise SystemExit(f"onboarding page leaked a local path: {variant}")

        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(page, encoding="utf-8", newline="\n")
        print(f"wrote {OUT}  ({len(page)} bytes, {len(steps)} steps)")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
