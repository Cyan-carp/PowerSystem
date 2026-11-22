# 笔记治理工具

本目录维护迁移后的可重复检查、解析辅助和工作区链接接口，不包含业务源码副本。

## 文件

- `migration-map.json`：迁移前后路径及改名映射，作为旧路径的唯一审计入口。
- `workspace-links.json`：Vault 外工作区目标、代理页、仓库相对路径、Git 跟踪状态和 GitHub URL 清单。
- `build_workspace_links.py`：按代理页位置计算可移植的仓库相对路径，并按当前 Git 跟踪状态重建代理页。
- `log-catalog.json`：部署日志的导航目录。**编号、标题、日期、批次指针、结论的唯一编辑位置**；同一字段不要在总索引与月份页重复手填。
- `log-catalog-provenance.json`：拆分前旧总表 112 行的逐行原文与去向映射，作为“每条历史批次都还在”的可复查依据；生成校验会确认映射未失效。
- `build_log_index.py`：从目录生成按月时间轴、月份索引，并只替换总索引里 `<!-- BEGIN GENERATED:timeline -->` 与结束标记之间的内容。`--check` 在有待生成文件时返回非 0。
- `note_links.py`：Markdown/Obsidian 链接、章节锚点与路径解析的共用模块；`check_notes.py` 与 `tools/note-graph/build_graph.py` 共用同一份提取与定位逻辑，避免“检查器说没问题、图谱说断链”。支持的语法与两种锚点方言见下节。
- `check_notes.py`：治理检查器，默认严格模式，`--ci` 放宽仓库根 `artifacts/` 私有证据的缺失判定，`--verbose` 列出全部警告。
- `test_check_notes.py` / `test_build_log_index.py` / `test_graph_consistency.py`：在临时夹具里制造缺陷或过期生成物，不移动真实笔记。

## 支持的链接语法

**提取**（`note_links.iter_links`）

- 行内链接/图片：`[text](target)`、`![alt](target)`；目标里的括号成对消费，文件名可含中文括号。
- 引用式：完整 `[text][label]`、折叠 `[text][]`、简写 `[label]`（仅当 `[label]: target` 已定义）；引用式图片同样识别。
- Obsidian：`[[note#标题|别名]]`、`![[file]]`；表格内 `\|` 视为转义。
- 围栏代码块（``` / ~~~）整段不扫描；行内代码在提取链接时剔除。
- **不声明支持**：嵌套方括号、跨行链接文本、HTML `<a>`、Setext 标题（`===`/`---`）。遇到这些写法不会被当作链接，也不会报错。

**锚点判定**（`note_links.anchor_status`）

| 方言 | 适用 | 规则 |
| --- | --- | --- |
| `markdown` | Markdown 链接/图片/引用 | GitHub 规则：片段 URL 解码后比对标题 slug；标题内行内代码按渲染文字计入（`## Use \`pip\`` → `use-pip`）；重复标题按出现顺序追加 `-1`、`-2`；不做“标题原文”兜底 |
| `obsidian` | 双链 | 标题原文匹配（忽略大小写），支持 `#一级#二级` 取末段与 `^块ID`；不做 slug 归一化 |

同一页、跨页、库内指向库外 Markdown 的片段都按上述规则校验；只有目标是否为 `.md` 才影响读取，不影响是否校验。


## 检查内容

**Vault（`notes/`）**

- 元数据：`status`/`scope`/`updated` 取值与格式；单篇 15,000 字符上限；Markdown 重名。
- 结构：多篇笔记目录是否有 `00-` 索引；是否残留“开发阶段”旧目录；旧顶层目录是否复活。
- 链接：双链按“Vault 根路径 → 源文件相对路径 → 短名唯一匹配”解析；Markdown 链接**只**按源文件相对路径解析，不退回全库同名兜底。
- 章节：同页、跨页、库内指向库外 Markdown 的片段都校验；Markdown 用 GitHub slug（含重复标题编号、URL 解码），Obsidian 用标题原文或 `^块ID`。重复标题记歧义警告。
- 可达：从 `00-项目笔记总入口` 与 `00-当前系统状态` 出发做可达性统计；区分“未被本目录索引直接列出”“索引无入链”“全库不可达”三种情况。
- 覆盖豁免：只有第四至第八版的 `03-跨对话交接/` 五个具名目录不要求“每页被入口直接列出”（入口按轮次收窄，完整列表见版本索引）；豁免按目录写死并注明理由，不是全库放宽。
- 迁移：目标缺失但能沿 `migration-map.json` 追到现行位置时，报 `migration-residue` 并给出新路径。
- 大小写：Windows 能打开、Linux 与 GitHub 打不开的路径记 `case-mismatch`。
- 其它：验收清单未勾选条目须带“来源：”；工作区代理页、`tracked` 标记与 GitHub URL 与清单一致。

**库外项目 Markdown**

- 根 `README.md`/`AGENTS.md`/`SKILL.md`、`stages/`、`deploy/`、`tools/` 以及 `shared-memory/` 三个入口文件。
- `node_modules`、`artifacts`、`dist`、`build` 等依赖与生成目录不参与判定。
- 库外用双链指向笔记时按笔记解析；指向工作区路径的双链只记警告（GitHub 不解析双链）。

**会话历史**

- `shared-memory/conversations/` 只统计本地链接可解析数，不回判历史旧路径。

## 部署日志索引与时间轴

部署日志总索引不再逐条追加：全量时间轴按月份拆到 `部署与运维日志/时间轴/`，总入口只保留用途导航、最近批次和月份入口。

**唯一编辑位置**

- 编号、标题、日期、版本、批次指针、结论只改 `log-catalog.json`。
- 生成文件（`时间轴/*.md`、总索引标记块）不手改；改了也会被下次生成覆盖。
- 总索引里标记块之外的“阅读顺序、分类目录、维护规则、相关笔记”是人工内容，生成器不碰；B4 一类人工导航修正可以放心写在块外。

**生成与校验**

```powershell
python -B tools/note-governance/build_log_index.py --check   # 只读：登记、页面存在、编号冲突、旧表映射、生成物是否最新
python -B tools/note-governance/build_log_index.py           # 生成月份页与总索引标记块
```

`--check` 有两类失败：结构性问题（日志页未登记、条目指向不存在的页面、入口页缺生成标记、旧表映射失效）直接返回 1；结构正常但月份页/索引/标记块与目录不一致时，先输出 `N file(s) would change`，再返回 1。只有“全部已最新”才返回 0，因此 CI 的绿色结果表示**生成物已同步**，不只是“目录能解析”。编号冲突仍需人工决定改哪个，保持警告不阻断。

生成器不写时间戳，输入不变时重复生成字节一致；它只替换标记块与 `时间轴/` 下的文件，标记之外的手写导航不受影响。CI 只跑 `--check`，不会借流水线覆盖手写内容。

## 运行

```powershell
python tools/note-governance/build_workspace_links.py
python -B tools/note-governance/build_log_index.py
python -B tools/note-governance/check_notes.py
python -B tools/note-governance/check_notes.py --ci
python -B -m unittest discover -s tools/note-governance -p 'test_*.py'
python -B tools/note-graph/build_graph.py
```

`build_workspace_links.py` 只在工作区目标或代理关系变化时执行，避免无关生成噪音。`build_graph.py` 的 `generated` 记录真实生成时间（UTC、ISO 8601）；`inputDigest` 按笔记路径与字节内容计算，输入未变时沿用上次时间，因此重复生成字节一致。它表示图谱生成时间，不是生产更新时间。图谱只画 Obsidian 双链，`stats.broken` 同时包含目标页缺失与章节缺失，并分别给出 `brokenTargets`、`brokenAnchors`；章节失效时仍保留该边（目标页存在），只在 `broken` 中记一笔，所以边数不等于有效链接数。章节判定与检查器共用 `note_links`。

GitHub Actions 在 `notes/**`、`shared-memory/**`、`tools/note-governance/**`、`tools/note-graph/**`、`README.md`、`AGENTS.md`、`SKILL.md`、`stages/**/*.md`、`deploy/**/*.md`、`tools/**/*.md` 或本工作流变更时，依次跑解析回归测试、`build_log_index.py --check` 与 `check_notes.py --ci`（`.github/workflows/notes-check.yml`）。生成检查是只读的，失败表示“有人改了目录却没重新生成”，不会自动改写文件。仓库未核实远端分支保护，因此只能确认“工作流失败”，不能宣称“已强制阻断合并”。

迁移前的文件哈希、用户已修改验收清单和旧图谱备份保存在被 Git 忽略的 `artifacts/笔记重整基线-20261017/`。
