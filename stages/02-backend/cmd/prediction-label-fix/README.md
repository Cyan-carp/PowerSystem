# prediction-label-fix

一次性修正 `prediction_outcomes` 的历史真值口径：把旧 `window_end + 2h` 标签重算为冻结的
`(window_end, window_end + 1h]`，并逐行写入审计表。**默认 dry-run，不写库。**

## 构建与交付（生产可执行性）

API 运行镜像是 Alpine，**不含 Go 编译器**，不能在生产容器里 `go run`。两种可行交付方式：

**方式 A — 预编译静态二进制（推荐用于一次性执行机）**

在装有 Go 的构建机编译，再把二进制带到执行环境：

```bash
cd stages/02-backend
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
  go build -trimpath -ldflags="-s -w" -o prediction-label-fix ./cmd/prediction-label-fix
```

**方式 B — 随 API 镜像交付**

`stages/02-backend/Dockerfile` 已在 build 阶段一并构建本二进制并复制到最终镜像
`/app/prediction-label-fix`；使用同一镜像即可在容器内直接运行，无需再编译。

**连接（PostgreSQL 容器本地 Unix socket trust）**

- 已证实可用的是 **PostgreSQL 容器内的本地 Unix socket trust 认证**：在 `postgres`
  容器内以 `psql -U powersystem -d powersystem` 即可免密连接（trust 模式下 DSN 不含密码）。
- **API 容器并未挂载 `/var/run/postgresql`**：不要声称可以在 API 容器内直接以 Unix socket
  连 postgres。**唯一已证实的路径**是：把静态二进制 `docker cp` 进 `postgres` 容器，
  在 postgres 容器内以 Unix socket DSN 运行；DB 连接一律走 postgres 容器的本地 Unix socket，
  而不是跨容器 TCP。
- **不要据此声称 API → postgres 的 Docker 网络 TCP 也免密**：该路径尚未被证实免密，
  不要把 `postgres://powersystem@postgres/powersystem?sslmode=disable` 这类经服务名的 TCP DSN
  写成“免密”。需要联网连接时，按实际凭证执行，不要自相矛盾。
- **无密码的 Unix socket DSN 可以写进文档作为样例**（下面 dry-run/apply 给出的就是这种
  不含任何口令的连接串，可直接展示），因为它本身不含任何秘密；但**凡真正含有口令/密钥的
  完整 DSN，一律不得写进命令历史、文档、聊天记录，也不得打印到日志或回显**。本命令只输出一行
  JSON 摘要，绝不回显任何真实凭据。
- 修正命令必须与已部署的 api 使用同一版本源码/同一个 `WindowFault` 规则；不要用旧分支的二进制跑修正。

## 前置

1. 已发布带 `label_rule` 的 api（`019_prediction_outcome_label_rule.sql` 会在 api 启动时应用）。
2. 确认 api 已用新代码写入 `label_rule='window_1h'`；迁移后旧 api 写入的行仍是 `legacy`，不会被误认。
3. 只对 `window_end_ms >= 1793604000000`（2026-11-02T07:20:00Z）且已满 1 小时的行操作。
4. dry-run 是只读查询，可直接在生产库执行；如担心争用可改用只读副本或维护窗口，但非强制。DSN 走 postgres 容器内无密码 Unix socket（见上），真正含口令的完整 DSN 不在命令/日志中出现。
5. 修正命令必须与已部署的 api 使用同一版本源码/同一个 `WindowFault` 规则；不要用旧分支的二进制跑修正。

## dry-run

```bash
# 无密码的 Unix socket DSN（postgres 容器内本地 trust，host 指向 socket 目录，不含任何口令，
# 可直接展示；真正含口令的完整 DSN 不得写进文档/日志/命令历史）
SOCKET_DSN='postgres://powersystem@localhost/powersystem?host=/var/run/postgresql&sslmode=disable'

# 唯一已证实路径：把随 API 镜像构建的静态二进制 docker cp 进 postgres 容器，
# 在 postgres 容器内以 Unix socket 运行（API 容器未挂载 /var/run/postgresql，不可在其内直连）
docker cp <api-container>:/app/prediction-label-fix /tmp/prediction-label-fix
docker cp /tmp/prediction-label-fix <postgres-container>:/tmp/prediction-label-fix
docker exec -i <postgres-container> env PREDICTION_QUALITY_DSN="$SOCKET_DSN" \
  /tmp/prediction-label-fix -max-rows 200000
```

dry-run 是只读查询，**可直接在生产库执行**；如仍担心与线上写入争用，可临时改用只读副本或维护窗口，
但**并非强制**。

输出字段：

| 字段 | 含义 |
| --- | --- |
| `pending_rows` | epoch 之后、已成熟、仍非 `window_1h` 的行数 |
| `scanned` | 本次实际扫描去重行数（稳定 keyset 游标保证每行最多一次） |
| `updated` | 实际写入行数（dry-run 恒为 0；apply 为成功 UPDATE 的行数） |
| `changed` | 标签值真正变化的行数 |
| `flipped_positive_to_negative` | 旧 2h 正标签、新 1h 负标签；这是“60–120 分钟提前叫出”被剔除的数量 |
| `flipped_negative_to_positive` | 旧负、新正（正常应为 0） |
| `remaining` | 真实未处理行数 = `pending_rows - scanned`（受 `-max-rows` 上限未处理的部分） |

## apply

```bash
# 同样以 postgres 容器 Unix socket DSN 在 postgres 容器内运行（无密码）
SOCKET_DSN='postgres://powersystem@localhost/powersystem?host=/var/run/postgresql&sslmode=disable'

docker exec -i <postgres-container> env PREDICTION_QUALITY_DSN="$SOCKET_DSN" \
  /tmp/prediction-label-fix -apply -actor '<操作者>' -reason '<变更单/原因>' -max-rows 200000
```

- 每批 500 行一个事务；失败整批回滚并退出 1，**已提交批次的计数写入运行表，未提交批次的计数不写入**；重跑从未修正的行继续（稳定 keyset 游标保证每行最多处理一次）。
- 每次运行在 `prediction_outcome_label_correction_runs` 留一行；逐行旧值/新值在
  `prediction_outcome_label_corrections`，唯一键 `(prediction_id, rule_to)` 保证幂等。
- 审计记录**只对应成功 UPDATE 的行**；被并发先行改写的行会在批内跳过且不产生审计。
- `completed` 只表示本次扫描完成；`remaining` 不为 0 时需再次运行。

## 修正后核验

```sql
-- 规则分布：修正后 legacy 只应剩 epoch 之前或未成熟行
SELECT label_rule, count(*) FROM prediction_outcomes GROUP BY 1 ORDER BY 2 DESC;

-- 2h→1h 翻转计数
SELECT status, scanned, updated, changed, flipped_positive_to_negative, flipped_negative_to_positive
FROM prediction_outcome_label_correction_runs ORDER BY started_at DESC LIMIT 5;

-- 抽查一条：旧值仍在审计表
SELECT * FROM prediction_outcome_label_corrections WHERE prediction_id = <id>;
```

日聚合表由下一轮 quality worker 覆盖（最多 1 小时）；批次 KPI 和按日批次线直接查
`prediction_outcomes`，不受该延迟影响。若需要立即刷新，重启 api 即可触发首轮 worker。

## 回滚（仅在复核发现修正错误时）

审计表保留旧值，可按 `run_id` 精确恢复；不要全表回写：

```sql
UPDATE prediction_outcomes AS o
SET actual_fault = c.actual_fault_before,
    fault_onset_ms = c.fault_onset_ms_before,
    label_rule = c.rule_from
FROM prediction_outcome_label_corrections AS c
WHERE o.prediction_id = c.prediction_id
  AND c.run_id = '<run_id>';
```

回滚后同样等待日聚合刷新，并重新跑 dry-run 核对。
