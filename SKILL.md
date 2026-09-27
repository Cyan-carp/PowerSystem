---
# 定价元数据
suggested_price: "19.9 CNY/per_use"
pricing_tier: "L2-标准级"
pricing_model: "per_use"
summary: "Obsidian笔记库即磁盘文件夹,直接管理"
---
# Obsidian

Obsidian vault = a normal folder on disk.

Vault structure (typical)

* Notes: `*.md` (plain text Markdown; edit with any editor)
* Config: `.obsidian/` (workspace + plugin settings; usually don’t touch from scripts)
* Canvases: `*.canvas` (JSON)
* Attachments: whatever folder you chose in Obsidian settings (images/PDFs/etc.)

## Find the active vault(s)

Obsidian desktop tracks vaults here (source of truth):

* `~/Library/Application Support/obsidian/obsidian.json`

`obsidian-cli` resolves vaults from that file; vault name is typically the **folder name** (path suffix).

Fast “what vault is active / where are the notes?”

* If you’ve already set a default: `obsidian-cli print-default --path-only`
* Otherwise, read `~/Library/Application Support/obsidian/obsidian.json` and use the vault entry with `"open": true`.

Notes

* Multiple vaults common (iCloud vs `~/Documents`, work/personal, etc.). Don’t guess; read config.
* Avoid writing hardcoded vault paths into scripts; prefer reading the config or using `print-default`.

## 使用流程

Pick a default vault (once):

* `obsidian-cli set-default "<vault-folder-name>"`
* `obsidian-cli print-default` / `obsidian-cli print-default --path-only`

Search

* `obsidian-cli search "query"` (note names)
* `obsidian-cli search-content "query"` (inside notes; shows snippets + lines)

Create

* `obsidian-cli create "Folder/New note" --content "..." --open`
* Requires Obsidian URI handler (`obsidian://…`) working (Obsidian installed).
* Avoid creating notes under “hidden” dot-folders (e.g. `.something/...`) via URI; Obsidian may refuse.

Move/rename (safe refactor)

* `obsidian-cli move "old/path/note" "new/path/note"`
* Updates `[[wikilinks]]` and common Markdown links across the vault (this is the main win vs `mv`).

Delete

* `obsidian-cli delete "path/note"`

Prefer direct edits when appropriate: open the `.md` file and change it; Obsidian will pick it up.

## 依赖说明

### 运行环境
- **Agent平台**: 支持SKILL.md的任意AI Agent(Claude Code / Cursor / Codex / Gemini CLI等)
- **操作系统**: Windows / macOS / Linux

### 依赖说明
| 依赖项 | 类型 | 是否必需 | 获取方式 |
|:-------|:-----|:---------|:---------|
| LLM API | API | 必需 | 由Agent内置LLM提供 |

### API Key 配置
- 本Skill基于Markdown指令,无需额外API Key(除内容中明确标注的外部API)

### 可用性分类
- **分类**: MD+EXEC(纯Markdown指令,部分功能需要exec命令行执行能力)
- **说明**: 基于Markdown的AI Skill,通过自然语言指令驱动Agent执行任务

## 核心能力

- Work with Obsidian vaults (plain Markdown notes) and automate via obsidian-cli
- 触发关键词: vaults, plain, obsidian

## 适用场景

| 场景 | 输入 | 输出 |
|------|------|------|
| 基础使用 | 用户请求 | 处理结果 |

**不适用于**：需要人工判断的复杂决策场景

## 示例

### 示例1：基础用法

```
Pick a default vault (once):

* `obsidian-cli set-default "<vault-folder-name>"`
* `obsidian-cli print-default` / `obsidian-cli print-default --path-only`

Search

* `obsidian-cli search "query"` (note names)
* `obsidian-cli search-content "query"` (inside notes; shows snippets + lines)

Create

```

## 错误处理

| 错误场景 | 原因 | 处理方式 |
|---------|------|---------|
| 配置错误 | 参数缺失或格式错误 | 检查依赖说明中的配置要求 |
| 运行时错误 | 运行环境不满足 | 确认运行环境符合依赖说明 |
| 网络错误 | 连接超时或不可达 | 检查网络连接后重试，参考国内替代方案 |

## 常见问题

### Q1: 如何开始使用Obsidian？
A: 请先阅读使用流程章节，确认环境满足依赖说明中的要求。

### Q2: 遇到错误怎么办？
A: 请参考错误处理章节，按照表格中的处理方式操作。

### Q3: Obsidian有什么限制？
A: 请参考已知限制章节了解具体限制。

## 已知限制

- 需要LLM支持，无LLM环境无法使用
- 复杂场景可能需要人工辅助判断
- 性能取决于底层模型能力

---
## 边界条件与限制

### 输入限制
- **Vault路径长度**: 由于命令行工具的限制，输入的Vault路径长度不应超过260个字符。
- **文件名规范**: 输入的文件名应遵循操作系统文件命名规范，避免使用特殊字符。
- **搜索内容**: 搜索内容应避免使用过于复杂的正则表达式，以免影响搜索效率。

### 性能边界
- **并发处理**: obsidian-cli命令不支持并发执行，同时执行多个命令可能会导致性能下降。
- **数据量**: 对于包含大量笔记的Vault，某些操作（如搜索、移动/重命名）可能需要较长时间。

### 兼容性约束
- **操作系统**: obsidian-cli仅在Windows、macOS和Linux操作系统上运行。
- **Obsidian版本**: obsidian-cli需要与Obsidian应用程序兼容，建议使用最新版本的Obsidian。
- **插件兼容性**: 部分obsidian-cli命令可能受到特定插件的影响，使用前请确保插件与obsidian-cli兼容。

### 其他限制
- **外部API**: 部分功能（如网络请求）可能受到外部API的限制，如访问频率限制、数据量限制等。
- **脚本执行**: obsidian-cli命令不支持直接执行外部脚本，需要通过exec命令行执行能力来实现。
---

