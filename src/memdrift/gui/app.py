"""The single page ``memdrift ui`` serves. Three strings, zero external files.

No framework and no build step, for the same reason the digest is one file:
the page has to work offline, from a loopback server, under a CSP that allows
nothing but its own inline style and script. It reuses the digest's
stylesheet, so the two views share one look — and one dark mode.

Two rules hold the whole thing together:

* Every dynamic string goes through ``esc()`` before it touches ``innerHTML``.
  A memory page whose title is an HTML tag renders as text.
* Nothing is written on a click. A write is always: ask the server for a plan,
  show the plan, and only then let the human confirm — see ``undo()``.

The page keeps no state on its own behalf. What it displays is what the API
returned; what it confirms is what it displayed.
"""

from __future__ import annotations

from ..digest import CSS

#: The page's own rules, layered on the digest's so the theme (and dark mode)
#: comes from one place. The layout is a two-pane app: list on the left, the
#: selected change and its evidence on the right.
STYLE = CSS + """
html, body { height: 100%; }
body { margin: 0; padding: 0; overflow: hidden; }
.app { display: grid; grid-template-columns: minmax(0, 1fr) 380px;
       grid-template-rows: auto minmax(0, 1fr); height: 100vh; }
header.top { display: flex; align-items: center; gap: 14px; padding: 0 20px;
             height: 52px; border-bottom: 1px solid var(--line);
             margin: 0; background: var(--bg); grid-column: 1 / -1; }
.brand { font-weight: 700; font-size: 15px; letter-spacing: -0.01em; }
.vault { color: var(--faint); font-size: 12.5px; max-width: 260px;
         overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tabs { display: flex; gap: 2px; margin-left: 6px; }
.tabs button { font: inherit; font-size: 13.5px; padding: 6px 12px; border: 0;
               border-radius: 7px; background: transparent; color: var(--muted);
               cursor: pointer; }
.tabs button:hover { background: var(--wash); color: var(--ink); }
.tabs button.on { background: var(--wash); color: var(--ink); font-weight: 600; }
.tabs button:focus-visible, .row:focus-visible, .btn:focus-visible {
  outline: 2px solid var(--accent); outline-offset: -2px; }
.grow { flex: 1; }
main { overflow: auto; padding: 22px 26px 90px; }
aside { border-left: 1px solid var(--line); overflow: auto; padding: 22px 22px 50px; }
.busy { opacity: 0.55; pointer-events: none; transition: opacity 0.12s; }
h2.pane { font-size: 14px; margin: 0 0 12px; text-transform: none; letter-spacing: 0;
          font-family: var(--mono); font-weight: 600; color: var(--ink); word-break: break-all; }
.bar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: 0 0 16px; }
.bar h1 { font-size: 20px; margin: 0; }
.seg { display: inline-flex; border: 1px solid var(--line); border-radius: 999px; overflow: hidden; }
.seg button { font: inherit; font-size: 12.5px; padding: 5px 13px; border: 0;
              background: var(--bg); color: var(--muted); cursor: pointer; }
.seg button + button { border-left: 1px solid var(--line); }
.seg button.on { background: var(--ink); color: var(--bg); }
.sub2 { color: var(--muted); font-size: 13px; }
.mono { font-family: var(--mono); }
button.row { font: inherit; color: inherit; text-align: left; width: 100%;
             background: transparent; border: 0; border-radius: 0;
             cursor: pointer; appearance: none; }
.row { grid-template-columns: minmax(120px, 200px) minmax(90px, 170px) minmax(0, 1fr); gap: 14px; align-items: baseline; }
.row.sel, .row.sel:hover { background: var(--wash); box-shadow: inset 3px 0 0 var(--accent); }
.row .pt { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.row .fd { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.row .val { font-family: var(--mono); font-size: 12.5px; overflow-wrap: anywhere; }
.row.pg .val { font-family: var(--sans); font-size: 13.5px; font-weight: 550; }
.row .old { color: var(--faint); text-decoration: line-through; text-decoration-thickness: 1px; }
.row .arw { color: var(--faint); margin: 0 7px; white-space: nowrap; }
.row .new { font-weight: 550; }
.pill { display: inline-block; font-size: 10.5px; text-transform: uppercase;
        letter-spacing: 0.05em; padding: 1px 7px; border-radius: 3px; font-weight: 600;
        background: var(--wash); color: var(--muted); border: 1px solid var(--line); }
.pill.agent { color: var(--accent); border-color: var(--accent); }
.pill.human { color: var(--ok); border-color: var(--ok); }
.tag { margin: 0 0 0 7px; }
.pill + .sub2, .chead .sub2 { color: var(--faint); }
.notice { border: 1px solid var(--line); border-left: 3px solid var(--accent); border-radius: 6px;
          padding: 10px 14px; margin: 0 0 14px; font-size: 13.5px; color: var(--ink); background: var(--bg); }
.notice.warn { border-left-color: var(--risky); background: var(--risky-wash); }
.notice.ok { border-left-color: var(--ok); }
.box { border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; margin: 0 0 14px;
       background: var(--wash); font-size: 13.5px; overflow-wrap: anywhere; }
.box.confirm { border-color: var(--risky); background: var(--risky-wash); }
.box .hint { margin: 8px 0; }
/* The after-commit offer and the undo confirmation are dialogs, not toasts:
   both of them change the commit chain, so neither may fade away unseen. */
.modal { position: fixed; inset: 0; display: flex; align-items: center; justify-content: center;
         background: rgba(20, 22, 26, 0.42); z-index: 50; padding: 24px; }
.modal .sheet { background: var(--bg); border: 1px solid var(--line); border-radius: 12px;
                padding: 20px 22px; width: min(620px, 100%); max-height: 80vh; overflow: auto;
                box-shadow: 0 18px 50px rgba(0, 0, 0, 0.28); font-size: 13.5px; }
.modal .sheet h3 { margin: 0 0 6px; font-size: 15.5px; }
.modal .sheet .hint { color: var(--muted); margin: 0 0 12px; }
.modal .sheet ul { margin: 0 0 14px; padding-left: 18px; }
.modal .sheet li { margin: 4px 0; overflow-wrap: anywhere; }
.modal .sheet .btnrow { justify-content: flex-end; }
.modal .oldid { font-family: var(--mono); }
.rev { margin-left: 7px; font-size: 11.5px; color: var(--accent);
       border: 1px solid var(--line); border-radius: 999px; padding: 1px 7px; }
.dl { display: grid; grid-template-columns: auto 1fr; gap: 6px 14px; margin: 0 0 14px; font-size: 13px; }
.dl dt { color: var(--faint); }
.dl dd { margin: 0; overflow-wrap: anywhere; }
.evlist { margin: 0 0 14px; padding: 0; list-style: none; font-size: 12.5px; }
.evlist li { padding: 7px 0; border-top: 1px solid var(--line); }
.evlist .q { display: block; margin-top: 4px; padding-left: 9px;
             border-left: 2px solid var(--line); font-style: italic; color: var(--muted); }
.btn { font: inherit; font-size: 13px; padding: 7px 14px; border-radius: 7px;
       border: 1px solid var(--line); background: var(--bg); color: var(--ink);
       cursor: pointer; text-decoration: none; }
.btn:hover { background: var(--wash); }
.btn.primary { background: var(--ink); color: var(--bg); border-color: var(--ink); }
.btn.primary:hover { filter: brightness(1.12); }
.btn.danger { color: var(--risky); border-color: var(--risky); }
.btn.danger:hover { background: var(--risky-wash); }
.btnrow { display: flex; gap: 8px; margin-top: 4px; }
input[type=text] { font: inherit; font-size: 13.5px; padding: 7px 10px; flex: 1; min-width: 0;
                   border: 1px solid var(--line); border-radius: 7px;
                   background: var(--bg); color: var(--ink); }
input[type=text]:focus { outline: 2px solid var(--accent); outline-offset: 1px; }
.cfg { font-family: var(--mono); font-size: 12.5px; line-height: 1.6; padding: 14px; margin: 0 0 12px;
       border: 1px solid var(--line); border-radius: 8px; background: var(--wash);
       overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; user-select: all; }
.hint { color: var(--muted); font-size: 13.5px; line-height: 1.6; margin: 0 0 14px; }
.pl { margin: 0; padding: 0; list-style: none; }
.pl li { padding: 2px 0; font-family: var(--mono); font-size: 12.5px; }
.link { font: inherit; font-size: 13px; border: 0; background: none; color: var(--accent);
        cursor: pointer; padding: 0; }
@media (max-width: 1180px) {
  /* Three columns stop fitting before the two-pane layout does. Values get
     their own line instead of being squeezed into a 4-pixel column. */
  .row { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }
  .row .val { grid-column: 1 / -1; }
}
@media (max-width: 960px) {
  body { overflow: auto; }
  .app { grid-template-columns: 1fr; grid-template-rows: auto auto auto; height: auto; }
  aside { border-left: 0; border-top: 1px solid var(--line); }
  .vault { display: none; }
  main { padding: 16px 14px 40px; }
}
@media (max-width: 620px) {
  .row { grid-template-columns: 1fr; gap: 3px; }
  .chead { gap: 8px; }
}
"""

#: Static skeleton. Everything inside ``main`` and ``aside`` is rendered by
#: the script from API responses.
BODY = """\
<div class="app">
<header class="top">
<span class="brand">memdrift</span>
<span class="vault mono" id="vault"></span>
<nav class="tabs" id="nav" aria-label="views"></nav>
<span class="grow"></span>
<button type="button" class="chip" id="lang" title="中文 / EN">EN</button>
</header>
<main id="main"></main>
<aside id="side"></aside>
</div>
<div id="modal"></div>
"""

SCRIPT = r"""
'use strict';
(function () {
  const L = {
    zh: {
      timeline: '时间线', pages: '页面', digest: '日报', connect: '接入编辑器',
      w24h: '24 小时', w7d: '7 天', wall: '全部',
      sum: (c, n) => c + ' 次提交 · ' + n + ' 处变化',
      risk: k => k + ' 处建议复核', allclear: '没有需要紧张的改动',
      unsure: k => '有 ' + k + ' 次提交的置信度偏低，下面的变化来自这些提交。',
      hidden: k => k + ' 个标记为 private 的页面没有显示。',
      empty: '这个时间窗里没有变化。',
      nohist: '还没有提交。先运行 memdrift init，或 memdrift commit -m "…"。',
      more: n => '还有 ' + n + ' 处没有显示，请缩小时间窗。',
      onlyRisk: '只看需复核',
      pending: n => n + ' 处修改还没提交：',
      why: '这些改动是因为…（必填）', commit: '提交',
      add: '新增页面', del: '删除页面', ren: '重命名',
      hint: '在左边点一条变化，这里会显示：它原来是什么、谁改的、依据是哪条证据。',
      body: '正文', review: '建议复核',
      when: '何时', who: '谁', what: '哪次提交', why_: '提交说明', conf: '置信度',
      evidence: '证据', none: '（无）', agent: 'agent', human: '人',
      undo: '撤销这一处', confirm: '确认撤销？',
      confirmBody: '会新增一个 commit，历史不改写，原来那次提交仍然保留。',
      restore: '恢复为', remove: '移除这个字段（它是由这次提交引入的）',
      yes: '确认撤销', no: '先不动',
      done: id => '已撤销，记为 commit ' + id + '。',
      did: id => '已提交，记为 commit ' + id + '。',
      undoTitle: (id, n) => '已提交 ' + id + ' · ' + n + ' 处变化',
      undoOfferBody: id => '撤销会再新增一个 commit，' + id + ' 仍然留在历史里，追溯不会断。',
      undoCommit: '撤销这次提交', keepIt: '留着',
      undoCommitTitle: id => '撤销 ' + id + '？',
      undoCommitBody: id => '会新增一个 commit 把这次改动改回去，' + id + ' 本身不删除、不改写。',
      undoWhat: '这次提交改了：',
      undone: (n, o) => '已撤销：新 commit ' + n + ' 记录了 reverts ' + o + '。',
      undoNoParent: '这是第一次提交，前面没有可恢复的状态。',
      undoNothing: '这次提交没有可以在这里撤销的变化。',
      reverts: id => '撤销了 ' + id,
      readonly: '只读空间：这次安装可以看这个目录，但不能改。写入会被拒绝。',
      conflict: '这一处在你查看之后变了。列表已刷新，请重新确认。',
      cannot: '页面的新增、删除、重命名不在这里撤销。请用命令行：memdrift revert --commit',
      loop: '只在本机可用。要分享请导出日报，不要分享这个地址。',
      loadErr: '加载失败：', failed: '没有成功：',
      allpages: '← 所有页面', lastChange: '每个字段的最后一次变化',
      digestTitle: '导出一份日报',
      digestBody: '一个单文件 HTML，已脱敏，可以直接发给别人。时间窗：',
      download: '下载 digest',
      connectTitle: '把历史接进编辑器',
      connectBody: '把这段配置贴进支持 MCP 的客户端（Cursor、Claude Desktop、Zed 等）。7 个工具里 5 个只读，2 个写入会先征求确认。',
      copy: '复制', copied: '已复制',
      loading: '加载中…',
      now: '刚刚', agoMin: n => n + ' 分钟前', agoH: n => n + ' 小时前', agoD: n => n + ' 天前', uncommitted: '未提交'
    },
    en: {
      timeline: 'Timeline', pages: 'Pages', digest: 'Digest', connect: 'Connect editor',
      w24h: '24h', w7d: '7d', wall: 'All',
      sum: (c, n) => c + ' commit' + (c === 1 ? '' : 's') + ' · ' + n + ' change' + (n === 1 ? '' : 's'),
      risk: k => k + ' need review', allclear: 'nothing to worry about',
      unsure: k => k + ' commit' + (k === 1 ? ' has' : 's have') + ' low confidence; the changes below come from them.',
      hidden: k => k + ' page' + (k === 1 ? ' is' : 's are') + ' marked private and not shown.',
      empty: 'No changes in this window.',
      nohist: 'Nothing committed yet. Run memdrift init, or memdrift commit -m "…".',
      more: n => n + ' more not shown — narrow the window.',
      onlyRisk: 'Needs review only',
      pending: n => n + ' uncommitted change' + (n === 1 ? '' : 's') + ':',
      why: 'Why did these change… (required)', commit: 'Commit',
      add: 'new page', del: 'page deleted', ren: 'renamed',
      hint: 'Pick a change on the left. This pane shows what it was before, who changed it, and the evidence behind it.',
      body: 'body', review: 'review',
      when: 'when', who: 'who', what: 'commit', why_: 'message', conf: 'confidence',
      evidence: 'evidence', none: '(none)', agent: 'agent', human: 'human',
      undo: 'Undo this change', confirm: 'Undo this?',
      confirmBody: 'A new commit is added; history is not rewritten. The original commit stays.',
      restore: 'restore to', remove: 'remove this field (it was introduced by that commit)',
      yes: 'Undo', no: 'Leave it',
      done: id => 'Undone as commit ' + id + '.',
      did: id => 'Committed as ' + id + '.',
      undoTitle: (id, n) => 'Committed ' + id + ' · ' + n + ' change' + (n === 1 ? '' : 's'),
      undoOfferBody: id => 'Undoing adds another commit; ' + id + ' stays in history, so the trail is not broken.',
      undoCommit: 'Undo this commit', keepIt: 'Keep it',
      undoCommitTitle: id => 'Undo ' + id + '?',
      undoCommitBody: id => 'A new commit puts the values back. ' + id + ' itself is not deleted or rewritten.',
      undoWhat: 'This commit changed:',
      undone: (n, o) => 'Undone: new commit ' + n + ' records reverts ' + o + '.',
      undoNoParent: 'This is the first commit — there is nothing before it to restore.',
      undoNothing: 'Nothing in this commit can be undone here.',
      reverts: id => 'undoes ' + id,
      readonly: 'Read-only space: this install may look here, not change it. Writes are refused.',
      conflict: 'This changed after you looked at it. The list was refreshed — check it again.',
      cannot: 'Page adds, deletes and renames are not undone here. Use: memdrift revert --commit',
      loop: 'Local only. To share, export a digest — not this address.',
      loadErr: 'Failed to load: ', failed: 'Did not succeed: ',
      allpages: '← All pages', lastChange: 'Last change per field',
      digestTitle: 'Export a digest',
      digestBody: 'One self-contained HTML file, redacted, safe to send. Window: ',
      download: 'Download digest',
      connectTitle: 'Bring the history into your editor',
      connectBody: 'Paste this into any MCP client (Cursor, Claude Desktop, Zed…). 5 of the 7 tools are read-only; the 2 writers ask first.',
      copy: 'Copy', copied: 'Copied',
      loading: 'Loading…',
      now: 'just now', agoMin: n => n + ' min ago', agoH: n => n + ' h ago', agoD: n => n + ' d ago', uncommitted: 'uncommitted'
    }
  };
  const VIEWS = [['timeline', 'timeline'], ['pages', 'pages'], ['digest', 'digest'], ['connect', 'connect']];

  // ------------------------------------------------------------ helpers
  let lang = 'en';
  try {
    lang = localStorage.getItem('memdrift.lang') ||
      ((navigator.language || '').toLowerCase().indexOf('zh') === 0 ? 'zh' : 'en');
  } catch (err) {}
  const t = k => L[lang][k];
  const T = new URLSearchParams(location.search).get('t') || '';
  const $ = id => document.getElementById(id);
  const esc = v => (v == null ? '' : String(v)).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const clip = (v, n) => { const s = v == null ? '' : String(v); return esc(s.length > n ? s.slice(0, n - 1) + '…' : s); };
  const val = (v, n) => (v === null || v === undefined || v === '' ? '<span class="sub2">∅</span>' : clip(v, n));
  const pair = (b, a, n) => '<span class="old">' + val(b, n) + '</span><span class="arw">→</span><span class="new">' + val(a, n) + '</span>';
  const EMPTY = '<span class="sub2">∅</span>';

  function ago(iso) {
    if (!iso) return esc(t('uncommitted'));
    const d = new Date(iso);
    if (isNaN(d.getTime())) return esc(iso);
    const s = Math.max(0, (Date.now() - d.getTime()) / 1000);
    if (s < 60) return esc(t('now'));
    if (s < 3600) return esc(t('agoMin')(Math.floor(s / 60)));
    if (s < 86400) return esc(t('agoH')(Math.floor(s / 3600)));
    if (s < 86400 * 7) return esc(t('agoD')(Math.floor(s / 86400)));
    return esc(new Intl.DateTimeFormat(lang === 'zh' ? 'zh-CN' : 'en-CA', { year: 'numeric', month: '2-digit', day: '2-digit' }).format(d));
  }
  function fieldLabel(f) {
    if (!f) return '';
    if (f === 'body') return t('body');
    if (f.indexOf('section:') === 0) return '§ ' + f.slice(8);
    if (f.indexOf('frontmatter.') === 0) return f.slice(12);
    return f;
  }
  const OPS = { add: 'add', delete: 'del', rename: 'ren' };
  function who(it) {
    const kind = it.authorType === 'agent' ? t('agent') : t('human');
    return kind + ': ' + (it.author || '?');
  }

  // ------------------------------------------------------------ state
  let since = '7d', view = 'timeline', riskOnly = false;
  let st = null;            // the last /api/state answer, untouched
  let pagePath = null, fieldList = [];
  let sel = null;           // one normalised change the human is looking at
  let mode = 'show', plan = null;
  let lastCommit = null;    // {id, changes, canUndo} — the commit to offer undo for
  let undoPlan = null;      // the dry-run plan for undoing that commit
  let note = null, busy = false;
  let config = '', configLoading = false;
  let commitOpen = false, commitMsg = '';
  let REG = [];             // rows rendered in the current frame, by index

  async function api(path, opt) {
    opt = opt || {};
    const url = path + (path.indexOf('?') >= 0 ? '&' : '?') + 't=' + encodeURIComponent(T);
    const init = { method: opt.body ? 'POST' : 'GET' };
    if (opt.body) {
      init.body = JSON.stringify(opt.body);
      init.headers = { 'Content-Type': 'application/json', 'X-Memdrift': 'ui' };
    }
    let res;
    try { res = await fetch(url, init); }
    catch (err) { throw new Error(String(err)); }
    let data = null;
    try { data = await res.json(); } catch (err) {}
    if (!res.ok) {
      const e = new Error((data && data.error) || ('HTTP ' + res.status));
      e.status = res.status;
      throw e;
    }
    return data;
  }

  async function load() {
    busy = true; render();
    try {
      st = await api('api/state?since=' + encodeURIComponent(since));
    } catch (err) {
      st = null;
      note = { kind: 'warn', text: t('loadErr') + err.message };
    }
    busy = false; render();
  }

  // --------------------------------------------------------- normalise
  // A timeline event and a blame entry describe the same thing with different
  // field names. One shape here means one detail pane, and one undo path.
  function fromEvent(ev) {
    return {
      path: ev.path, field: ev.op === 'modify' ? (ev.field || '') : '', op: ev.op, from: ev.from,
      before: ev.before, after: ev.after, ts: ev.ts, commit: ev.short || '',
      author: (ev.author && ev.author.name) || '', authorType: (ev.author && ev.author.type) || '',
      message: ev.message || '', confidence: ev.confidence, evidence: ev.evidence || [],
      risk: !!ev.risk, canUndo: ev.op === 'modify' && !!ev.field, reverts: ev.reverts_short || ''
    };
  }
  function fromField(f, path) {
    return {
      path: path, field: f.op === 'modify' ? (f.field || '') : '', op: f.op, from: null,
      before: f.before, after: f.after, ts: f.ts, commit: f.commit || '',
      author: typeof f.author === 'string' ? f.author : '', authorType: f.author_type || '',
      message: f.message || '', confidence: f.confidence, evidence: f.evidence || [],
      risk: f.op === 'delete', canUndo: f.op === 'modify' && !!f.field
    };
  }
  const sameKey = (a, b) => a.commit === b.commit && a.path === b.path && a.field === b.field;

  // ----------------------------------------------------------- pieces
  const seg = () => '<span class="seg">' + [['24h', 'w24h'], ['7d', 'w7d'], ['', 'wall']]
    .map(w => '<button type="button" data-act="since" data-v="' + w[0] + '" class="' + (since === w[0] ? 'on' : '') + '">' + esc(t(w[1])) + '</button>').join('') + '</span>';

  function summary() {
    if (!st) return '';
    const head = t('sum')(st.commits, st.changes);
    if (st.risky) return head + ' · ' + t('risk')(st.risky);
    return st.changes ? head + ' · ' + t('allclear') : '';
  }
  function opText(it) {
    if (it.op === 'rename') return '<span class="old">' + esc(it.from) + '</span><span class="arw">→</span><span class="new">' + esc(it.path) + '</span>';
    return esc(t(OPS[it.op] || it.op));
  }
  function roundOff(it) {
    // An undo commit points back at what it undid. Shown once per commit, in
    // the header: repeating it on every row would say the same thing twice
    // per changed field.
    return it.reverts ? '<span class="rev">↺ ' + esc(it.reverts) + '</span>' : '';
  }
  function row(it) {
    const on = sel && sameKey(sel, it);
    const label = it.field ? esc(fieldLabel(it.field)) : '<span class="sub2">' + esc(t(OPS[it.op] || it.op)) + '</span>';
    const delta = it.field ? pair(it.before, it.after, 90) : (it.op === 'rename' ? opText(it) : EMPTY);
    REG.push(it);
    return '<button type="button" class="row' + (on ? ' sel' : '') + '" data-act="sel" data-i="' + (REG.length - 1) + '">' +
      '<span class="pt">' + esc(it.path) + '</span>' +
      '<span class="fd">' + label + (it.risk ? '<span class="tag warn">' + esc(t('review')) + '</span>' : '') + '</span>' +
      '<span class="val">' + delta + '</span></button>';
  }
  function commitCard(g) {
    const kind = (g.author && g.author.type) || '';
    const conf = (g.confidence === null || g.confidence === undefined) ? '' : '<span class="sub2">conf ' + Number(g.confidence).toFixed(2) + '</span>';
    return '<section class="commit' + (g.hot ? ' hot' : '') + '"><div class="chead">' +
      '<span class="hash">' + esc(g.short) + '</span>' + roundOff(g) + '<span class="msg">' + esc(g.message) + '</span>' +
      '<span class="pill ' + esc(kind) + '">' + esc(who({ authorType: kind, author: g.author && g.author.name })) + '</span>' + conf +
      '<span class="when">' + ago(g.ts) + '</span></div><div class="rows">' +
      g.items.map(row).join('') + '</div></section>';
  }
  function groups() {
    const out = [];
    let cur = null;
    (st ? st.events : []).forEach(ev => {
      if (riskOnly && !ev.risk) return;
      if (!cur || cur.id !== ev.id) {
        cur = { id: ev.id, short: ev.short, ts: ev.ts, author: ev.author, message: ev.message, confidence: ev.confidence, hot: false, items: [], reverts: ev.reverts_short || '' };
        out.push(cur);
      }
      if (ev.risk) cur.hot = true;
      cur.items.push(fromEvent(ev));
    });
    return out;
  }
  function pendingBox() {
    const SIGN = { add: '+', modify: '~', delete: '−', rename: '→' };
    const list = st.pending.map(p => '<li><span class="sub2">' + (SIGN[p.op] || '~') + '</span> ' + esc(p.path) + '</li>').join('');
    let h = '<div class="notice"><div>' + esc(t('pending')(st.pending.length)) + '</div><ul class="pl">' + list + '</ul>';
    if (!st.writable) {
      // Offering a Commit button on a space we are not allowed to write would
      // be a lie the server then has to refuse. Say it before the click.
      h += '<p class="hint">' + esc(t('readonly')) + '</p></div>';
      return h;
    }
    if (commitOpen) {
      h += '<div class="bar" style="margin-top:10px"><input type="text" id="cmsg" autocomplete="off" value="' + esc(commitMsg) + '" placeholder="' + esc(t('why')) + '" aria-label="' + esc(t('why')) + '">' +
        '<button type="button" class="btn primary" data-act="commit">' + esc(t('commit')) + '</button></div>';
    } else {
      h += '<button type="button" class="btn" data-act="commit-open">' + esc(t('commit')) + '</button>';
    }
    return h + '</div>';
  }

  // ------------------------------------------------------------- views
  function mainTimeline() {
    let h = '<div class="wrap"><div class="bar">' + seg() +
      '<button type="button" class="chip" data-act="risk" aria-pressed="' + (riskOnly ? 'true' : 'false') + '">' + esc(t('onlyRisk')) + '</button>' +
      '<span class="grow"></span><span class="sub2">' + esc(summary()) + '</span></div>';
    if (st && st.pending.length) h += pendingBox();
    if (st && st.unsure) h += '<div class="notice">' + esc(t('unsure')(st.unsure)) + '</div>';
    if (st && st.hidden) h += '<p class="sub2">' + esc(t('hidden')(st.hidden)) + '</p>';
    if (!st) return h + '</div>';
    if (!st.has_history) return h + '<div class="empty">' + esc(t('nohist')) + '</div></div>';
    const g = groups();
    h += g.map(commitCard).join('') || '<div class="empty">' + esc(t('empty')) + '</div>';
    if (st.truncated) h += '<p class="sub2">' + esc(t('more')(st.truncated)) + '</p>';
    return h + '</div>';
  }
  function mainPages() {
    if (!st) return '<div class="wrap"></div>';
    if (!pagePath) {
      const list = st.page_list;
      return '<div class="wrap"><div class="bar"><h1>' + esc(t('pages')) + '</h1><span class="grow"></span><span class="sub2">' + list.length + '</span></div>' +
        (st.hidden ? '<p class="sub2">' + esc(t('hidden')(st.hidden)) + '</p>' : '') +
        '<div class="commit">' + (list.map(p =>
          '<button type="button" class="row pg" data-act="page" data-v="' + esc(p.path) + '"><span class="pt">' + esc(p.path) + '</span><span class="val">' + esc(p.title) + '</span></button>'
        ).join('') || '<div class="empty">' + esc(t('nohist')) + '</div>') + '</div></div>';
    }
    const rows = fieldList.map(f => row(fromField(f, pagePath))).join('');
    return '<div class="wrap"><div class="bar"><button type="button" class="link" data-act="back">' + esc(t('allpages')) + '</button>' +
      '<span class="grow"></span><span class="sub2">' + esc(t('lastChange')) + '</span></div>' +
      '<h2 class="pane">' + esc(pagePath) + '</h2><div class="commit"><div class="rows">' +
      (fieldList.length ? rows : '<div class="empty">' + esc(t('loading')) + '</div>') + '</div></div></div>';
  }
  function mainDigest() {
    return '<div class="wrap"><div class="bar"><h1>' + esc(t('digestTitle')) + '</h1></div>' +
      '<p class="hint">' + esc(t('digestBody')) + '</p><div class="bar">' + seg() +
      '<a class="btn primary" href="api/digest?since=' + encodeURIComponent(since) + '&amp;t=' + encodeURIComponent(T) + '">' + esc(t('download')) + '</a></div></div>';
  }
  function mainConnect() {
    if (!config) {
      if (!configLoading) {
        configLoading = true;
        api('api/mcp-config').then(d => { config = JSON.stringify(d, null, 2); })
          .catch(err => { note = { kind: 'warn', text: t('loadErr') + err.message }; })
          .then(() => { configLoading = false; if (view === 'connect') render(); });
      }
      return '<div class="wrap"><div class="empty">' + esc(t('loading')) + '</div></div>';
    }
    return '<div class="wrap"><div class="bar"><h1>' + esc(t('connectTitle')) + '</h1></div>' +
      '<p class="hint">' + esc(t('connectBody')) + '</p><pre class="cfg" id="cfg">' + esc(config) + '</pre>' +
      '<button type="button" class="btn" data-act="copy" id="copy">' + esc(t('copy')) + '</button></div>';
  }
  const MAINS = { timeline: mainTimeline, pages: mainPages, digest: mainDigest, connect: mainConnect };

  function detail() {
    const it = sel;
    let h = '<h2 class="pane">' + esc(it.path) + (it.field ? '<br>' + esc(it.field) : '') + '</h2>';
    h += '<div class="box mono">' + (it.field ? pair(clip(it.before, 400), clip(it.after, 400)) : opText(it)) + '</div>';
    h += '<dl class="dl"><dt>' + esc(t('when')) + '</dt><dd>' + ago(it.ts) + '</dd>' +
      '<dt>' + esc(t('who')) + '</dt><dd>' + esc(who(it)) + '</dd>' +
      '<dt>' + esc(t('what')) + '</dt><dd class="mono">' + esc(it.commit || '—') + '</dd>';
    if (it.message) h += '<dt>' + esc(t('why_')) + '</dt><dd>' + esc(it.message) + '</dd>';
    if (it.confidence !== null && it.confidence !== undefined) {
      h += '<dt>' + esc(t('conf')) + '</dt><dd>' + Number(it.confidence).toFixed(2) + '</dd>';
    }
    h += '</dl>';
    const evs = it.evidence.length ? it.evidence.map(e =>
      '<li><b>' + esc(e.kind) + ':' + esc(e.ref) + '</b>' + (e.quote ? '<span class="q">' + esc(e.quote) + '</span>' : '') + '</li>'
    ).join('') : '<li>' + esc(t('none')) + '</li>';
    h += '<div class="sub2">' + esc(t('evidence')) + '</div><ul class="evlist">' + evs + '</ul>';
    if (mode === 'confirm' && plan) {
      const change = plan.after === null || plan.after === undefined
        ? esc(t('remove'))
        : esc(t('restore')) + ' ' + val(plan.after, 200);
      h += '<div class="box confirm"><b>' + esc(t('confirm')) + '</b><p class="hint">' + esc(t('confirmBody')) + '</p>' +
        '<div class="mono">' + esc(plan.path) + (plan.field ? ' · ' + esc(plan.field) : '') + '<br>' + change + '</div></div>' +
        '<div class="btnrow"><button type="button" class="btn danger" data-act="yes">' + esc(t('yes')) + '</button>' +
        '<button type="button" class="btn" data-act="no">' + esc(t('no')) + '</button></div>';
    } else if (it.canUndo && st && st.writable) {
      h += '<button type="button" class="btn danger" data-act="undo">' + esc(t('undo')) + '</button>';
    } else if (st && !st.writable) {
      h += '<p class="hint">' + esc(t('readonly')) + '</p>';
    } else {
      h += '<p class="hint">' + esc(t('cannot')) + '</p>';
    }
    return h;
  }
  function sidePanel() {
    let h = note ? '<div class="notice ' + esc(note.kind || '') + '">' + esc(note.text) + '</div>' : '';
    if (sel) return h + detail();
    return h + '<p class="hint">' + esc(t('hint')) + '</p><p class="sub2">' + esc(t('loop')) + '</p>';
  }

  // ---------------------------------------------------------------- dialogs
  // One element, three states: offer, confirm, closed. Rendered from the same
  // data the request will carry, so "what you confirmed" and "what was sent"
  // cannot drift apart.
  function modal() {
    if (!lastCommit) return '';
    if (mode === 'undo-offer') {
      const noParent = !lastCommit.canUndo;
      return '<div class="modal" role="dialog" aria-modal="true">' +
        '<div class="sheet">' +
        '<h3>' + esc(t('undoTitle')(lastCommit.id, lastCommit.changes)) + '</h3>' +
        '<p class="hint">' + esc(noParent ? t('undoNoParent') : t('undoOfferBody')(lastCommit.id)) + '</p>' +
        '<div class="btnrow">' +
        (noParent ? '' : '<button type="button" class="btn danger" data-act="undo-commit">' + esc(t('undoCommit')) + '</button>') +
        '<button type="button" class="btn primary" data-act="keep">' + esc(t('keepIt')) + '</button>' +
        '</div></div></div>';
    }
    if (mode === 'undo-confirm' && undoPlan) {
      const rows = undoPlan.plans.map(p => {
        const change = p.after === null || p.after === undefined
          ? esc(t('remove'))
          : esc(t('restore')) + ' ' + val(p.after, 160);
        return '<li><span class="mono">' + esc(p.path) + (p.field ? ' · ' + esc(p.field) : '') + '</span><br>' + change + '</li>';
      }).join('');
      return '<div class="modal" role="dialog" aria-modal="true">' +
        '<div class="sheet">' +
        '<h3>' + esc(t('undoCommitTitle')(undoPlan.commit)) + '</h3>' +
        '<p class="hint">' + esc(t('undoCommitBody')(undoPlan.commit)) + '</p>' +
        '<div class="sub2">' + esc(t('undoWhat')) + '</div><ul>' + rows + '</ul>' +
        '<div class="btnrow">' +
        '<button type="button" class="btn" data-act="undo-no">' + esc(t('no')) + '</button>' +
        '<button type="button" class="btn danger" data-act="undo-yes">' + esc(t('yes')) + '</button>' +
        '</div></div></div>';
    }
    return '';
  }

  function render() {
    document.documentElement.lang = lang === 'zh' ? 'zh-CN' : 'en';
    $('lang').textContent = lang === 'zh' ? 'EN' : '中文';
    $('vault').textContent = st ? st.vault : '';
    $('nav').innerHTML = VIEWS.map(v =>
      '<button type="button" data-act="view" data-v="' + v[0] + '" class="' + (view === v[0] ? 'on' : '') + '">' + esc(t(v[1])) + '</button>'
    ).join('');
    REG = [];
    $('main').innerHTML = MAINS[view]();
    $('side').innerHTML = sidePanel();
    $('modal').innerHTML = modal();
    $('main').classList.toggle('busy', busy);
    $('side').classList.toggle('busy', busy);
  }

  // ------------------------------------------------------------- writes
  const reasons = r => (r.plans || []).map(p => p.reason || '').join('; ');
  async function undo() {
    if (!sel || busy) return;
    busy = true; render();
    try {
      const r = await api('api/revert', { body: { page: sel.path, field: sel.field, dry_run: true } });
      const p = r.plans[0];
      note = null;
      if (p.skipped) note = { kind: 'warn', text: t('failed') + (p.reason || '') };
      else { plan = p; mode = 'confirm'; }
    } catch (err) {
      note = { kind: 'warn', text: t('failed') + err.message };
    }
    busy = false; render();
  }
  async function confirm() {
    if (!sel || !plan || busy) return;
    const target = sel, shown = plan;   // confirm exactly what was on screen
    busy = true; render();
    try {
      const r = await api('api/revert', { body: {
        page: target.path, field: target.field, dry_run: false,
        expect: { path: shown.path, field: shown.field, fingerprint: shown.fingerprint }
      } });
      note = r.commit ? { kind: 'ok', text: t('done')(r.commit) } : { kind: 'warn', text: t('failed') + reasons(r) };
    } catch (err) {
      note = err.status === 409 ? { kind: 'warn', text: t('conflict') } : { kind: 'warn', text: t('failed') + err.message };
    }
    mode = 'show'; plan = null; sel = null;
    busy = false;
    await load();
  }
  async function commit() {
    const message = commitMsg.trim();
    if (!message || !st || busy) return;
    busy = true; render();
    try {
      const r = await api('api/commit', { body: { message: message, expect: st.pending } });
      commitOpen = false; commitMsg = '';
      note = null;
      if (r.committed) {
        // Always offer the way back. A commit you cannot take back in the same
        // breath is one people learn to fear, and a fearful tool gets bypassed.
        lastCommit = { id: r.id, changes: r.changes, canUndo: !!r.can_undo };
        mode = 'undo-offer';
      } else {
        note = { kind: '', text: r.reason };
      }
    } catch (err) {
      note = err.status === 409 ? { kind: 'warn', text: t('conflict') } : { kind: 'warn', text: t('failed') + err.message };
    }
    busy = false;
    await load();
  }
  async function keepCommit() {
    const kept = lastCommit;
    lastCommit = null; undoPlan = null; mode = 'show';
    if (kept) note = { kind: 'ok', text: t('did')(kept.id) };
    render();
  }
  async function askUndoCommit() {
    if (!lastCommit || busy) return;
    const target = lastCommit;
    busy = true;
    try {
      const r = await api('api/undo', { body: { commit: target.id, dry_run: true } });
      undoPlan = r; mode = r.changes ? 'undo-confirm' : 'show';
      if (!r.changes) { note = { kind: 'warn', text: t('undoNothing') }; lastCommit = null; }
    } catch (err) {
      note = { kind: 'warn', text: t('failed') + err.message };
      lastCommit = null; mode = 'show';
    }
    busy = false; render();
  }
  async function confirmUndoCommit() {
    if (!undoPlan || busy) return;
    const shown = undoPlan;   // confirm exactly this plan
    busy = true; render();
    try {
      const r = await api('api/undo', { body: {
        commit: shown.commit, dry_run: false,
        expect: { commit: shown.commit, fingerprint: shown.fingerprint }
      } });
      note = r.new_commit
        ? { kind: 'ok', text: t('undone')(r.new_commit, r.reverts) }
        : { kind: 'warn', text: t('undoNothing') };
    } catch (err) {
      note = err.status === 409 ? { kind: 'warn', text: t('conflict') } : { kind: 'warn', text: t('failed') + err.message };
    }
    mode = 'show'; undoPlan = null; lastCommit = null;
    busy = false;
    await load();
  }
  async function openPage(path) {
    pagePath = path; sel = null; note = null; fieldList = [];
    render();
    try {
      fieldList = (await api('api/page?path=' + encodeURIComponent(path))).fields;
    } catch (err) {
      note = { kind: 'warn', text: t('loadErr') + err.message };
      pagePath = null;
    }
    if (view === 'pages') render();
  }

  // One delegated listener: rows are re-rendered on every state change, so
  // binding to them individually would mean rebinding each time.
  document.body.addEventListener('click', ev => {
    const el = ev.target.closest('[data-act]');
    if (!el || busy) return;
    const act = el.dataset.act;
    if (act === 'view') {
      view = el.dataset.v; sel = null; mode = 'show'; plan = null; note = null;
      if (view === 'timeline') load(); else render();
    } else if (act === 'since') {
      since = el.dataset.v; sel = null; mode = 'show'; plan = null; note = null; load();
    } else if (act === 'risk') {
      riskOnly = !riskOnly; sel = null; render();
    } else if (act === 'sel') {
      const it = REG[+el.dataset.i];
      sel = sel && it && sameKey(sel, it) ? null : it;
      mode = 'show'; plan = null; note = null; render();
    } else if (act === 'page') {
      openPage(el.dataset.v);
    } else if (act === 'back') {
      pagePath = null; sel = null; render();
    } else if (act === 'commit-open') {
      commitOpen = true; render();
      const box = $('cmsg');
      if (box) box.focus();
    } else if (act === 'commit') {
      commit();
    } else if (act === 'undo') {
      undo();
    } else if (act === 'no') {
      mode = 'show'; plan = null; render();
    } else if (act === 'yes') {
      confirm();
    } else if (act === 'undo-commit') {
      askUndoCommit();
    } else if (act === 'undo-yes') {
      confirmUndoCommit();
    } else if (act === 'undo-no') {
      mode = 'undo-offer'; undoPlan = null; render();
    } else if (act === 'keep') {
      keepCommit();
    } else if (act === 'copy') {
      const btn = el;
      const done = () => { btn.textContent = t('copied'); setTimeout(() => { btn.textContent = t('copy'); }, 1500); };
      if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(config).then(done, () => {});
      else { const box = $('cfg'); if (box) { const range = document.createRange(); range.selectNodeContents(box); const s = window.getSelection(); s.removeAllRanges(); s.addRange(range); } }
    }
  });
  document.body.addEventListener('input', ev => {
    if (ev.target && ev.target.id === 'cmsg') commitMsg = ev.target.value;
  });
  document.body.addEventListener('keydown', ev => {
    if (ev.key === 'Enter' && ev.target && ev.target.id === 'cmsg') { ev.preventDefault(); commit(); }
    // Esc is the "no" of every dialog. A modal you cannot dismiss without
    // committing to something is a trap, not a confirmation.
    if (ev.key === 'Escape' && mode === 'undo-confirm') { mode = 'undo-offer'; undoPlan = null; render(); }
    else if (ev.key === 'Escape' && mode === 'undo-offer') { keepCommit(); }
  });
  $('lang').addEventListener('click', () => {
    lang = lang === 'zh' ? 'en' : 'zh';
    try { localStorage.setItem('memdrift.lang', lang); } catch (err) {}
    render();
  });

  render();
  load();
})();
"""

PAGE = (
    "<!doctype html>\n"
    '<html lang="en"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    '<meta name="referrer" content="no-referrer">'
    "<title>memdrift</title>"
    f"<style>{STYLE}</style></head>"
    f"<body>{BODY}<script>{SCRIPT}</script></body></html>"
)

__all__ = ["PAGE", "SCRIPT", "STYLE"]
