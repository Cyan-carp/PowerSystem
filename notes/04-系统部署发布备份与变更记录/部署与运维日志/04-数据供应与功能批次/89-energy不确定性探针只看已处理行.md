---
tags: [源网智联, 部署运维, 遥测链路, 发电量, 发布]
status: 现行
scope: 部署运维
updated: 2026-11-03
---

# energy 不确定性探针只看已处理行（89）

> 承接 [[85-遥测入库积压与发电量归零排查]] 与 [[88-入库取任务改为最新优先]]。本批只改一条 SQL 与它的守卫测试。

## 一、症状

api 容器每 10 秒输出一条：

```
{"level":"warn","msg":"energy_rebuild_failed","error":"timeout: context deadline exceeded"}
```

`energy` worker 的 `RebuildPending` 单轮预算 3 秒（`inboxTransactionBudget`），每轮重建 12 个 dirty day，跑不完就被 context 取消。

## 二、根因

`internal/energy/energy.go` 的 `uncertainExistsSQL`：

```sql
SELECT EXISTS(SELECT 1 FROM telemetry_inbox
  WHERE device_id=? AND ts_ms>=? AND ts_ms<=?
    AND business_state IS DISTINCT FROM 'success')
```

两个问题叠在一起：

1. **语义**：未处理的积压行 `business_state` 为 NULL，`IS DISTINCT FROM 'success'` 判为 true，被当成「该日数据不确定」。而这些行只是还没处理，不代表数据质量有问题。
2. **性能**：没有 `processed_at` 条件时，`idx_inbox_energy (device_id,ts_ms,id) WHERE processed_at IS NOT NULL` 这个部分索引用不上，查询退化为扫该设备当天全部行。2026-11-03 单设备单日积压 10 万级，单个 dirty day 就超过 3 秒预算。

## 三、改法

```sql
... AND processed_at IS NOT NULL AND business_state IS DISTINCT FROM 'success'
```

只加一个条件：

- 未处理行被排除，不再被误判为「不确定」；
- 查询重新走部分索引，只扫已处理的行（当天通常几百条）；
- 判据本身不动（仍然用 `IS DISTINCT FROM`，已处理但 state 为 NULL 的老行照旧算不确定）。

`internal/energy/energy_test.go` 的 `TestRebuildSQLKeepsDaySliceIndexAndExistenceProbe` 同步加断言，防止以后被改回不带 `processed_at` 的版本。

提交 `8e47d01`，源码快照 `bdd13926…`（635 文件），构建回执 `54d806fd…`。

## 四、上线与验证

api 镜像 `powersystem-api:energy-probe-20261103-r1` = `sha256:4a1d2e93…`，容器 `a62da6358084`，23:14 重建，manifest `eae6a89b…`。

验证：`ping=200`；`docker logs --since 60s | grep -c energy_rebuild_failed` = **0**（改前 60 秒内应有 6 次）。

## 五、没有解决的

这条修复只止住了超时告警，**不会让发电量恢复**。`energy_daily` 的样本查询同样要求 `processed_at IS NOT NULL AND business_state='success'`，当天样本全在积压里时聚合仍为空。要出数必须等入库真正恢复（见 88 篇）。

上游供数在 22:47 停止，此后没有新样本，因此本批修复对「重建能否出数」没有实测。

## 相关笔记

- [[00-当前系统状态]]
- [[85-遥测入库积压与发电量归零排查]]
- [[88-入库取任务改为最新优先]]
