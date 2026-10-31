# 2026-10-30 · artifacts 归档库体检与缓存清理

> **注意**：本文前四节的目录名写于 2026-10-30 改名之前，正文里的英文目录名（如 `v4-assistant`）现已是 `v4-智能助手`，对照见 `artifacts/_改名对照表-20261030.md`。历史判断过程保留原样，不回改。

## 任务
所有者要求整理 `artifacts/`（认为「太乱」），并说明该目录用途。

## 结论（可直接引用）
- `artifacts/` = **运行产物档案库**，被仓库根 `.gitignore` 第 11 行整目录排除，不进 Git；不是部署数据卷，不等于每日加密备份。权威索引在 `notes/02-工程规范边界与验收治理/运行产物说明/`（6 篇分卷）。
- 清理前：17.36 GB / 231,459 文件 / 95 个顶层条目（其中 67 个是 `主题-日期-rN` 单批次）。
- **清理后：12.06 GB / 77,099 文件 / 88 个条目**，释放 5.30 GB（31%）。
- 结构矛盾（真实成因）：67 个单批次只占 0.20 GB，却制造了绝大部分视觉噪音；真正占磁盘的是 3 个专题根（`v4-assistant` / `v5-desktop` / `telemetry-reliability`），合计 10.01 GB / 占 83%。

## 已删除（15 项，全部为可再生缓存）
go-cache、go-mod、mermaid-cli/node_modules、stage8 venv、stage9 venv、stage9 render-tools/node_modules、stage8 baseline 前端 node_modules、v4 s02 venv、v4 s09 embedding-deps、v4 s09 embedding-nlp、stage4 browser-harness/node_modules、stage7 distribution/build、testdeps、bin、legacy；另删空目录 testtmp、repository-layout-test-tmp。

## 未删（证据，勿再提议删除）
`runtime/`（笔记明写「不移动、不删除」MA 证据）、67 个批次包、`v4-assistant/s02-r2/postgres.zip`(325 MB 整库快照)、`qdrant-storage/`(~1 GB 向量库原始段)、`v5-desktop` 的 4 份 Setup.exe + 2 份 zip（已交付桌面客户端）、`stage8/models/multilingual-e5-small`(449 MB 权重)。

## 本次形成的文件
- `artifacts/README.md`（新建）：根目录导航 + 四类处置规则 + 硬性边界 + 清理记录。
- `artifacts/_索引-20261030.md`（生成）：88 项完整清单，含大小/文件数/最后活跃。
- `artifacts/_清理记录-20261030.md`（生成）：15 项清单、依据、B 组（证据包内部缓存副本 2.44 GB）未执行。
- `notes/.../01-数据链路/05-阶段一目录迁移记录.md`：第 24 行「可再生的构建/依赖文件」补注 2026-10-30 已清理；`updated` 改 2026-10-30。`check_notes.py` 与 `--ci` 均 PASS（546 notes）。

## 踩坑（重要，下次直接照做）
1. **应用沙箱有批量删除保护**：单轮删除超 50 文件报 `[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED]`，会话内无法执行大批量删除；且探查该机制实现会被沙箱拒绝。可行路径 = 让所有者在本机 PowerShell 自行执行。
2. **Go module cache 文件全部只读**（21,198 个）：`[System.IO.Directory]::Delete($p,$true)` 直接失败。必须先用
   `Get-ChildItem -LiteralPath $p -Recurse -Force | ForEach-Object { $_.Attributes = 'Normal' }`
   清属性再删，否则报「拒绝访问」。官方姿势是 `go clean -modcache`（若该目录被 GOMODCACHE 指向）。
3. **不搬目录**：`notes/` 正文按路径引用 74 个 artifacts 子目录（v4-assistant 71 处、stage8 45 处、telemetry 33 处），任何按主题重排都会断链。
4. 统计体积用 Python `os.walk`，`du -sh` 在 23 万文件上会被 SIGTERM。

## 待办
- B 组（`telemetry-reliability/TR-S02|TR-S04` 的 go-cache、TR-S04 baseline/.cache、`stage4/go-cache-publish`，共 2.44 GB）仍在，属可选项，所有者未决定。

## 追加（同日）：命名标准 + 105 个目录改中文名

### 落地的规则（两处）
- 新增 `notes/02-工程规范边界与验收治理/运行产物说明/07-运行产物目录收纳与命名规范.md`：顶层三类（阶段根/专题根/批次包）、命名标准（中文主题 + YYYYMMDD + rN）、登记前置、容量复核、清理边界。
- `01-目录边界与笔记治理.md` 只留一行指针（原文加五行会让该页超 15,000 字符上限，check_notes 直接 FAIL；拆分后 14,9xx 通过）。
- `00-索引·运行产物说明` 分卷导航挂接 07，并在「阅读与保留」补「新增批次必须登记」。

### 改名结果
- **105 个目录**改为中文名：批次 70 个（含 asset-archive→历史归档、frp-source-tests→来源自动维护-测试目录、tdengine-config-backup→TDengine配置备份）+ 专题根 3 个及子目录 32 个。
- 专题根：`v4-assistant`→`v4-智能助手`、`v5-desktop`→`v5-桌面客户端`、`telemetry-reliability`→`遥测可靠性`；子批次按正式阶段名（S01-立项契约…S09-联合验收 / S01-基础安装版… / S01-监控与失败状态…S04-容量恢复与实时通道）。
- 未改：11 个阶段根（`stage1-数据链路` 等已可读）、`runtime`（禁动）、`mermaid-cli`/`go-cache`（缓存固定名）。
- 同步引用：第一批 72 文件 / 297 处，第二批 111 文件；旧名残留复查两次均为 0。
- 映射与回滚依据：`artifacts/_改名映射-20261030.json`、`_改名映射-专题根-20261030.json`，人读版 `artifacts/_改名对照表-20261030.md`（105 条）。

### 踩坑（务必记住）
1. **代理页文件名不会自动跟随改名**：`build_workspace_links.py` 用 `old_proxy.get(path, proxy_name(path))` 复用旧 proxy 名，而 `proxy` 字段不含 `artifacts/` 前缀，全局替换改不到。必须同时改 `tools/note-governance/workspace-links.json` 的 `proxy` 值（或删旧页 + 改 json 后重跑）。
2. **笔记里的 wikilink 用 `artifacts-<目录名>` 形式**（指向 06-代码工作区导航的代理页），`artifacts/<目录名>` 的替换规则覆盖不到，会留 broken wikilink；改名后必须单独扫一遍 `artifacts-` 前缀形式。
3. **写回文件用 `open(...,'w',encoding='utf-8')` 会把 CRLF 变 LF**（读时不带 newline=''）。仓库 `core.autocrlf=true`，git 会规范化所以 diff 不暴增，但工作区文件换行与其余文件不一致。
4. 工作区**本来就有未提交改动**（home-dev 迁移、AGENTS.md 新增段、00-当前系统状态 第七节），diff 里出现的这些不是本次造成，不要误判为误伤。

### 验证
`check_notes.py` 严格与 `--ci` 双 PASS（547 notes）；`build_graph.py` broken=0；旧名残留 0。
