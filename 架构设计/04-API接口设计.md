---
tags:
  - 架构设计
  - API
  - RESTful
  - 源网智联
---

# 04 · API 接口设计

> 回到 [[00-索引·架构设计]]
> 对应手册任务 12（API 接口设计）

定义前后端交互的全部 RESTful 接口，是 [[01-系统架构图]] 中「REST /api/v1」连线的展开，也是后端开发（手册任务 15）的施工图。

## 一、RESTful 设计规范

| 规范 | 约定 |
| --- | --- |
| 资源命名 | 名词复数，如 `/devices`、`/alarms` |
| 动作表达 | GET 查询 / POST 新建 / PUT 修改 / DELETE 删除 |
| 版本号 | URL 带版本前缀 `/api/v1` |
| 统一响应 | 三字段 `code` / `message` / `data` |
| 分页 | 列表接口必带 `page`、`page_size`，返回 `total` |
| 错误码 | 4xx 客户端错、5xx 服务端错 |

## 二、全生命周期规划接口清单（阶段二实现子集见第七节）

> 本节是设计清单，不等于当前代码的已实现接口。`/telemetry/export`、`/reports/availability` 仍为 FR-11、FR-12 的规划项；实际路由以 `stages/02-backend/internal/api/server.go` 为准。二次开发时先核对当前接口契约，再补迁移和测试。

### 1. 认证模块（FR-08）

| 方法 | 路径 | 说明 | 鉴权 |
| --- | --- | --- | --- |
| POST | `/api/v1/auth/register` | 注册（密码 bcrypt 加密存储） | 否 |
| POST | `/api/v1/auth/login` | 登录，签发 JWT | 否 |

### 2. 设备模块（FR-05）

| 方法 | 路径 | 说明 | 鉴权 |
| --- | --- | --- | --- |
| GET | `/api/v1/devices` | 设备列表（分页 + 分组筛选） | JWT |
| POST | `/api/v1/devices` | 新建设备 | JWT |
| GET | `/api/v1/devices/{id}` | 设备详情 | JWT |
| PUT | `/api/v1/devices/{id}` | 修改设备 | JWT |
| DELETE | `/api/v1/devices/{id}` | 删除设备 | JWT |

### 3. 遥测与数据模块（FR-06、FR-11、FR-12）

| 方法 | 路径 | 说明 | 鉴权 |
| --- | --- | --- | --- |
| GET | `/api/v1/devices/{id}/telemetry` | 历史曲线（`metric`+`start`+`end`） | JWT |
| GET | `/api/v1/devices/{id}/telemetry/latest` | 设备最新值（读 Redis） | JWT |
| GET | `/api/v1/devices/{id}/telemetry/export` | 导出原始遥测（CSV） | JWT |
| GET | `/api/v1/reports/availability` | 设备可用率 / 处理时效报表 | JWT |

### 4. 告警模块（FR-02、FR-03、FR-04）

| 方法 | 路径 | 说明 | 鉴权 |
| --- | --- | --- | --- |
| GET | `/api/v1/alarms` | 告警列表（按 `status`/`level` 筛选） | JWT |
| GET | `/api/v1/alarms/{id}` | 告警详情（含关联设备曲线） | JWT |
| POST | `/api/v1/alarms/{id}/ack` | 确认告警（记录操作人与时间） | JWT |

### 5. AI 预测模块（FR-09）

| 方法 | 路径 | 说明 | 鉴权 |
| --- | --- | --- | --- |
| GET | `/api/v1/devices/{id}/prediction` | 设备最新故障预测 + 特征依据 | JWT |
| POST | `/predict` | FastAPI 预测服务（后端内部调用） | 内网 |

### 6. 实时推送（FR-02、FR-07）

| 协议 | 路径 | 说明 |
| --- | --- | --- |
| WebSocket | `/ws/realtime` | 实时推送新遥测值 + 新告警 |

## 三、统一响应结构

成功（`code = 0`）：

```json
{
  "code": 0,
  "message": "success",
  "data": { "list": [], "page": 1, "page_size": 20, "total": 128 }
}
```

失败（`code ≠ 0`）：

```json
{ "code": 40101, "message": "token 过期或无效" }
```

## 四、错误码分级（示例）

| code | 含义 | HTTP |
| --- | --- | --- |
| 0 | 成功 | 200 |
| 40001 | 参数错误 / 校验失败 | 400 |
| 40101 | 未登录 / token 失效 | 401 |
| 40301 | 无权限（越权访问） | 403 |
| 40401 | 资源不存在 | 404 |
| 50000 | 服务器内部错误 | 500 |

## 五、接口 ↔ 功能需求映射

| 功能需求 | 对应接口 |
| --- | --- |
| FR-01 设备接入 | （网关侧 MQTT，非 HTTP 接口） |
| FR-02/03/04 告警 | GET/POST `/alarms` + WS `/ws/realtime` |
| FR-05 设备台账 | `/devices` CRUD |
| FR-06 历史曲线 | GET `/devices/{id}/telemetry` |
| FR-07 总览大屏 | WS `/ws/realtime` + `/dashboard/summary` + `/devices` + `/alarms` |
| FR-08 登录鉴权 | `/auth/register`、`/auth/login` |
| FR-09 故障预测 | GET `/devices/{id}/prediction` |
| FR-11 数据导出 | GET `/devices/{id}/telemetry/export` |
| FR-12 报表统计 | GET `/reports/availability` |
| FR-13 断网补传 | （网关侧，非 HTTP 接口） |

## 六、验收自检（任务 12 标准）

- [x] 随便挑一个功能能指出调用接口、参数、返回结构（例：「查看设备历史曲线」→ `GET /api/v1/devices/{id}/telemetry?metric=temperature&start=...&end=...`，返回 `code/message/data` 中的曲线点数组）；
- [x] 统一响应三字段、列表分页、错误码分级均已定义；
- [x] 与 [[03-数据库设计]] 字段一一对应（`device_code`↔`devices` 表、`level/status`↔`alarm_records` 表）。

> 架构设计模块到此完成。四件套（架构图 → 数据流 → 数据库 → API）全部交付，进入开发实战前可做一次「设计评审」（见 [[00-索引·架构设计]] 四件套自洽核对）。

## 七、阶段二已实现契约

| 类别 | 接口 | 约定 |
| --- | --- | --- |
| 公开 | `GET /api/v1/ping`；`POST /api/v1/auth/register`、`/auth/login` | 登录返回 8 小时 JWT；注册仅创建 `operator` |
| 设备 | `/api/v1/devices`、`/api/v1/devices/{id}` 的 GET/POST/PUT/DELETE | 列表按 `page/page_size/group_name`；删除为软删除，编号/场站编号不可修改 |
| 遥测 | `GET /api/v1/devices/{id}/telemetry`、`.../latest` | 指标白名单；历史时间为 RFC3339、≤24 小时、≤5000 点；最新值读 Redis |
| 规则 | `/api/v1/alarm-rules`、`/api/v1/alarm-rules/{id}` 的 GET/POST/PUT/DELETE | 每设备每指标一条规则；阈值、比较符和级别受校验 |
| 告警 | `GET /api/v1/alarms`、`/alarms/{id}`；`POST /alarms/{id}/ack` | 列表按状态/级别筛选；确认后持续越限不重报，恢复后可再报 |
| 总览 | `GET /api/v1/dashboard/summary` | 保留在线/离线/故障数、当前功率、活动告警与运行正常占比；阶段四 P0 增加 `today_energy_kwh`、`retained_energy_kwh`、`energy_start_ms`、`energy_updated_at` 和 `health_score_percent`，空数据为 `null`；远端对账与公网接口已核对 |
| 实时 | `POST /api/v1/ws-ticket`；`GET /ws/realtime?ticket=...` | JWT 换一次性 60 秒票据；WebSocket 推送遥测及告警事件 |

所有阶段二业务 REST 接口均需 Bearer JWT，成功与失败都返回 `code/message/data`；列表默认第 1 页、每页 20 条，上限 100。详情告警不内嵌曲线，可通过关联设备的遥测接口查询。上文列出的预测、导出和报表接口仍是后续阶段规划，M2 不以其上线为前提。运行和自测命令见 [[stages/02-backend/README.md]]。


## 八、阶段三预测接口落地

阶段三在原设计的 `GET /api/v1/devices/{id}/prediction` 实现 JWT 查询，成功时继续使用 `code/message/data`。返回最近 30 分钟窗口末尾时间、未来一小时故障概率、阈值、风险等级、特征依据、模型版本、来源及 `stale`。无结果为 404；旧结果超过 15 分钟仍可查，但标记过期。Go 后端每五分钟调用本机 FastAPI `POST /predict`，请求包含设备数据库 ID、窗口结束时间和近期电压、电流、温度、功率原始点。数据库按设备、窗口结束时间、模型版本去重；高风险走独立 `ai_failure_risk` 告警指标。详见 [[开发阶段3-AI预测模块/03-预测服务与告警]]。

## 九、阶段四第 13、14 周前端增量契约

| 方法 | 路径 | 约定 |
| --- | --- | --- |
| GET | `/api/v1/predictions?page=&page_size=` | JWT；每台未删除设备一行，返回设备 ID、编号、名称、场站、分组及最新预测概要 |

列表延续 `code/message/data` 和 `{list,page,page_size,total}`。有结果行包含 `window_end_ms`、`probability`、`threshold`、`risk_level`、`model_version`、`source`、`stale`；无结果行的预测字段为 `null`，`stale=false`。有效结果按概率降序，过期结果和无结果设备随后。详情及 `top_factors` 仍由 `GET /api/v1/devices/{id}/prediction` 提供。过期界限是窗口结束时间距当前超过 15 分钟，前端须标示“合成数据训练模型”。

本地 Vite 跨端口开发时，Go 后端可通过 `WS_ALLOWED_ORIGINS` 配置精确 WebSocket Origin 白名单；不配置时维持同源校验。前端先用 JWT 调用 `POST /api/v1/ws-ticket`，再用单次 ticket 连接 `/ws/realtime`。详见 [[开发阶段4-前端、上线与沉淀/02-第14周·实时告警与预测]]。

## 相关笔记

- 本模块索引：[[00-索引·架构设计]]
