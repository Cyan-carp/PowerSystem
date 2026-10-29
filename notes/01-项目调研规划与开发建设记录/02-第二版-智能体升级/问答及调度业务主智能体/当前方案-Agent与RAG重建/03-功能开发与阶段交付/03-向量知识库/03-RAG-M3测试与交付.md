---
tags:
  - 源网智联
  - 第二版
  - Agent与RAG
status: 现行
scope: 第二版-问答
updated: 2026-10-13
---

# 03 · RAG-M3测试与交付

运行编号 `rebuild-20261011`，日期2026-10-11。本机合成演示范围，R730XD及公网未发布。证据在忽略目录 `artifacts/stage8-agent-rag/rebuild-20261011/`；真实与受控故障分开判定。

## 一、实际结果

本地multilingual-e5-small、384维Cosine、query/passage前缀；Qdrant独立命名卷仅绑定回环16333。初始6份适用资料，最终集合95个片段；所有片段实测最多400个模型token，默认返回5片。

“平台有哪些容器，如何拉起服务”和“现场逆变器的风扇冷却方式是什么”实际向量检索命中。真实R3内部部署资料与组合查询调用search_knowledge并返回文档来源。

MD/TXT/DOCX（含表格）/文本PDF提取通过；扫描PDF记录未提取，不实施OCR。独立测试别名实际构建、修改A、删除B、保留上版集合；缺失文件失败后原别名未改变，失败报告status=failed。历史来源能从保留集合读取。

## 二、证据

`knowledge-final.json`（初始批次见knowledge-import.json）、`local-checks.json`、`import-checks.json`、`import-fixture/`各独立批次、`agent-tests.log`和`real-r3/responses.json`。生产别名与导入测试别名独立。

## 三、阶段结论

RAG-M3本地功能验收通过，允许进入完整Agent阶段。增量更新采用完整新集合重建，未做仅重算变化片段的性能优化。废案不自动入库。

## 相关笔记

[[01-项目调研规划与开发建设记录/02-第二版-智能体升级/问答及调度业务主智能体/当前方案-Agent与RAG重建/00-索引·Agent与RAG重建]] · [[02-工程规范边界与验收治理/项目工程规范/00-索引·项目工程规范]] · [[06-代码工作区导航与链接映射/stages-08-agent-rag-README|README]]
