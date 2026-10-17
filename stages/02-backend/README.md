---
tags: [源网智联, 后端, 源码运行]
---

# 阶段二 · 后端主体

历史记录（2026-10-09）：自然语言问答候选新增认证只读 `GET /api/v1/alarms/statistics`。允许 `group_by=device|metric|level|status`、带时区 `start/end`、设备 ID 与受限状态筛选；默认滚动近 30 天，按 `triggered_at` 左闭右开计数，包含已恢复和未恢复告警。固定数据库聚合返回 `total_events/groups/complete`，不使用分页列表推算排名；006 迁移增加时间和设备复合索引。Agent 代理接受 `out_of_scope`；本地隔离启动可用 `MQTT_CLIENT_PREFIX` 区分持久订阅身份。验收见[本地交付](../../notes/01-项目调研规划与开发建设记录/第二版-智能体升级/问答及调度业务主智能体/废案-旧智能体方案/02-功能开发与阶段交付/05-自然语言问答改造/02-测试与本地交付.md)。

> 三台既有模拟器或新登记设备 → EMQX → 阶段二持久网关 → TDengine；独立后端订阅同一遥测主题，等待入库确认后更新 Redis、评估 PostgreSQL 告警规则并推送 WebSocket。阶段一 M1 代码和数据库保持独立。

## 一、环境与启动

依赖 Windows PowerShell 5.1+、Docker Desktop、Go 1.24+ 和可运行的 Python 3.12 环境。阶段一预检会准备 `.venv`；若它在含中文的仓库路径下启动失败，按下文烟测说明改用其他已安装依赖的解释器。所有命令从仓库根目录执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\stages\02-backend\scripts\preflight-stage2.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\stages\02-backend\scripts\start-stage2.ps1 -SkipPreflight
```

预检沿用阶段一的 `powersystem` Compose 项目和 EMQX/TDengine 数据卷，增加 PostgreSQL、Redis 持久卷；自动生成本机 `.env` 缺少的阶段二密码与 JWT 密钥，编译服务、执行 PostgreSQL 迁移，并登记 `INV-1001`～`INV-1003`。阶段二时序库固定为 `powersystem_stage2`，不读写阶段一按运行编号建立的验收库。服务仅监听本机回环地址。重复执行预检不会清除已有数据。

服务日志和进程编号位于 `artifacts/stage2-后端主体/runtime/`。停止本机进程：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\stages\02-backend\scripts\stop-stage2.ps1
```

该命令保留容器和数据卷。请勿用 `docker compose down -v` 清理阶段一或阶段二卷。

## 二、接口

公共接口：`GET /api/v1/ping`、`POST /api/v1/auth/register`、`POST /api/v1/auth/login`。注册账号为 `operator`，密码至少 8 位；登录返回 8 小时 JWT。所有业务 REST 接口使用 `Authorization: Bearer <token>`。阶段四 P0 起设备与告警规则写接口要求数据库当前角色为 `admin`，无令牌返回 401、越权返回 403；告警确认仍允许 `operator`。仅在服务器内使用 `stages/04-frontend/deploy/bootstrap-admin.sh` 创建独立管理员。

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
- 第一版阶段五将设备越限、AI 风险、确认、恢复和规则关闭事件与告警状态同事务写入 `business_notification_outbox`（`deploy/postgres/004_business_notifications.sql`）；API 后台按事件编号向内网飞书适配器投递，失败保留并重试。业务通知为至少一次投递，群内可能出现带同一事件编号的重复消息。目标服务器及群内验收见 [[notes/01-项目调研规划与开发建设记录/第一版-平台主体/05-功能开发与阶段交付/05-飞书告警通知/03-阶段五测试与交付]]。
- 数据库初始化与增量脚本依次为 `deploy/postgres/001_init.sql`、`002_predictions.sql`、`003_energy.sql`，时序库为 `deploy/tdengine/init.sql`。`003_energy.sql` 为已处理遥测补建待重算日期，API 后台将结果写入 `energy_daily`；部署库的 6 个跨日设备汇总已与原始去重样本独立复算一致。`deploy/postgres/backfill_energy.sql` 可反复标记所有已处理日期，由 API 后台重算；本轮远端连续执行两次均成功。运行生成物和审查包始终留在被忽略的 `artifacts/` 下，不上传公开仓库。

上述历史电量说明记录 `001`～`003` 的阶段四状态；当前 API 还会执行 `004_business_notifications.sql`。在 R730xd `/opt/powersystem` 中需要主动重算留存历史时执行：

```bash
docker compose --env-file .env -f stages/04-frontend/deploy/compose.yaml exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U powersystem -d powersystem -f /dev/stdin \
  < stages/02-backend/deploy/postgres/backfill_energy.sql
```

## 四、验证与 M2

短时功能用例（自动创建一台设备并验证 MQTT→入库→告警→WebSocket）：

```powershell
$python = 'C:\path\to\python3.12.exe' # 改为本机已安装 paho-mqtt 的可用解释器
& $python .\stages\02-backend\scripts\smoke-stage2.py --output .\artifacts\stage2\smoke-cases.csv
```

自动运行、故障注入、对账和审查包：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\stages\02-backend\scripts\stop-stage2.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\stages\02-backend\scripts\run-stage2-test.ps1
```

默认每台 1440 条、间隔 5 秒，真实运行约两小时；脚本自动重启 Broker、阶段二网关和 API。输出 `artifacts/stage2-后端主体/<运行编号>/`，其中 `reconciliation.json`、`summary.md`、`cases.csv` 和 `review-bundle.zip` 用于 M2 审查。短测可用 `-SamplesPerDevice 12 -IntervalSeconds 1 -NoFaults`，但不能代替两小时 M2。故障和恢复时间记录在 `events.jsonl`；对账要求三台设备序号无缺失或重复、模拟器/网关队列及后端收件待处理均清零。

M2 已通过：

| 运行 | 结果 |
| --- | --- |
| 完整运行 `stage2_20260927_230451_2b69` | 实测 7220.4 秒，三台各 1440 条、共 4320/4320，30/30 用例通过 |
| 最终代码短回归 `stage2_20260928_010824_32e1` | 270/270、38/38 用例通过，并验证三类服务故障恢复 |

详细证据和阶段一回归见 [04-M2测试与交付](../../notes/01-项目调研规划与开发建设记录/第一版-平台主体/05-功能开发与阶段交付/02-后端主体/04-M2测试与交付.md)。

现有 TDengine 容器的 vnode 已满，直接运行阶段一脚本新建验收库会失败。可在不动现有数据卷的前提下执行隔离短回归：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\stages\02-backend\scripts\regress-stage1-isolated.ps1 -SamplesPerDevice 60 -IntervalSeconds 1
```

该脚本使用临时 TDengine 容器与原阶段一模拟器、网关，核对三台设备序号和队列，并注入 Broker 停止与网关重启；结果写入 `artifacts/stage1-数据链路/<运行编号>/isolated-reconciliation.json`。已通过运行编号为 `stage1_iso_20260928_015551_7664`。阶段二本机测试仍只覆盖三台演示设备的两小时稳定性；NFR-01 的 200 台容量需要另行压测。阶段二笔记索引：[00-索引·开发阶段2-后端主体](../../notes/01-项目调研规划与开发建设记录/第一版-平台主体/05-功能开发与阶段交付/02-后端主体/00-索引·开发阶段2-后端主体.md)。

## 五、阶段四前端所需增量

- `GET /api/v1/predictions?page=&page_size=` 需要 Bearer JWT，返回每台未删除设备的最新预测概要与 `stale`；无结果设备的预测字段为 `null`。有效结果按概率降序，过期和无结果置后。预测依据继续由设备详情预测接口提供。
- 本地 Vite 前端从 `http://127.0.0.1:5173` 跨端口代理 WebSocket 时，可在启动 API 前设置 `WS_ALLOWED_ORIGINS=http://127.0.0.1:5173`。该值只接受精确 http(s) Origin；空值维持同源校验。Vite 代理不改写浏览器 Origin。
- 这两项服务于阶段四第 13、14 周前端，不改变 M2 的既有验收结论。前端运行见 [README.md](../04-frontend/README.md)。

## 第二版阶段一兼容扩展

`POST /api/v1/agent/chat` 复用当前用户 JWT，Agent 未开启或不可用时返回503，不影响既有业务。设备列表增加可选keyword，告警列表增加device_id/start/end；默认查询兼容原接口。运行与测试见[阶段七运行说明](../07-agent/README.md)。

现有烟测的设备与规则CRUD需管理员，普通用户仍用于查询和确认告警；新增 `--admin-token-file` 参数从忽略的私有文件读取测试管理员JWT，不输出凭据。2026-09-30在Agent正常及停止后两次38/38通过，证据见[V2-M1测试与交付](../../notes/01-项目调研规划与开发建设记录/第二版-智能体升级/问答及调度业务主智能体/废案-旧智能体方案/02-功能开发与阶段交付/01-基础服务与只读工具链/05-V2-M1测试与交付.md)。

## 第二版阶段二兼容扩展

新增 `005_agent_interpretations.sql` 为兼容迁移，仅增加任务、快照、监控与已读表。`AGENT_INTERPRET_ENABLED=true` 启用独立扫描、两个工作协程与监控 Stream 消费；首次以当前最大告警 ID 为基线，重启继续处理，原告警事务不调用 Agent。

用户解读列表、详情和已读接口位于 `/api/v1/agent/interpretations`，使用当前 JWT；模型状态及检测限管理员，检测每分钟一次。内部 HTTP 45 秒预算、永久配置错误不重试、暂时错误最多三次。Agent 停机及解读表单独故障时的两轮既有业务回归均为 38/38，见[阶段二交付](../../notes/01-项目调研规划与开发建设记录/第二版-智能体升级/问答及调度业务主智能体/废案-旧智能体方案/02-功能开发与阶段交付/02-告警智能解读与大屏联动/05-V2-M2测试与交付.md)。

## 第二版阶段三兼容扩展

第三阶段增加 `GET /api/v1/agent/knowledge/:id`，沿用 JWT，并校验受控片段 ID、返回体大小、上游超时和来源一致性。问答响应兼容扩展知识库状态、联网状态及三种证据类型，不增加业务表。运行与验收入口仍为[阶段七 README](../07-agent/README.md)。

## 通用问答重建（2026-10-11）

新问答由阶段8独立服务接管；旧方案笔记已归档。阶段7历史事件解读与页面帮助保持独立。运行与接口说明见 [[stages/08-agent-rag/README]]；新笔记见 [[notes/01-项目调研规划与开发建设记录/第二版-智能体升级/问答及调度业务主智能体/当前方案-Agent与RAG重建/00-索引·Agent与RAG重建]]。通用问答及BND-M1已在8.138.42.212验收发布；核验接口/取证面板、实际证据与限制见[[notes/01-项目调研规划与开发建设记录/第二版-智能体升级/问答及调度业务主智能体/智能体边界建设/04-功能开发与阶段交付/01-取证提示与主动核验/03-阶段1测试与交付]]。

## BND-M2发布增量（2026-10-11）

Go保留身份注入、接纳和out_of_scope透传；前端增加程序回复标识及核验入口限制。BND-M2已在R730XD验收发布，公网入口8.138.42.212。本机及最终Linux Agent98项、前端19项、类型构建和Go全包通过；最终公网11类/19项、重启历史10项、实际公网浏览器9项、源码/镜像实际回退、最终加密备份隔离恢复及300秒固定镜像观察通过。五页阶段二笔记已同步，未提交或推送Git。设计和证据见[[notes/01-项目调研规划与开发建设记录/第二版-智能体升级/问答及调度业务主智能体/智能体边界建设/04-功能开发与阶段交付/02-日常问题调用边界/00-索引·日常问题调用边界]]及[[notes/01-项目调研规划与开发建设记录/第二版-智能体升级/问答及调度业务主智能体/智能体边界建设/04-功能开发与阶段交付/02-日常问题调用边界/03-阶段2测试与交付]]；操作命令见[[stages/08-agent-rag/README]]。

## 多智能体接口规划入口（2026-10-12）

[[notes/01-项目调研规划与开发建设记录/第二版-智能体升级/多智能体协作建设/00-索引·多智能体协作建设]]规划Go服务端签名身份、固定任务查询/取消及后续管理员审批代理；当前接口尚未实现，本次不改后端代码或迁移。现有JWT只接受operator/admin；用户任务不接入现有全体WebSocket广播，平台GET权限及业务库只读边界保持。

## MA-M1本机实施与交付历史（2026-10-12，阶段二启动前）

2026-10-12：MA-M1本机技术验收正式签收：MA-T01～MA-T07及MA-C01～MA-C19在声明范围通过，五页、相关入口与交付审计已同步。本次到MA-M1为止；MA-M2～MA-M4未启动，不提交/推送、不更新服务器、不导入知识库。 正式事实：[[notes/01-项目调研规划与开发建设记录/第二版-智能体升级/多智能体协作建设/04-基础运维模型/02-功能开发与阶段交付/01-双智能体只读闭环/03-MA-M1测试与交付]]；运行命令唯一入口：[[stages/09-ops-agent/README]]。

## MA-M2完成与签收历史（2026-10-12，MA-M3启动前）

2026-10-12：MA-T08～MA-T12及MA-C20～MA-C25在本机隔离环境＋R730XD项目资产范围完成，MA-M2技术验收正式签收。21项资产/35份快照、11项R0工具、真实权限与双模型诊断、实际页面及受影响回归通过；公网主业务未发布，MA-M3/M4未启动。当前判定见[[notes/01-项目调研规划与开发建设记录/第二版-智能体升级/多智能体协作建设/04-基础运维模型/02-功能开发与阶段交付/02-基础设施只读诊断/03-MA-M2测试与交付]]，命令仅由[[stages/09-ops-agent/README]]维护。

## MA-M3实施状态（2026-10-12）

MA-T13～MA-T16及MA-C26～MA-C31在本机隔离演练范围完成，MA-M3本机技术验收通过，可进入MA-M4。阶段9共108项、阶段8/BND共100项、阶段7离线113项、Go全包、前端30项/类型/构建通过；真实双模型委派、当前Go身份、Docker重启与Chrome页面均已验证。演练写窗口已关闭，服务器写能力及公网主业务未发布，MA-M4未启动。

当前事实见[[notes/01-项目调研规划与开发建设记录/第二版-智能体升级/多智能体协作建设/04-基础运维模型/02-功能开发与阶段交付/03-受控运维执行/03-MA-M3测试与交付]]；阶段入口[[notes/01-项目调研规划与开发建设记录/第二版-智能体升级/多智能体协作建设/04-基础运维模型/02-功能开发与阶段交付/03-受控运维执行/00-索引·受控运维执行]]，命令仅由[[stages/09-ops-agent/README]]维护。范围仅本机隔离演练；临时写窗口已关闭，保留审计和旧失败。不提交/推送、不导入知识库，不变更服务器或公网。


## SEC-M2本机集成

SEC-M2复用本人任务代理与当前用户表身份，不新增自由送审接口。cmd/ma-local-api仅作为本机验收代理，不启动原MQTT/预测/告警通知工作进程。专用回归库隔离，Go全包与实际跨用户404须单独验收。

当前状态见[[notes/01-项目调研规划与开发建设记录/第二版-智能体升级/多智能体协作建设/05-安全审查模型/05-功能开发与阶段交付/02-强制门禁集成/03-SEC-M2测试与交付]]；本阶段统一运行命令见[阶段10源码README](../10-security-agent/README.md)。历史“尚未接入”段落指建设前状态，不能作当前源码判定。

## 相关笔记

- [[notes/02-工程规范边界与验收治理/项目工程规范]]
- [[notes/01-项目调研规划与开发建设记录/第二版-智能体升级/多智能体协作建设/00-索引·多智能体协作建设]]

## 2026-10-15 API故障恢复与现行收尾

17:33基础编排重建导致API版本回退及助手配置遗漏；本次按已签收r6镜像恢复API并加载阶段7/8/9完整配置，保留当前前端、通知与模拟数据。两公网入口的会话/任务列表及详情、核心只读业务、HTTPS/WSS和一次技术模型问答已复验。完整恢复、备份及观察证据见[[notes/04-系统部署发布备份与变更记录/部署与运维/API版本回退恢复记录-2026-10-15]]；现行待办按[[notes/04-系统部署发布备份与变更记录/部署与运维/项目收尾核对-2026-10-15]]核对，历史阶段未勾选项不覆盖后来签收。
