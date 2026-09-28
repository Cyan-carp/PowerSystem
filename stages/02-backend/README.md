# 阶段二 · 后端主体

> 三台既有模拟器或新登记设备 → EMQX → 阶段二持久网关 → TDengine；独立后端订阅同一遥测主题，等待入库确认后更新 Redis、评估 PostgreSQL 告警规则并推送 WebSocket。阶段一 M1 代码和数据库保持独立。

## 一、环境与启动

依赖 Windows PowerShell 5.1+、Docker Desktop、Go 1.24+、项目已有 `.venv`。所有命令从仓库根目录执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\stages\02-backend\scripts\preflight-stage2.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\stages\02-backend\scripts\start-stage2.ps1 -SkipPreflight
```

预检沿用阶段一的 `powersystem` Compose 项目和 EMQX/TDengine 数据卷，增加 PostgreSQL、Redis 持久卷；自动生成本机 `.env` 缺少的阶段二密码与 JWT 密钥，编译服务、执行 PostgreSQL 迁移，并登记 `INV-1001`～`INV-1003`。阶段二时序库固定为 `powersystem_stage2`，不读写阶段一按运行编号建立的验收库。服务仅监听本机回环地址。重复执行预检不会清除已有数据。

服务日志和进程编号位于 `artifacts/stage2/runtime/`。停止本机进程：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\stages\02-backend\scripts\stop-stage2.ps1
```

该命令保留容器和数据卷。请勿用 `docker compose down -v` 清理阶段一或阶段二卷。

## 二、接口

公共接口：`GET /api/v1/ping`、`POST /api/v1/auth/register`、`POST /api/v1/auth/login`。注册账号为 `operator`，密码至少 8 位；登录返回 8 小时 JWT。所有业务 REST 接口使用 `Authorization: Bearer <token>`。

| 模块 | 路径 | 行为 |
| --- | --- | --- |
| 设备 | `/api/v1/devices`、`/api/v1/devices/:id` | 分页、分组筛选、创建、详情、更新元数据、软删除 |
| 遥测 | `/api/v1/devices/:id/telemetry` | `metric` 为 voltage/current/temperature/power；`start/end` 为 RFC3339，跨度 ≤24 小时、最多 5000 点 |
| 最新值 | `/api/v1/devices/:id/telemetry/latest` | Redis 中最近处理的遥测 |
| 规则 | `/api/v1/alarm-rules`、`/api/v1/alarm-rules/:id` | 每设备每指标一条规则；比较符 `>`、`>=`、`<`、`<=`，级别 urgent/major/minor |
| 告警 | `/api/v1/alarms`、`/api/v1/alarms/:id`、`/api/v1/alarms/:id/ack` | 分页、状态/级别筛选、详情、确认 |
| 总览 | `/api/v1/dashboard/summary` | 在线/离线/故障数、实时功率、活动告警和基础运行健康比例 |
| 实时 | `POST /api/v1/ws-ticket` → `/ws/realtime?ticket=...` | JWT 换取 60 秒有效的一次性票据，再建立 WebSocket；`/test/ws` 是本机测试页 |

响应统一为 `code/message/data`；成功 `code=0`，错误使用 HTTP 4xx/5xx 及设计稿错误码。列表默认 `page=1&page_size=20`，单页最多 100。设备编号与场站编号在建档后保持不变，以免与 TDengine 标签不一致。

## 三、数据与故障语义

- 阶段二网关仅接受 PostgreSQL 中已登记、未删除且场站编号匹配的设备。合法消息先落本地 SQLite，再 ACK MQTT；按设备数据库 ID 创建 TDengine 子表并批量写入，写库失败时保留待写消息。
- API 服务拥有独立的持久 MQTT 会话。消息先以 `(run_id, device_id, seq)` 写 PostgreSQL 收件表，再 ACK；后台工作者确认 TDengine 已入库后才处理告警。服务或数据库短暂不可用时，待处理消息可恢复；API 重启时会恢复 Redis 最新值缓存。
- 每设备每指标最多一条未恢复告警。语义：确认只记录人和时间；持续越限不重复；数值恢复后关闭本次事件；再越限才生成新事件。WebSocket 掉线后需重新换票据，并通过 REST 补查告警。
- 数据库设计与迁移脚本以 `deploy/postgres/001_init.sql`、`deploy/tdengine/init.sql` 为准。运行生成物和审查包始终留在被忽略的 `artifacts/` 下，不上传公开仓库。

## 四、验证与 M2

短时功能用例（自动创建一台设备并验证 MQTT→入库→告警→WebSocket）：

```powershell
.\.venv\Scripts\python.exe .\stages\02-backend\scripts\smoke-stage2.py --output .\artifacts\stage2\smoke-cases.csv
```

自动运行、故障注入、对账和审查包：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\stages\02-backend\scripts\stop-stage2.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\stages\02-backend\scripts\run-stage2-test.ps1
```

默认每台 1440 条、间隔 5 秒，真实运行约两小时；脚本自动重启 Broker、阶段二网关和 API。输出 `artifacts/stage2/<运行编号>/`，其中 `reconciliation.json`、`summary.md`、`cases.csv` 和 `review-bundle.zip` 用于 M2 审查。短测可用 `-SamplesPerDevice 12 -IntervalSeconds 1 -NoFaults`，但不能代替两小时 M2。故障和恢复时间记录在 `events.jsonl`；对账要求三台设备序号无缺失或重复、模拟器/网关队列及后端收件待处理均清零。

M2 已通过：

| 运行 | 结果 |
| --- | --- |
| 完整运行 `stage2_20260927_230451_2b69` | 实测 7220.4 秒，三台各 1440 条、共 4320/4320，30/30 用例通过 |
| 最终代码短回归 `stage2_20260928_010824_32e1` | 270/270、38/38 用例通过，并验证三类服务故障恢复 |

详细证据和阶段一回归见 [[开发阶段2-后端主体/04-M2测试与交付]]。

现有 TDengine 容器的 vnode 已满，直接运行阶段一脚本新建验收库会失败。可在不动现有数据卷的前提下执行隔离短回归：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\stages\02-backend\scripts\regress-stage1-isolated.ps1 -SamplesPerDevice 60 -IntervalSeconds 1
```

该脚本使用临时 TDengine 容器与原阶段一模拟器、网关，核对三台设备序号和队列，并注入 Broker 停止与网关重启；结果写入 `artifacts/stage1/<运行编号>/isolated-reconciliation.json`。已通过运行编号为 `stage1_iso_20260928_015551_7664`。阶段二本机测试仍只覆盖三台演示设备的两小时稳定性；NFR-01 的 200 台容量需要另行压测。阶段二笔记索引：[[开发阶段2-后端主体/00-索引·开发阶段2-后端主体]]。

## 五、阶段四前端所需增量

- `GET /api/v1/predictions?page=&page_size=` 需要 Bearer JWT，返回每台未删除设备的最新预测概要与 `stale`；无结果设备的预测字段为 `null`。有效结果按概率降序，过期和无结果置后。预测依据继续由设备详情预测接口提供。
- 本地 Vite 前端从 `http://127.0.0.1:5173` 跨端口代理 WebSocket 时，可在启动 API 前设置 `WS_ALLOWED_ORIGINS=http://127.0.0.1:5173`。该值只接受精确 http(s) Origin；空值维持同源校验。Vite 代理不改写浏览器 Origin。
- 这两项服务于阶段四第 13、14 周前端，不改变 M2 的既有验收结论。前端运行见 [[stages/04-frontend/README.md]]。
