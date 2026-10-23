# 笔记治理工具

本目录维护迁移后的可重复检查和工作区链接接口，不包含业务源码副本。

- `migration-map.json`：迁移前后路径及改名映射，作为旧路径的唯一审计入口。
- `workspace-links.json`：Vault 外工作区目标、代理页、仓库相对路径、Git 跟踪状态和 GitHub URL 清单。
- `build_workspace_links.py`：按代理页位置计算可移植的仓库相对路径，并按当前 Git 跟踪状态重建代理页。
- `check_notes.py`：检查生命周期元数据、重名、索引、开发阶段旧目录、双链、Markdown 链接、代理目标和验收清单来源。默认严格模式；`--ci` 放宽 `artifacts/` 私有证据的缺失判定（该目录按设计不入库），仓库内文件、双链与元数据仍严格校验。

运行：

```powershell
python tools/note-governance/build_workspace_links.py
python tools/note-governance/check_notes.py
python tools/note-governance/check_notes.py --ci
python tools/note-graph/build_graph.py
```

GitHub Actions 在 `notes/**`、`shared-memory/**`、`stages/**`、`deploy/**` 或本目录变更时执行 `--ci` 模式（`.github/workflows/notes-check.yml`），失败即阻断合并；提交前应先跑严格模式。

迁移前的文件哈希、用户已修改验收清单和旧图谱备份保存在被 Git 忽略的 `artifacts/note-reorg-20261017-baseline/`。
