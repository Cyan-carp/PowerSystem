---
tags: [源网智联, 后端, 源码运行]
---

# 阶段二 · 后端主体

遥测链路修复的四阶段方案与验收见[专项总索引](../../notes/01-项目调研规划与开发建设记录/专项修复-遥测链路可靠性/00-索引·遥测链路可靠性修复.md)，新对话从[接续入口](../../notes/01-项目调研规划与开发建设记录/专项修复-遥测链路可靠性/03-跨对话交接/00-TR当前接续入口.md)开始；新增实际命令仍维护本README，运行状态只看[唯一状态页](../../notes/00-当前系统状态.md)。

2026-10-21告警v2已发布（旧业务告警10-22清理完成）：`AGENT_INTERPRET_PROTOCOL=v2`启用专属事件协议；当次发布的`SIMULATION_ALARM_MODE=controlled`核对fleet中143台设备并抑制自然新告警，`paused`仅停止新演示，已登记门控仍保留（历史行为）。迁移009/010/011分别维护演示日程、持久调用预算和维护冻结。自然预测仍保存，真实设备/平台监控不受每日五条配额影响。`GET /api/v1/alarms`及statistics支持`origin=normal|simulation`，statistics另支持`group_by=origin`；默认总数不变。

2026-10-22 自然规则切换（**未生产发布**）：新增`deploy/postgres/012_natural_alarm_rules.sql`，为已核对的143台合成设备各建四类规则（五站共20类，`ON CONFLICT DO NOTHING`不覆盖既有规则）。`ProcessSample`不再对合成设备一律跳过自然新建，而是只在五个北京时间窗口内、由当日轮换设备、且当日策略设备自然/演示记录少于5条时允许新建；五条限额只统计策略表内合成设备，真实设备与平台监控不受限。`SIMULATION_ALARM_MODE=controlled`仅作为兼容输入，worker只结束历史演示，不再新建演示告警；旧演示记录保留。12个候选文件已按上传规范同步服务器源码、留原文件副本并核对LF/SHA；未构建候选或切换生产。012已在服务器上隔离断网的PostgreSQL夹具验证语法和幂等，但尚未在候选API镜像的实际迁移入口执行。

清空旧告警仅由受控发布驱动在备份、冻结、排空后执行`scripts/reset-business-alarms.sql`；保留序列，禁止手工TRUNCATE/重置ID。定向短测为`go test ./internal/api ./internal/service -run 'Test(V2|Fact|EventFacts|Simulation|NormalizeSimulation)' -count=1`，真实PG/Redis夹具仅由隔离驱动运行。设计和当前发布结论见[官方解读与五次演示](../../notes/04-系统部署发布备份与变更记录/部署与运维日志/03-智能体与模型调用链/30-官方独立告警解读与每日五次演示.md)。

启动兼容注意：既有`store.Migrate`按ASCII分号拆语句，注释/字符串中的分号也会被拆开；复杂SQL不能只用psql通过就投入此入口。009至011已经增加实际应用迁移入口的隔离短测。本轮只修010注释，不扩大到全库迁移器重写。

历史记录（2026-10-09）：自然语言问答候选新增认证只读 `GET /api/v1/alarms/statistics`。允许 `group_by=device|metric|level|status`、带时区 `start/end`、设备 ID 与受限状态筛选；默认滚动近 30 天，按 `triggered_at` 左闭右开计数，包含已恢复和未恢复告警。固定数据库聚合返回 `total_events/groups/complete`，不使用分页列表推算排名；006 迁移增加时间和设备复合索引。Agent 代理接受 `out_of_scope`；本地隔离启动可用 `MQTT_CLIENT_PREFIX` 区分持久订阅身份。验收见[本地交付](../../notes/01-项目调研规划与开发建设记录/第二版-智能体升级/问答及调度业务主智能体/废案-旧智能体方案/02-功能开发与阶段交付/05-自然语言问答改造/02-测试与本地交付.md)。

> 三台既有模拟器或新登记设备 → EMQX → 阶段二持久网关 → TDengine；独立后端订阅同一遥测主题，等待入库确认后更新 Redis、评估 PostgreSQL 告警规则并推送 WebSocket。阶段一 M1 代码和数据库保持独立。

## 第四版设计与交接

本模块第四版设计和后续实施按[第四版契约](../../notes/01-项目调研规划与开发建设记录/第四版-智能助手行动化重构/01-立项与总体设计/03-架构与接口契约.md)与[阶段交接](../../notes/01-项目调研规划与开发建设记录/第四版-智能助手行动化重构/03-跨对话交接/00-当前接续入口.md)接续；源码沿用本目录。当前状态见[当前系统状态](../../notes/00-当前系统状态.md)。

## 一、环境与启动

V4-S02提前提供最小只读`GET /api/v1/reports/energy`：`start_date/end_date`为北京时间自然日、end不含、1..31天；`scope/station_code/device_id`筛选取交集。仅汇总`energy_daily`中有有效段且值有限非负的记录，返回逐日、总量、实际窗口、聚合时间和缺测；无记录不补零，不作额外两位小数舍入。无新迁移，不生成文件，S05仍负责报表产物验收。补验使用`S02_ENERGY_TEST_DATABASE_URL`指向本轮新建回环25432隔离库；`go test ./internal/api -run TestEnergyReport -count=1 -v`覆盖真实PG DATE/小数/非有限值与接口鉴权。未配置专用测试库时该PG用例跳过，不能计通过。

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
| 设备 | `/api/v1/devices`、`/api/v1/devices/:id` | 分页、分组筛选、可选 `scope`/`station_code`、创建、详情、更新元数据、软删除 |
| 场站 | `/api/v1/stations` | 从共享清单返回场站名称、范围、期望设备数和有效台账登记数 |
| 遥测 | `/api/v1/devices/:id/telemetry` | `metric` 为 voltage/current/temperature/power；`start/end` 为 RFC3339，跨度 ≤24 小时、最多 5000 点 |
| 最新值 | `/api/v1/devices/:id/telemetry/latest` | Redis 中最近处理的遥测 |
| 规则 | `/api/v1/alarm-rules`、`/api/v1/alarm-rules/:id` | 每设备每指标一条规则；比较符 `>`、`>=`、`<`、`<=`，级别 urgent/major/minor |
| 告警 | `/api/v1/alarms`、`/api/v1/alarms/:id`、`/api/v1/alarms/:id/ack` | 分页、状态/级别筛选、详情、确认 |
| 总览 | `/api/v1/dashboard/summary` | 在线/离线/故障数、实时功率、活动告警和基础运行健康比例 |
| 范围总览 | `/api/v1/dashboard/overview` | 按 `scope=all|business|baseline` 和可选 `station_code` 返回当前聚合、日电量、完整性与场站汇总 |
| 实时 | `POST /api/v1/ws-ticket` → `/ws/realtime?ticket=...` | JWT 换取 60 秒有效的一次性票据，再建立 WebSocket；`/test/ws` 是本机测试页 |

响应统一为 `code/message/data`；成功 `code=0`，错误使用 HTTP 4xx/5xx 及设计稿错误码。列表默认 `page=1&page_size=20`，单页最多 100。设备编号与场站编号在建档后保持不变，以免与 TDengine 标签不一致。

`scope` 省略时仍为 `all`，保持旧调用兼容。`/dashboard/overview` 只批量读取当前快照，并从现有 `energy_daily` 汇总当日电量，不扫描全天原始遥测；`partial=true` 表示登记数少于共享清单期望数，或当前有效观测少于已登记设备。缺失功率、电量或健康度返回 `null`，不补成零。

4 场站台账工具当前仅提供离线只读 dry-run，不连接数据库、不执行 SQL：

```powershell
python .\stages\02-backend\scripts\plan-fleet-import.py
python .\stages\02-backend\scripts\plan-fleet-import.py --existing .\path\to\exported-devices.json
```

无冲突退出 0，发现身份冲突退出 2，输入非法退出 1。上述 planner 始终只读。

正式导入 SQL 由 `render-fleet-import.py` 从同一清单生成，只处理业务设备，使用单个事务、五秒锁等待、身份冲突检查与 `ON CONFLICT DO NOTHING`；不覆盖现有行，不改原三台或应用表结构。先保存备份和 dry-run，再在已授权的目标库执行：

```powershell
python stages/02-backend/scripts/render-fleet-import.py --output artifacts/stage2/import-140-<运行编号>.sql
python -m unittest discover -s stages/02-backend/scripts -p test_render_fleet_import.py -q
```

SQL 以 `psql -v ON_ERROR_STOP=1` 执行；生产由现有容器内 `psql -U powersystem -d powersystem` 从标准输入读取，不把数据库密码写在命令中。脚本拒绝覆盖已有输出文件，任何设备身份冲突都会整体回滚。已提交的数据不会随应用镜像回退而自动删除。

### 设备扩容最小验证入口

从仓库根目录执行。先按阶段一 README 运行七台离线模拟器，以下命令复用其已创建的证据目录。显式绝对路径让验证不依赖 API 默认工作目录，不需要读取 `.env` 或启动服务栈：

```powershell
$fleetSmokeRoot = (Get-Location).Path
$env:FLEET_CONFIG_PATH = (Resolve-Path 'stages/01-data-chain/simulator/fixtures/fleet-smoke-7.json').Path
$env:FLEET_SMOKE_OUTPUT = Join-Path $fleetSmokeRoot 'artifacts/stage2/fleet-minimal-<运行编号>/api-responses.json'
python stages/02-backend/scripts/plan-fleet-import.py --fleet $env:FLEET_CONFIG_PATH
Push-Location stages/02-backend
try { go test ./internal/api -run '^TestFleetMinimalSmoke$' -count=1 -timeout 90s -v } finally { Pop-Location }
python stages/02-backend/scripts/smoke-fleet-agent.py --responses $env:FLEET_SMOKE_OUTPUT
```

Go 冒烟通过 `Server.New` 装载指定清单，使用独立内存 SQLite 承载七台测试行，调用真实 stations/devices/alarms/overview 处理函数共 13 次，导出三档范围响应。Redis 缺席，因此 `observed=0`、`partial=true` 是预期空观测结果；不验证 PostgreSQL 方言、认证中间件或实时供数联动。环境变量只影响当前 PowerShell 会话，运行后可关闭该会话。

Agent 脚本用 `httpx.MockTransport` 回放上述响应，阶段 8 调用真实 `Tools.execute`，阶段 9 调用真实 `Executor.call`，各查询一次业务场站和基线总览；不请求模型。第三步已落实生产清单挂载和 140 台正式导入，现行发布事实见 [扩容上线记录](../../notes/04-系统部署发布备份与变更记录/部署与运维日志/04-供数与业务批次/26-四场站140台设备扩容上线.md)。

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

以下为v1历史策略，v2统一以本页顶部持久两次调用上限为准。用户解读列表、详情和已读接口位于 `/api/v1/agent/interpretations`，使用当前 JWT；模型状态及检测限管理员，检测每分钟一次。内部 HTTP 45 秒预算、永久配置错误不重试、暂时错误最多三次。Agent 停机及解读表单独故障时的两轮既有业务回归均为 38/38，见[阶段二交付](../../notes/01-项目调研规划与开发建设记录/第二版-智能体升级/问答及调度业务主智能体/废案-旧智能体方案/02-功能开发与阶段交付/02-告警智能解读与大屏联动/05-V2-M2测试与交付.md)。

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

## 2026-10-21 · 告警解读预算与截断重试

版本 `interpretation-budget-20261021-r1` 已上线。`answer_truncated` 首次失败可额外重试一次（attempts<2），其余原校验失败仍不重试；管理员probe明确要求 `interpretation=true`。Agent仅解读使用4096预算，模型35秒/总40秒，API45秒保持。38项解读测试与线上短解读通过，正式记录见 [[notes/04-系统部署发布备份与变更记录/部署与运维日志/03-智能体与模型调用链/18-解读模型通道兼容与输出预算（原18+28+29）]]。

## 相关笔记

- [[notes/02-工程规范边界与验收治理/项目工程规范]]
- [[notes/01-项目调研规划与开发建设记录/第二版-智能体升级/多智能体协作建设/00-索引·多智能体协作建设]]

## 2026-10-15 API故障恢复与现行收尾

17:33基础编排重建导致API版本回退及助手配置遗漏；本次按已签收r6镜像恢复API并加载阶段7/8/9完整配置，保留当前前端、通知与模拟数据。两公网入口的会话/任务列表及详情、核心只读业务、HTTPS/WSS和一次技术模型问答已复验。完整恢复、备份及观察证据见[[notes/04-系统部署发布备份与变更记录/部署与运维日志/01-部署基线与仓库发行/06-API版本回退恢复记录]]；现行待办按[[notes/04-系统部署发布备份与变更记录/部署与运维日志/01-部署基线与仓库发行/09-项目收尾核对]]核对，历史阶段未勾选项不覆盖后来签收。

## S03持久任务与事件流交接

后端运行协议、恢复与验收边界见[S03阶段交接](../../notes/01-项目调研规划与开发建设记录/第四版-智能助手行动化重构/03-跨对话交接/S03-阶段交接.md)。当前状态见[当前系统状态](../../notes/00-当前系统状态.md)。独立`ma-run/v1`只读绑定与旧ma/v1分开，V4_RUNS_ENABLED默认false；关闭新受理保留查询/核查，不回退重放。

### S03网关验收命令

在本阶段目录执行`go test ./...`和`go vet ./...`；真energy_daily回归可设置`S03_ENERGY_TEST_DATABASE_URL`，仅允许127.0.0.1:25433/v4_s03_*_test（原S02变量与严格隔离白名单兼容）。JSON/SSE代理透传202/400/409/410；游标为64位ASCII非负十进制，JWT不入URL。

## V4-S04工作台交接导航

工作台实现、隔离验收与恢复边界见 [[notes/01-项目调研规划与开发建设记录/第四版-智能助手行动化重构/03-跨对话交接/S04-阶段交接]]；当前状态见 [[notes/00-当前系统状态]]。本机复现说明：[[stages/04-frontend/deploy/v4-workbench-acceptance]]。此处不复述生产状态。

S05 隔离交付与适用边界见 [S05阶段交接](../../notes/01-项目调研规划与开发建设记录/第四版-智能助手行动化重构/03-跨对话交接/S05-阶段交接.md)。当前状态以唯一状态页为准。

## V4-S06安全审查与业务操作

[阶段交接与验收边界](../../notes/01-项目调研规划与开发建设记录/第四版-智能助手行动化重构/03-跨对话交接/S06-阶段交接.md)提供本轮实现、必要验证、人工补验及停服清单。当前状态见 [唯一状态](../../notes/00-当前系统状态.md)。

V4-S07受控运维的实现范围、必要验收与交接门槛见 [S07测试与交付](../../notes/01-项目调研规划与开发建设记录/第四版-智能助手行动化重构/02-分阶段实施/07-S07受控运维闭环/02-V4-S07测试与交付.md)。

V4-S08定期任务、只读预演与验收门槛见 [S08测试与交付](../../notes/01-项目调研规划与开发建设记录/第四版-智能助手行动化重构/02-分阶段实施/08-S08定期任务与方案预演/02-V4-S08测试与交付.md)。

V4-S08八项交接与接续边界见 [S08阶段交接](../../notes/01-项目调研规划与开发建设记录/第四版-智能助手行动化重构/03-跨对话交接/S08-阶段交接.md)。

S09联合整改、必要验证与未发布交接见 [S09阶段交接](../../notes/01-项目调研规划与开发建设记录/第四版-智能助手行动化重构/03-跨对话交接/S09-阶段交接.md)，具体候选/备份/回退与观察提案见 [发布准备](../../stages/04-frontend/deploy/v4-release-readiness.md)。当前状态以唯一系统状态页为准。

## TR-S01 隔离验收与迁移

契约、逐读写者清单和实际验收见[TR-S01交接](../../notes/01-项目调研规划与开发建设记录/专项修复-遥测链路可靠性/02-分阶段实施/01-S01监控与失败状态/04-TR-S01阶段交接.md)。当前运行状态只看[唯一状态页](../../notes/00-当前系统状态.md)。

`014_telemetry_reliability.sql`由`store.MigrationPaths`接入API启动，整份014在同一事务执行。保留旧列/索引/历史NULL状态，不清洗历史；首次索引构建仍需维护窗口评估。迁移后仅前滚：先停所有旧worker，再迁移并启动新候选；禁止直接启动旧API回退。`-migrate-only`完成schema、种子及适用自然规则后退出，不再要求Redis/TD可用。本轮未执行生产迁移。

### 专用依赖

Linux/WSL夹具使用PostgreSQL16、Redis7、Prometheus2.45和官方独立Alertmanager0.26；仅绑定15439、16389、18089、19090、19093的loopback端口。依赖须解包到`$TR_S01_FIXTURE_ROOT/deps/root/usr/`，PostgreSQL库位于其`lib/x86_64-linux-gnu`；不得指向现有业务库。发行版Alertmanager可能硬编码系统模板，使用官方独立二进制，保持名称`prometheus-alertmanager`。

```bash
export TR_S01_FIXTURE_ROOT=/home/user23103/tr-s01-fixture
bash stages/02-backend/scripts/tr-s01-fixture.sh start
# 仅重置本夹具两库及16389 Redis；保留原证据，必要重测时才执行
bash stages/02-backend/scripts/tr-s01-fixture.sh reset
```

### 构建与真实依赖验证

以下PowerShell从阶段二目录执行。证据目录应事先建立并在每次修订后另建批次；测试缺依赖会失败，不能把SKIP算作通过。

```powershell
go build -o ../../artifacts/telemetry-reliability/TR-S01-local/api.exe ./cmd/api
$env:TR_S01_API_BIN=(Resolve-Path ../../artifacts/telemetry-reliability/TR-S01-local/api.exe).Path
$env:TR_S01_TEST_DSN='postgres://tr_s01@127.0.0.1:15439/tr_s01_test?sslmode=disable'
go test -tags tr_s01 ./internal/api -run '^TestTRS01Isolated$' -v -count=1 -timeout 90s
```

该测试经真实API二进制验证空库首装、重复与旧数据升级；真实PG/Redis，TD为loopback协议夹具，不含MQTT、模型或外部通知。普通单测不用`tr_s01`标签。必要定向回归为电量报表、overview聚合、遥测解析及`TestTelemetry*`；不重跑其他智能体矩阵。

### 告警闭环与边界

以下从仓库根目录在Linux/WSL执行；`--evidence`必须是新目录。使用实际`stages/04-frontend/deploy/alerts.yml`和15秒抓取/求值，预算在执行前写入`budget-before.json`。

```bash
python3 stages/02-backend/scripts/test-telemetry-alerts.py \
  --bin-dir /home/user23103/tr-s01-fixture/deps/root/usr/bin \
  --evidence artifacts/telemetry-reliability/TR-S01-local/alerts-r1
python3 stages/02-backend/scripts/telemetry-rule-boundaries.py /home/user23103/tr-s01-fixture/rules.json
/home/user23103/tr-s01-fixture/deps/root/usr/bin/promtool test rules /home/user23103/tr-s01-fixture/rules.json
bash -n stages/02-backend/scripts/tr-s01-fixture.sh
bash stages/02-backend/scripts/tr-s01-fixture.sh stop
```

闭环只路由到本地假webhook。生产Alertmanager、飞书送达和维护协议不由此签收。新metrics由独立5秒watchdog生成，单轮4秒预算；handler只读内存快照。固定15秒新鲜度沿用本专题当前契约，不能直接外推上报周期更长的真实设备。

`business_state`的pending/retry仍由当前worker消费；依赖重试暂为1秒，无复杂退避/公平调度。`processed_at`保留兼容的终结标记，只有`business_state='success'`可解释为业务成功。历史NULL行不重分类；legacy恢复须TD再验证，否则missing/unknown。

电量数值保留legacy来源，隔离/重试不纳入积分。`source_quality=legacy_unknown/provisional`会使报表日/组/总计`complete=false`并给出`source_uncertain`；数值仍保留。`verified`仅说明来源状态经过验证；`complete`仍指所选设备日聚合可用，不承诺全天无采样缺口。summary分别返回今日与保留历史的暂定标记，overview返回今日标记。显式历史重建只使用`deploy/postgres/backfill_energy.sql`，本轮只在隔离库执行，禁止据本README重放生产历史。

## TR-S02 维护、时限与隔离验证

实现契约与八项交接见[TR-S02交接](../../notes/01-项目调研规划与开发建设记录/专项修复-遥测链路可靠性/02-分阶段实施/02-S02维护门禁与超时/04-TR-S02阶段交接.md)，当前状态只认[唯一状态页](../../notes/00-当前系统状态.md)。S01迁移只允许前滚的限制保留；本轮无新迁移、不发布、不重放历史告警或实发外部通知。

维护门禁区分关闭/开启/SQL错误/缺id=1，后三者停业务写。原`UPDATE enabled=true`等待业务事务的共享行锁后构成冻结边界；原在途解读任务排空仍须保留。原始MQTT接收与网关TD写不随之停止，不能把维护当成整个平台停机；不自动解除维护。

`/live`为独立存活接口；`/api/v1/ping`保留响应格式并在实际Run时同`/ready`强制就绪，避免原容器healthcheck在早启HTTP时提前200。额外返回`telemetry_ready/telemetry_phase`；`/ready`为真实数据就绪，恢复未完成、依赖未知/不可用、维护或预期集合覆盖不足均503。TD每轮独立限1秒SELECT只读探针；probe_td_state区分0未初始化/1成功/2错误/3超时/4未执行，固定计数、最近时间及分钟限流日志可诊断。新鲜度从探针测量起点持续增长，不能冻结在快照年龄。恢复失败每5秒重试一次受限恢复，保持未就绪；schema/依赖初始化总30秒、单次恢复30秒、单设备2秒。大库初始化超时须按S04统一维护窗口处理，不通过延长健康等待伪造新鲜。

缓存`snapshot_source=td_confirmed/business_commit=not_asserted`只表达已读到TD样本，不表示PG告警事务已提交。`commit_unknown`后只允许先查持久inbox终态再处理，不因Redis/WS成功重放告警。

广播全局64条、每连接8条/独立writer，最多128连接，每载荷16KiB，写预算1秒。队满或超长使旧epoch失效并断开，慢连接满或超时也断开；重连取新票据，沿用前端connected触发的只读刷新恢复，不保证WS事件必达或外部通知恰好一次。

### 专用夹具启停（仓库根目录、WSL）

复用已解包的S01 PG16/Redis7二进制，S02另建数据根与loopback端口15440/16390。脚本只允许专用`/home/.../tr-s02-fixture`；测试仅允许固定`tr_s02_test`和派生启动测试库，不指向业务库。

```bash
export TR_S02_FIXTURE_ROOT=/home/user23103/tr-s02-fixture
export TR_S02_DEPS_ROOT=/home/user23103/tr-s01-fixture/deps/root
bash stages/02-backend/scripts/tr-s02-fixture.sh start
# 完成本轮验证后停止；保留数据与失败证据供下一阶段复核
bash stages/02-backend/scripts/tr-s02-fixture.sh stop
```

### 同候选必要验证（PowerShell，阶段二目录）

先创建新的批次目录，以下示例名称仅作示意，不覆盖已归档批次。每次修订另存输出；缺依赖测试失败，不能用SKIP代偿。测试会重建固定专用测试库的public schema，以及`tr_s02_startup_test`，所有数据均为本地合成夹具。

```powershell
$trEvidence='../../artifacts/telemetry-reliability/TR-S02-local'
New-Item -ItemType Directory -Force $trEvidence | Out-Null
$env:GOCACHE=(Join-Path (Resolve-Path $trEvidence).Path 'go-cache')
go build -o "$trEvidence/api.exe" ./cmd/api
$env:TR_S02_API_BIN=(Resolve-Path "$trEvidence/api.exe").Path
$env:TR_S02_TEST_DSN='postgres://tr_s02@127.0.0.1:15440/tr_s02_test?sslmode=disable'
$env:NATURAL_TEST_POSTGRES_DSN=$env:TR_S02_TEST_DSN
go test -tags tr_s02 ./internal/api -run '^TestTRS02(Isolated|StartupEntry|GETBoundary)$' -v -count=1 -timeout 90s
go test ./internal/api ./internal/service ./internal/energy ./internal/telemetry -run 'TestOriginAllowed|TestTelemetry|TestEnergy|TestOverview|TestMatches|TestFleetSlotWindow|TestSimulation|TestArchive|TestAlarm|TestRule|TestBusinessGrant|TestBusinessPolicy|TestPrediction|TestParse|TestIntegrate' -count=1
go vet ./internal/api ./internal/service ./internal/energy ./internal/store ./cmd/api
```

T01—T08使用真实PG/Redis、实际事务锁/触发器取消、实际COMMIT且丢回执的驱动夹具、真实WebSocket握手/专用慢写连接、真实API Run与本地MQTT协议对端。TD、MQTT对端及慢写网络为协议/故障夹具，不外推完整服务、容量或生产送达。StartupEntry经实际API二进制验证空库与重复`-migrate-only`。

前端源码无改动；重连与只读刷新复用`src/lib/realtime.ts`及各页面connected监听。若核查该恢复路径，在阶段四目录用既有`npm test -- src/lib/realtime.test.ts`运行单文件即可，无需重跑全前端或模型矩阵。严格笔记检查、图谱生成与`git diff --check`从仓库根目录按既有治理入口执行，证据归本轮独立批次。

## TR-S03 公平调度、退避与受控补偿

契约及八项交接见[TR-S03交接](../../notes/01-项目调研规划与开发建设记录/专项修复-遥测链路可靠性/02-分阶段实施/03-S03公平调度与可重试机制/04-TR-S03阶段交接.md)，当前状态只认[唯一状态页](../../notes/00-当前系统状态.md)。015由实际API迁移入口整体事务应用，保持014前滚限制；不支持旧worker混跑。游标只是调度元数据，inbox仍为唯一队列权威。每页最多500个设备队头，按inbox ID跨run有序；游标持久记录调度访问，退避/熔断跳过不计业务成功。

> S04新增资源阀、两通道和网关接收协议见本页末节；S03测量不作为S04容量基线。

### 专用依赖与必要矩阵

从仓库根在WSL执行下列命令，仅本轮独立loopback端口；start拒绝覆盖已运行实例，stop核对Redis PID。先确认根目录和依赖根存在，不使用生产连接。脚本须保留LF换行。

```bash
export TR_S03_FIXTURE_ROOT=/home/user23103/tr-s03-fixture
export TR_S03_DEPS_ROOT=/home/user23103/tr-s01-fixture/deps/root
bash stages/02-backend/scripts/tr-s03-fixture.sh start
# 验证结束只停止上述专用实例，保留PG数据和本轮证据
bash stages/02-backend/scripts/tr-s03-fixture.sh stop
```

从阶段二目录在PowerShell运行。测试仅允许精确的S03 DSN，缺依赖直接失败；每个T01—T08用例以实际API空库/重复迁移准入，并在下个用例重置public前复制合成数据到`retained_testtrs03...` schema。T02的服务间隔取真实时钟；长宽限使用明确的合成策略时间，不冒充长时间容量观察。

```powershell
$trEvidence='../../artifacts/telemetry-reliability/TR-S03-local'
New-Item -ItemType Directory -Force $trEvidence | Out-Null
$env:GOCACHE=(Join-Path (Resolve-Path $trEvidence).Path 'go-cache')
go build -o "$trEvidence/api.exe" ./cmd/api
go build -o "$trEvidence/tr-compensate.exe" ./cmd/tr-compensate
$env:TR_S03_API_BIN=(Resolve-Path "$trEvidence/api.exe").Path
$env:TR_S03_COMP_BIN=(Resolve-Path "$trEvidence/tr-compensate.exe").Path
$env:TR_S03_TEST_DSN='postgres://tr_s03@127.0.0.1:15441/tr_s03_test?sslmode=disable'
go test -tags 'tr_s02 tr_s03' ./internal/api -run 'TestTRS03|TestTRS02Isolated|TestTRS02GETBoundary' -v -count=1 -timeout 240s
go test ./internal/api ./internal/service ./internal/telemetry -run 'TestTelemetry|TestMatchesAtThreshold|TestEnergyReport(Route|Window|Scope|Seven|Distinguishes|NoDevices|NoAggregated)|TestParse' -v -count=1
go vet ./internal/api ./internal/service ./internal/store ./internal/model ./cmd/api ./cmd/tr-compensate
```

### 补偿清单与单条入口

补偿前逐条登记：候选/隔离环境、inbox ID及run/device/seq、持久状态/TD来源、首次失败/次数/稳定原因、原始行保留位置、依赖恢复证据、前驱及后继终态、操作者/原因/请求幂等键、预期业务及outbox数量。未知提交先锁读持久结果，success不重排；已有终结后继的历史行拒绝回队。补偿仅单ID，无范围扫描、自动任务或HTTP入口。

以下入口默认只检查持久状态。`-isolated-replay`须另行显式传入，服务同时强制核对专用DB名/地址/端口；仅用于合成夹具演练，不是生产积压或历史告警重放工具。不启动通知派发器。相同request-id必须对应相同ID、actor及reason，原状态/首次失败/次数保留在审计表与原行。

```powershell
$env:TR_COMPENSATION_DSN=$env:TR_S03_TEST_DSN
& "$trEvidence/tr-compensate.exe" -inbox-id <明确ID> -request-id <固定请求键> -actor <操作者> -reason <复核原因>
# 仅专用隔离库、完成上述清单后，显式增加 -isolated-replay 演练一次
```

不通过清队列、缩小预期集合或改写received_at/成功时间治理积压。显式停用/静默沿S01有actor/reason/起止的silence窗口；只改变预期输入口径，仍处理已经到达的正常行。生产容量、积压恢复、原子最新值与统一时钟/历史告警策略及发布门禁留S04按当轮授权执行。

## TR-S04 容量恢复、实时快照与历史业务

当前状态见[唯一状态页](../../notes/00-当前系统状态.md)。契约、实际测试与八项交接见[TR-S04交接](../../notes/01-项目调研规划与开发建设记录/专项修复-遥测链路可靠性/02-分阶段实施/04-S04容量恢复与实时通道/04-TR-S04阶段交接.md)。本节只维护命令与操作协议，容量数据只见正式交付页。

### 两通道与资源阀

- 网关：128条未确认易失缓冲；最多32条或20ms合并一次SQLite FULL事务，成功提交才ACK；TD按原50条持久批次、跨设备最多4组并发，部分失败保留整批重试。取消等待接收/写入协程退出，SQLite文件及MQTT持久会话保留。
- API：接收4槽/128条未确认缓冲，数据库单语句持维护SHARE与设备锁，到提交后才ACK。历史8槽/8条派发缓冲，页最多500设备队头；实时8槽/8条缓冲，页最多128设备/每设备最多3候选；PG连接上限28/空闲28。两个游标都只是检查点，原始inbox始终是队列权威。
- `telemetry_scheduler.history_enabled`可暂停历史，`history_parallelism`限定1—8；降低并行度在下一轮生效，维护排空仍以维护单例UPDATE提交为准，不能把调小阀门当成全备栅栏。
- 实时来源为已持久接收且精确匹配device/ts/seq的TD确认，`telemetry_snapshots`只保存派生来源引用。其提交后，另在维护SHARE及同设备锁下重读来源并写Redis；不是跨库原子事务。历史Redis故障不再回滚告警/outbox业务，未知PG提交仍先锁读inbox结果。
- Redis用20位十进制inbox ID字符串做Lua原子比较，run/seq不参与大小；旧采样时间不推进水位。数据库恢复后在锁下以实际持久来源校正残存缓存，包括缓存ID比恢复库高的情况；Redis运行期重启由同一路径分页恢复。派生键TTL为10分钟；未变值不会靠写入刷新年龄，过期后按原持久来源补回，已删除设备的孤立键有固定寿命。

### 时钟、读者及历史空档

UTC毫秒为采样格式，数据库`clock_timestamp()`为接收权威；入库48小时容差保持。实时候选相对原接收时间过去15秒/未来60秒；展示仍要求接收和采样各15秒，未来最多60秒。慢30秒/停滞/旧样本新接收为unknown；快30秒处于兼容容差；超过60秒为unknown；合法旧接收为offline，均不推断硬件故障。

summary、overview、health及watchdog复用同一时间谓词。summary/overview新增`telemetry_unknown`，未知不再并入offline；旧数值/字段保留，旧消费者须将未知与离线分开。latest保留`snapshot_source=td_confirmed/business_commit=not_asserted`与原接收时间，不宣称告警已完成。前端和07/08/09的现有只读工具继续接收JSON，忽略新增字段不会扩大写权限；故障诊断应读取unknown/partial及时间，不能以总数减online解释硬件故障。

历史/乱序样本整次抑制告警评估（含恢复），不补发历史通知；`alarm_evaluation=historical_suppressed`持久保留。`GET /api/v1/devices/:id/telemetry/progress?before_id=...`以64条有界页显示业务状态、TD确认、抑制空档及隔离原因，不返回原始payload，下一页用`next_before_id`；GET只读。历史告警记录本身不伪造回填。

电量仍只积分success及兼容legacy终结行；实时TD确认的pending与quarantine不积分，来源不确定仍provisional。流式重建按最后inbox ID合并同时间样本，超过15秒不积分；JSON报表新增`covered_ms/gap_count/max_gap_ms/coverage_known/coverage_complete`。旧complete保留“有效日聚合和来源”含义，全天连续覆盖要另外检查coverage_complete；旧日值不猜测补齐覆盖。报表保留独立dirty_pending缺口；dirty尚未重建时summary/overview为provisional，不用旧verified聚合冒充最新完整。CSV的source_uncertain/sample_gap继续显式传递缺口。

### 016迁移及安全停止

016通过实际API的`-migrate-only`入口按014/015后整体事务前滚，新增来源引用、告警评估水位、阀门与电量覆盖列；不清原始行、不猜测重分类legacy、不改既有kWh。不允许新旧worker混跑。S04容量依赖API和gateway两份实现，不能只更换API后外推结果。

旧API在新schema上可能写success却缺少新审计，并使用无顺序SET，不能用切旧镜像回退。可验证的保守退路是维护冻结并停止API/gateway写者，保留schema与原始数据，再由同schema候选恢复；不承诺旧版本可继续业务。需要可用版本回退时，生产发布前另构建并验证同schema兼容镜像，或按统一全备恢复至独立隔离环境核对后另获授权，不能在线降schema。

全备/恢复继续执行统一停止写者协议：维护UPDATE先排空API历史/实时写入，网关必须按原全备协议停止并等待其MQTT接收、SQLite/TD写协程退出；单独关闭历史阀不冻结网关。恢复保持维护开启，分别核PG来源、TD数据和派生缓存，之后走ready/健康及观察门禁。

### 专用隔离复验（仓库根PowerShell）

只对`tr-s04-*`四个专用容器及回环15442/16392/16042/11884操作。首次全新环境使用start；若已有本轮专用容器，先inventory核身份并保存回执，不能覆盖或停止同名但不归本轮的资源。

```powershell
$trEvidence=Join-Path (Get-Location) 'artifacts/telemetry-reliability/TR-S04-local'
./stages/02-backend/scripts/tr-s04-fixture.ps1 -Action start -EvidenceRoot $trEvidence
# 本轮已知专用夹具改用 -Action inventory，保留实际容器ID/镜像与配额回执。
./stages/02-backend/scripts/tr-s04-validate.ps1 -Run final-r1 -EvidenceRoot $trEvidence -OldApiBinary '实际保存的pre-S04隔离API绝对路径.exe'
./stages/02-backend/scripts/tr-s04-fixture.ps1 -Action stop -EvidenceRoot $trEvidence
```

验证器冻结API/gateway/测试二进制及旧兼容探针、源码/迁移/脚本清单，日志头尾核摘要与逐文件实哈，运行编号不可复用。Windows夹具记录组合API/网关进程CPU、RSS/Go堆及协程；真实四依赖性能不由协议故障对端代偿。143/5秒严格覆盖原140业务+3基线清单，286/5秒仅在隔离内存目录新增headroom身份，登记期望集合不排除；每设备4条指标规则，watchdog与电量重建持续竞争。二者均为专用隔离测量，后者是2倍余量验证，不自动注册生产设备或改频率。

夹具库、SQLite、失败/修订证据、旧候选和冻结备份至少保留至2026-11-27或专项复核/交接完成较晚者。stop只按回执核对后的精确容器ID停服，保留卷/网络；不清其他会话、生产或回退资产。本节及隔离通过均不授权生产上传、部署、积压处理或外部通知。

### 生产前滚迁移的离线预算

普通API启动预算保持30秒；`-migration-timeout`只有同时传`-migrate-only`才可设为30秒至15分钟，非离线模式拒绝延长。大表索引工作须由统一发布器在旧API/gateway均停止、候选后全备可核时执行一次性迁移；成功后正常启动只重复已存在的schema。禁止让普通API反复超时建索引、手工SQL代偿或直接切旧镜像。

本轮生产前适配、用户接受的失败冻结停写边界及真实同量级迁移证据见[发布记录53](../../notes/04-系统部署发布备份与变更记录/部署与运维日志/05-发布工具链与文档治理/53-遥测可靠性修复发布与前滚门禁.md)。正式生产命令只在[部署README](../04-frontend/deploy/README.md)维护；隔离验收不授权通用历史积压/告警重放。
