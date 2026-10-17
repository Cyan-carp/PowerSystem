# 笔记治理工具

本目录维护迁移后的可重复检查和工作区链接接口，不包含业务源码副本。

- `migration-map.json`：迁移前后路径及改名映射，作为旧路径的唯一审计入口。
- `workspace-links.json`：Vault 外工作区目标、代理页、仓库相对路径、Git 跟踪状态和 GitHub URL 清单。
- `build_workspace_links.py`：按代理页位置计算可移植的仓库相对路径，并按当前 Git 跟踪状态重建代理页。
- `check_notes.py`：检查生命周期元数据、重名、索引、开发阶段旧目录、双链、Markdown 链接、代理目标和验收清单来源。

运行：

```powershell
python tools/note-governance/build_workspace_links.py
python tools/note-governance/check_notes.py
python tools/note-graph/build_graph.py
```

迁移前的文件哈希、用户已修改验收清单和旧图谱备份保存在被 Git 忽略的 `artifacts/note-reorg-20261017-baseline/`。
