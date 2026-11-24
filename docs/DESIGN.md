# 设计说明（W1 实现）

这份文档记录 PRD 落地时做的判断和偏差。PRD 是意图，这里是决定。

## 0. 名字

PRD 用 `memgit`。动手前查了撞名：

- PyPI `memgit` 已被 `code4161/memgit` 0.16.0 占用，简介里就写着 "versions every change"，域名 `memgit.dev` 也是它的。
- 备选 `memlog`（top 143★）、`memtrace`（top 89★）同样被占。

改用 **`memdrift`**：PyPI 与 GitHub 均无冲突，且 `drift` 正是 PRD 第 11 节那篇文章的核心词——记忆在暗处漂移。包名、CLI 名、仓库名三处统一。

## 1. 为什么不用 git 做底层

PRD 已给理由（git 对象模型表达不了结构化 change；用户目录可能已在 git 里）。实现上还多一条：git 会往被管理目录里写 `.git/`，而 memdrift 的承诺是「只旁挂一个 `.memgit/`」。用 git 意味着要么污染用户目录，要么用 `--git-dir` 折腾 work tree，两条都不干净。

代价是自己实现对象存储——约 200 行，比预期轻。

## 2. 对象模型

```
objects/ab/cdef…   zlib( "{kind} {size}\0{data}" )，sha256 寻址
commits/ab/cdef…   commit json，id = sha256(canonical(payload))
refs/head          当前 commit id
index.sqlite       查询缓存
```

- commit id 不含自身 `id` 字段计算，写入后再补 `id`，读回时校验。这样 commit 是自校验的。
- tree 是 `{path: blob_id}` 的 canonical JSON，本身也存成 object（kind=tree）。空目录没有条目——v1 不需要目录节点。
- **`.memgit` 可删**：`tests/test_repo.py::test_deleting_memgit_keeps_memory_files` 守着这条。

## 3. 字段精度阶梯

PRD 的差异化全押在「变更能说人话」。所以解析顺序是硬约束：

| 层级 | field 形式 | 精度 |
|---|---|---|
| 1 | `frontmatter.pricing`、`frontmatter.contacts.owner` | 高，YAML 嵌套拍平成点号路径 |
| 2 | `section:定价` | 中，按 heading 切块逐块比对 |
| 3 | `body` | 低，标 `low_precision: true` |

降级条件：一次改动里 heading 结构塌了（新增+删除 ≥3 且重合 ≤30%），说明整页重排，此时逐个 section 报「删了 3 个又加了 3 个」是噪音，改为一条 body 级变更并标低精度。宁可承认不准，也不假装精确。

另一个降级：文本变了但字段都对不上（纯空白、frontmatter 之外的漂移），同样落 `body` + `low_precision`，**不静默丢弃**——静默丢弃就是 PRD 说的「记忆在暗处漂移」。

## 4. change_ratio

定义：**整页**的变更比例，`1 - SequenceMatcher(旧行, 新行).ratio()`，按行。

PRD commit schema 里 `change_ratio: 0.03` 出现在一个 frontmatter 字段的 change 上，量级像是整页比例（单个字段改动不该占 3% 的行）。同一 commit 内同一 path 的多个 change 共享这个整页比例，阈值判定（`> 0.5` 进「需要确认」）才能在页级成立。

## 5. rename

同一次快照对比里，`删除 A` + `新增 B` 且 blob id 相同 → `op: rename`，带 `from`。这在记忆系统里很常见：Linger 合并页面（`contacts/tom.md` → `contacts/thomas.md`）。不做 rename 检测，日报会把每次合并报成一次删除加一次新增，把「遗忘」区塞满假警报。

## 6. watch 的 debounce

trailing-edge timer：每个文件事件重置倒计时，安静 5 秒后才提交。Linger 那种一次性重写 30 页的行为，落成 1 个 commit 而不是 30 个。

- 自己写 `_Debouncer`，没用 watchdog 之外的库，`threading.Timer` 够用。
- 忽略 `.memgit/` 自身，否则自己提交会触发自己。
- Ctrl+C 时 flush 未落盘的残留事件，不丢最后一次写入。

## 7. 索引

SQLite 只是缓存，不是真相。`ops.log()` 每次先检查 `index.has_commit(HEAD)`：

- 索引落后或丢失（崩溃、`.memgit` 被拷贝、手动删了 sqlite）→ 自动从 commit 文件重建，再查。
- 所以「删了 index.sqlite 就坏了」这种事不会发生。

## 8. 与 PRD 的偏差

| PRD | 实现 | 原因 |
|---|---|---|
| `memgit` | `memdrift` | 撞名，见 §0 |
| 每条命令带 `<dir>` 位置参数 | 全局 `-C <dir>` | `blame <page>` 会和目录参数抢位置；git 用户对 `-C` 有肌肉记忆 |
| `git diff` 式 `diff <a> <b>` | 三态 + 独立的 `show`：`diff` 无参 = 工作区 vs HEAD，`diff <commit>` = 工作区 vs 该 commit（git 语义），`diff <a> <b>` = 两快照；`show <commit>` 看单次提交改了什么 | 单参数若表示「该 commit 的变更」会和 `git diff HEAD` 的肌肉记忆打架；分开命名两边都不别扭 |
| `confidence` 缺失视为 1.0 | 存 `null`，展示时按 1.0 处理 | 存 null 保留「没声明」和「声明为 1.0」的区别，日报要区分 |

## 9. 显示层（W2）

### 9.1 一根管道，两个出口

```
repo ──► events.build() ──► EventStream ──┬──► digest.render_digest()  → 单文件 html
                                          └──► server._Handler          → 本地页面
```

`digest` 和 `serve` 都不碰 `repo`，只吃 `EventStream`。这不是为了少写代码，是为了**保证两个视图不会说不同的话**。如果各自去读 commit json，迟早有一边漏了 rename，或者忘了加 `low_precision`。

### 9.2 脱敏做在事件层，不是渲染层

`security.Redactor` 在 `events.build()` 里就作用完了：路径、引文、private 页面都在进入渲染器之前处理掉。渲染器拿到的数据里**根本没有**本机绝对路径，所以它不可能泄漏。测试断言的是「HTML 里不含 root 字符串」，不是「HTML 里调用了 redact」。

私密页面的处理多走一步：`private: true` 的页面，它的路径从 `pages`、`events` 两处都剔除；**指向它的 evidence 也一并剔除**。否则页面藏起来了，`why: email:msg-private` 还在，等于换个位置漏。

### 9.3 risk 判定为什么收窄

第一版把「commit 的 confidence 低于阈值」也算进每条 change 的 risk，结果一次不确定的夜间整理让 10 条全部变红。**到处都是红，等于没有红。**

现在的规则：

| 条件 | 是否标红 | 理由 |
|---|---|---|
| `op == delete` | 是 | 内容真的没了 |
| `change_ratio >= 0.5` | 是 | 整页重写，字段归属开始不可信 |
| `op == add` | 否 | 纯增量，没有东西被覆盖 |
| commit confidence 低 | **否**，改成提交级一条提示 | 不确定性属于整次运行，不属于某个字段 |

低置信度在 UI 上是提交卡头上的一个灰标 `agent unsure · confidence 0.42`，不污染每一行。

### 9.4 serve 只绑回环

`check_host()` 白名单只有 `127.0.0.1 / localhost / ::1`，**没有 `--allow-public` 之类的开关**。理由：

- 记忆目录是一份传记，把它挂到局域网上不该是一个参数就能做到的事。
- 没有账号体系。补一套账号体系会反过来暗示「这个端点本来就该被外面访问」——那是错的方向。
- 要给别人看，就 `digest` 导出文件。**分享是显式动作，不是默认可达。**

同一个前提下，浏览器仍算敌对环境：别的网站可以朝 `localhost` 发请求。所以每个进程随机生成 token 放进 URL，缺 token 一律 403，响应带 `default-src 'none'` 的 CSP。测试里 `0.0.0.0`、`::`、`192.168.x`、域名五种都会被拒。

### 9.5 digest 为什么是单文件

不带 `<link>`、不带 `<img src>`、不带 webfont。理由不是洁癖：**一个你会转发出去的文件，一旦请求外部资源，就等于把你的客户名单报给了第三方**（字体 CDN、分析脚本、图片托管任一条都够）。

所以 CSS 内联、JS 内联、字体用系统栈。生成脚本里有一条硬检查：产物含 `http://` 或 `https://` 就直接退出。

### 9.6 加密：明确不做

`.memgit/objects` 是 zlib，**不是加密**。这一点在 README 里写在明处，而不是留给人自己发现。

不做的理由：加密要引入密钥管理（放哪、怎么轮换、丢了怎么办），而这一层的价值在于「可读的历史」。半吊子的加密比诚实的不加密更危险——它给人虚假的安全感。真需要，就把目录放到加密卷上，那是操作系统已经解决的问题。

## 11. 还没做（下一步）

- `revert`：反向 commit，不 rewrite 历史。
- MCP server：stdio，`memory_digest / memory_blame / memory_revert / memory_history`。
- 规则引擎、多目录聚合、跨系统冲突（v1.1）。
- 路径级 `--exclude` 的 glob 语义目前是简化实现（`**/name/**` 按目录名匹配），够覆盖 `node_modules` 这类场景，不追求完整 gitignore 语法。

## 12. 截图是跑出来的，不是画的

README 的八张图由 `scripts/render_terminal_shots.py` 生成，流程是：

1. `make_demo_vault.py` 重建一个干净的演示 vault（5 个 md，内容对应 PRD 的 Acme 案例）；
2. 用真实的 `memdrift` 可执行文件跑 `init / status / commit / log / blame / show / diff / digest / watch`；
3. 捕获 stdout（`CLICOLOR_FORCE=1` 保留 click 的 ANSI 颜色），解析成带颜色的 span；
4. 用 Pillow 画成终端窗口，逐字形做中英文回退（Cascadia Mono 没有中文，落到微软雅黑）。

约束：

- **任何命令失败，脚本直接退出**，不允许画一张"好看但没跑通"的图。
- watch 那张是**真启动进程**再改三个文件，`committed` 字样来自进程真实输出；没有就当失败。
- digest 那张要求文件真的落盘，且产物不含 root 路径、不含任何 URL，否则脚本退出。
- 绝对路径会被替换成 `~/vault`，截图里不出现本机目录。
- 最后一步删掉 `.memgit` 并对比 acme.md 的 sha256，把「历史可删、内容不丢」这张底牌做成可见证据。

改了 CLI 输出格式要重跑这个脚本，否则 README 会和产品对不上。这是唯一的维护成本，比手截图便宜。
