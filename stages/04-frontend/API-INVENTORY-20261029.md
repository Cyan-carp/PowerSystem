# 前端接口清单与后端对照（2026-10-29）

> 对象：覆盖后的 `stages/04-frontend/src`（独立前端，无 Element Plus）。
> 后端基准：`stages/02-backend/internal/api/server.go` 第 90–200 行注册表（唯一事实源），
> 参数基准为各 handler 里的 `c.Query(...)` 与 `parseAlarmStatistics`。
> 假后端基准：`dev/mock-server.mjs` 路由表。

## 一、结论

| 问题 | 结论 |
| --- | --- |
| 前端调用的接口总数 | **51 条**（50 条 HTTP + 1 条 WebSocket）<br>2026-10-29 晚补 `POST /auth/login`、`GET /ping`；同日深夜补 `POST /alarm-archive`；第五轮补运行态 7 条（run 快照/事件流/取消/动作决定、产物元数据与下载、知识章节）；第七轮补 `POST /agent/rule-preview`；第八轮补解读 3 条（列表/详情/已读） |
| 有对应后端路由的 | **51 条，全覆盖** |
| **前端有、后端没有的** | **0 条** |
| 后端有、前端没用的 | **17 条**（见第四节，即功能缺口；助手运行链路、方案预演、告警解读都已被页面用掉） |
| 相对旧前端真正新增的 | **2 条接口 + 2 处新参数**（见第三节；`/auth/login`、`/ping`、run 事件流、产物下载、知识章节旧前端也有，不算新增） |

也就是说：不存在「前端对着空气调接口」的情况。缺的是反方向——后端已经能做的事，界面还没有入口。

## 二、前端 → 后端逐条对照（51 条）

| # | 方法 | 前端路径 | 后端实现 | 假后端 |
| --- | --- | --- | --- | --- |
| 1 | GET | `/dashboard/summary` | `dashboard()` | ✅ |
| 2 | GET | `/dashboard/overview?scope&station_code` | `dashboardOverview()` | ✅ |
| 3 | GET | `/stations?scope` | `listStations()` | ✅ |
| 4 | GET | `/devices?page&page_size&group_name&scope&station_code&keyword` | `listDevices()` | ✅ |
| 5 | GET | `/devices/:id` | `getDevice()` | ✅ |
| 6 | GET | `/devices/:id/telemetry/latest` | `latest()` | ✅ |
| 7 | GET | `/devices/:id/telemetry?metric&start&end` | `history()` | ✅ |
| 8 | GET | `/devices/:id/telemetry/progress?before_id` | `telemetryProgress()` | ❌ 未实现（前端也未调用） |
| 9 | GET | `/devices/:id/telemetry/export?start&end` | `exportTelemetry()` | ✅ |
| 10 | GET | `/alarms?page&page_size&status&level&origin&scope&station_code&device_id&start&end` | `listAlarms()` | ✅ |
| 11 | GET | `/alarms/:id` | `getAlarm()` | ✅ |
| 12 | POST | `/alarms/:id/ack` | `ackAlarm()` | ✅ 本轮接界面 |
| 13 | POST | `/alarms/:id/archive` | `archiveAlarm()` | ✅ 本轮接界面 |
| 13b | POST | `/alarm-archive` `{ids:[…]}` → `{archived,failed,updated_at}` | `archiveAlarms()`（批量，≤200 条） | ✅ 本轮补 |
| 14 | GET | `/alarms/statistics?group_by=level\|status` | `alarmStatistics()` | ✅ 本轮补 |
| 15 | GET | `/health/devices` | `healthDevices()` | ✅ |
| 16 | GET | `/health/stations` | `healthStations()` | ✅ |
| 17 | GET | `/predictions?page&page_size` | `listPredictions()` | ✅ |
| 18 | GET | `/devices/:id/prediction` | `getPrediction()` | ✅ |
| 19 | GET | `/reports/availability?start&end&device_id&station_code` | `availability()` | ✅ |
| 20 | GET | `/reports/alarm-timeliness?…` | `alarmTimeliness()` | ✅ |
| 21 | GET | `/reports/{availability\|alarm-timeliness}?…&format=csv` | `availabilityCSV()` / `timelinessCSV()`（`report_export.go`） | ✅ 本轮补 |
| 22 | GET | `/agent/schedules` | `agentSchedulesProxy()` | ✅ |
| 23 | GET | `/agent/schedules/:id/history?offset` | 同上 | ✅ |
| 24 | POST | `/agent/schedules/:id/{pause\|resume\|cancel}` | 同上 | ✅ |
| 25 | POST | `/agent/schedules` | 同上 | ✅ |
| 26 | PUT | `/agent/schedules/:id` | 同上 | ✅ |
| 27 | GET | `/agent/sessions?offset` | `agentRAGProxy()` | ✅ |
| 28 | GET | `/agent/sessions/:id` | 同上 | ✅ |
| 29 | DELETE | `/agent/sessions/:id` | 同上 | ✅ |
| 30 | POST | `/agent/runs`（带 `Idempotency-Key`，202 回 receipt） | `agentRunsProxy()` → `agentRunSubmit()` | ✅ 本批补 |
| 30b | GET | `/agent/runs/:id` | `agentRunSnapshot()` | ✅ 本批补 |
| 30c | GET | `/agent/runs/:id/events?after_seq=` | `agentRunEvents()`（SSE） | ✅ 本批补 |
| 30d | POST | `/agent/runs/:id/cancel` | `agentRunCancel()` | ✅ 本批补 |
| 30e | POST | `/agent/runs/:id/actions/:action_id/decision` | `agentActionDecision()` | ✅ 本批补 |
| 30f | GET | `/agent/artifacts/:id`、`/agent/artifacts/:id/download` | `agentArtifactsProxy()` | ✅ 本批补 |
| 30g | GET | `/agent/knowledge/:id` | `agentKnowledge()` | ✅ 本批接界面 |
| 30h | POST | `/agent/rule-preview`（带 `Idempotency-Key`，202 回 receipt） | `agentSchedulesProxy()` 转 `/internal/agent/rule-preview` | ✅ 本轮补真预演 |
| 30i | GET | `/agent/interpretations?category&alarm_id&updated_after&attention&page&page_size` | `listInterpretations()`（`ok()` 信封） | ✅ 第八轮接界面 |
| 30j | GET | `/agent/interpretations/:id` | `getInterpretation()` | ✅ 第八轮接界面 |
| 30k | POST | `/agent/interpretations/:id/read` | `readInterpretation()`（写当前用户已读） | ✅ 第八轮接界面 |
| 31 | POST | `/ws-ticket` | `ticket()` | ✅ |
| 32 | GET | `/agent/tasks?session_id&offset&status&limit` | `opsProxy()` | ✅ |
| 33 | GET | `/agent/tasks/:id` | 同上 | ✅ |
| 34 | POST | `/agent/tasks/:id/cancel` | 同上 | ✅ |
| 35 | DELETE | `/agent/tasks/:id` | 同上（软删除） | ✅ |
| 36 | POST | `/agent/tasks/:id/approvals/:approval_id/decision` | 同上 | ✅ |
| 37 | WS | `/ws/realtime?ticket=` | `websocket()` | ✅ |
| 38 | POST | `/auth/login` | `login()`（**免鉴权**） | ✅ |
| 39 | GET | `/ping` | 匿名探针 handler（**免鉴权**） | ✅ |

## 三、相对旧前端真正新增的部分

旧前端（`backups/frontend-pre-cover-20261029/src`）本身已经调了 49 个接口方法，
覆盖 run 事件流、产物下载、AI 解读、登录、批量归档等。所以「新增接口」其实只有两条，其余是新参数：

| 类型 | 内容 | 对应的新界面 |
| --- | --- | --- |
| 新接口 | `GET /alarms/statistics?group_by=` | 告警页四格（总数 / 紧急 / 重要 / 已恢复）改为**全量聚合**；列表只取 100 条，不能用来当总数 |
| 新接口 | `GET /devices/:id/telemetry/progress` | 已声明、**没有页面调用**（预留回补缺口用，当前是死代码） |
| 新参数 | `GET /devices?keyword=` | 顶栏设备搜索（后端只搜 `name` 与 `device_code`，**不搜场站名**） |
| 新参数 | `GET /alarms?device_id=&start=&end=` | 设备详情页「此设备的告警」、告警页时间窗 |

## 四、后端有、前端没用的接口（17 条 = 缺口清单）

按影响排序，前三组是「不补就是功能缺失」，后面是管理面与遗留。

| 组 | 接口 | 缺的界面 |
| --- | --- | --- |
| **身份** | `POST /auth/register` | 登录页已接 `/auth/login`（2026-10-29 晚）；注册按产品决策不做界面，账号由管理员创建 |
| **会话核验** | `POST /agent/sessions/:id/verify`、`GET /agent/rag/sources/:id` | 结论核验入口缺失；来源已能按章节阅读（本批接 `knowledge/:id`） |
| **报表** | `GET /reports/energy` | 发电量报表页没有 |
| **设备台账** | `POST /devices`、`PUT /devices/:id`、`DELETE /devices/:id`、`PATCH /devices/:id/remark` | 台账只读；新建/改/删/备注都没有入口 |
| **告警规则** | `GET/POST /alarm-rules`、`GET/PUT/DELETE /alarm-rules/:id` | 规则管理页整体缺失 |
| **定期任务详情** | `GET /agent/schedules/:id` | 只用了列表与历史，单条详情没用 |
| **模型运维** | `GET /agent/model-status`、`POST /agent/model-probe` | 管理员模型状态面板缺失 |
| **遗留** | `POST /agent/chat` | 已被 `/agent/runs` 取代，V4 开关关闭时会 503，不应再有人调 |

另有 `/internal/*`（`schedule-read`、`ma actor/snapshot/execute/receipts`）是服务间接口，前端不涉及。

## 五、参数级不一致（会静默降级，不报错）

| # | 位置 | 现象 | 影响 |
| --- | --- | --- | --- |
| 1 | `GET /alarms/statistics` | 后端 `parseAlarmStatistics` **只接受** `group_by/start/end/device_id/status/origin`，多一个未知参数直接 400；且不支持 `scope` / `station_code` / `level` | 四格统计**无法跟随页面场站筛选**，切了场站仍是全场口径 |
| 2 | `GET /devices?keyword=` | 后端只 `strpos(name) / strpos(device_code)` | 搜索框提示写着「搜索设备编码、名称或场站」，**搜场站名搜不到** |
| 3 | `GET /alarms?station_code=` | 只接受单个 `station_code`（正则 `telemetry.Code`），不接受逗号拼接 | 多场站同时筛选做不了，前端 `stationCodes` 字段只能塞一个 |
| 4 | 假后端 `GET /reports/*` | 忽略 `station_code` / `device_id` | 本地演示切场站/设备，报表数字不变（真后端是生效的） |
| 5 | `GET /alarms` vs `GET /alarms/statistics` | 列表固定 `archived = FALSE`（`alarms.go:193`），统计**不带** archived 条件（`aggregateAlarms`） | **归档一条未处理告警后，列表少一条但顶部「未处理」不减少**。这是后端口径（统计按事件事实计数、归档只改标记），前端不擅自过滤，避免与生产分叉 |

## 六、假后端缺口

| 接口 | 状态 |
| --- | --- |
| `GET /alarms/statistics` | **本轮已补**：按 `group_by` 聚合、`total_events` 为全量（不再是 100 条列表长度） |
| `GET /reports/*?format=csv` | **本轮已补**：原来是 2 行占位，现在按后端列名输出 143 设备行 + 5 场站汇总行 / 4 条时效行 |
| `GET /devices/:id/telemetry/progress` | 仍未实现。前端也没有页面调用，暂时不影响 |
| `/alarm-rules`、`/reports/energy` | 未实现。前端也未调用，属同一批缺口 |
| `POST /alarms/:id/ack` | **本轮已补**：只允许 `unhandled` 且未恢复，重复确认回 409（同真后端） |
| `POST /alarms/:id/archive` | **本轮已补**：置 `archived=true`，不存在回 404 |
| `POST /alarm-archive` | **本轮已补**：`{ids}` 去重、≤200 条，返回 `{archived,failed,updated_at}`，不存在的编号进 `failed` 但不阻断其余 |
| `GET /alarms` | **本轮已补归档过滤**：带 `archived = FALSE`，与真后端一致（此前假后端会把归档的也列出来） |
| WS `/ws/realtime` | **本轮已补告警广播**：写接口成功后推 `alarm_acked` / `alarm_archived`，前端据此自动刷新 |
| `POST /agent/runs` | **本批改形状**：原来直接回快照（200），现在回 202 receipt + 事件日志，与真后端一致；否则流式正文与审批弹窗都验不到 |
| `GET /agent/runs/:id/events` | **本批实现**：支持 `after_seq` 游标、积压补发 + 实时推送、终态后关闭；seq 连续 |
| `POST /agent/runs/:id/actions/:id/decision` | **本批补**：校验 `plan_hash` / `approval_id` / `expires_at`，重复决定回 409（同真后端） |
| `GET /agent/artifacts/:id(/download)` | **本批补**：元数据裸 JSON + CSV 字节流，`sha256` 与 `size` 可被前端校验 |
| `GET /agent/sessions/:id` | **本批补 turns**：原来恒回空数组，历史轮次与来源无从验证 |
| `POST /agent/rule-preview` | **本轮改真预演**：原来直接造一条审批运行（`rule.preview` 不需要批准）。现在按 08 `Preview` 校准入参（时区窗口 ≤24h 且在过去、整数阈值、1–86400 秒间隔），取 `/devices/:id/telemetry` 的历史点，按 `rule_preview.evaluate` 同口径算覆盖与命中，产出 `rule-preview.json` 产物后完成 |
| `GET /agent/schedules` | **本轮改口径**：`runs` 只回配置审查与预演请求（真后端 join `assistant_schedule_requests`，最新 30 条，最新在前）；`list` 按 `created_at` 倒序，新批准的任务排最前 |
| `POST/PUT /agent/schedules` | **本轮改时序**：原来提交即写任务表（`paused / 等待批准`）。真后端是**批准后**才写 `assistant_schedules`，所以改成批准时落库并算 `next_run_at`、发授权；拒绝时改回原配置说明 |

## 七、待办（建议顺序）

1. ~~**登录 + token**（`POST /auth/login`）~~ **已完成（2026-10-29 晚）**：`lib/auth.ts` + 登录页 + 路由守卫 + 顶栏账号菜单；假后端 `/auth/login` 与 `/ping` 也补齐，除这两条外一律要求 `Authorization: Bearer`。
2. ~~**告警处置**（ack / archive / 批量归档）~~ **已完成（2026-10-29 深夜）**：抽屉内确认/归档 + 列表勾选批量归档，409 冲突与 503 维护有中文提示，写后自动对齐。
3. ~~**助手 run 事件流 + 动作审批**~~ **已完成（2026-10-29 第五轮）**：迁入 v4 run 客户端（SSE、seq 连续性、断线退避、绑定校验）+ 审批弹窗自动弹出 + 产物下载校验 + 来源阅读。
4. **报表导出已接回后端 CSV**（第四轮完成），剩下 `reports/energy` 页。
5. ~~**定期任务「方案预演」**（`POST /agent/rule-preview` + 最近审查）~~ **已完成（2026-10-29 第七轮）**：预演表单 + 结果块 + 审查列表 + 审查运行就地审批。
6. ~~**AI 告警解读**（`GET /agent/interpretations` 三条）~~ **已完成（2026-10-29 第八轮）**：解读面板改读后端异步任务结果（结论/事实/建议/局限/证据），模板降级为兜底；顶栏红点改用 `attention=true` 的真实未读数。
7. 结论核验（`POST /agent/sessions/:id/verify` + `GET /agent/rag/sources/:id`）：需先确定核验项勾选与设备选择的产品形态。
