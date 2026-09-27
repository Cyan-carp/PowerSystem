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

## 二、接口清单

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
| FR-07 总览大屏 | WS `/ws/realtime` + `/devices` + `/alarms` |
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
