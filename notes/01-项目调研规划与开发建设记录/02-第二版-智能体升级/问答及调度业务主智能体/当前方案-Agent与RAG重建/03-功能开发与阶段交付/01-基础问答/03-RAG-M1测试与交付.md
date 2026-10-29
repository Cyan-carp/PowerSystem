---
tags:
  - 源网智联
  - 第二版
  - Agent与RAG
status: 现行
scope: 第二版-问答
updated: 2026-10-13
---

# 03 · RAG-M1测试与交付

运行编号 `rebuild-20261011`，日期2026-10-11。本机合成演示范围，R730XD及公网未发布。证据在忽略目录 `artifacts/stage8-agent-rag/rebuild-20261011/`；真实与受控故障分开判定。

## 一、实际结果

独立FastAPI与主模型协议接入，普通技术、生活和长回答不要求来源、固定JSON或章节。真实R3的TCP、菜谱、长篇培训和库外技术问答通过；长回答超过1500字符，源码未设置旧短答案限制。

受控HTTP503验证Provider产生真实错误、degraded、空答案；不伪造成功，也不引入备用模型。新Agent16项测试覆盖自由回答、故障、只读参数、历史与真实PostgreSQL隔离；`pip check`无缺失依赖。

## 二、证据

`real-r3/responses.json`、`local-checks.json`、`agent-tests.log`。真实配置模型标识为deepseek-v4.1-flash，接口经已有中转主机；这不构成对供应商内部模型身份的独立证明。

## 三、阶段结论

RAG-M1本地功能验收通过，允许进入联网搜索阶段。模型故障期间开放式问答不保证可用。

## 相关笔记

[[01-项目调研规划与开发建设记录/02-第二版-智能体升级/问答及调度业务主智能体/当前方案-Agent与RAG重建/00-索引·Agent与RAG重建]] · [[02-工程规范边界与验收治理/项目工程规范/00-索引·项目工程规范]] · [[06-代码工作区导航与链接映射/stages-08-agent-rag-README|README]]
