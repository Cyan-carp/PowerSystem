---
tags:
  - 设备扩容
  - 核心MVP
  - 实施记录
  - 源网智联
status: 现行
scope: 跨版本
updated: 2026-10-22
---

# 04 · 核心MVP实施记录

> 本页只记录本轮**实际完成**的核心 MVP：唯一清单、dry-run 计划、模拟器共享客户端与错峰、范围 API 与总览、前端、阶段 8/9 工具和短测试。需求口径见 [[01-项目调研规划与开发建设记录/第三版-设备扩容/01-立项需求与架构/02-设备扩容需求范围与验收口径]]，尚未执行的事项在文末单列。

## 一、本轮结果概览

后续第三步已于 2026-10-20 完成正式台账导入和供数，生产版本 `fleet-release-20261020-r2`；本页“未执行”仍仅指首轮实施，现行结果见 [[04-系统部署发布备份与变更记录/部署与运维日志/26-四场站140台设备扩容上线]]。

本页保留首轮代码实施记录；之后完成的七台最小验证、浏览器与工具回放见 [[05-设备扩容测试与交付]] 第十节，当前任务状态见 [[07-设备扩容任务表与后续路线]]。

本地代码已支持 4 个业务场站 140 台合成逆变器，加原基线共 5 站 143 台。所有改动集中在阶段一模拟器、阶段二后端、阶段四前端与阶段 8/9 智能体工具，未新增服务、端口或持久卷。

- 唯一清单：`stages/01-data-chain/simulator/fleet.json`；
- 清单解析与校验：`stages/01-data-chain/simulator/fleet.py`；
- 只读台账计划：`stages/02-backend/scripts/plan-fleet-import.py`；
- 范围与总览：`stages/02-backend/internal/api/fleet_scope.go`、`overview.go`；
- 前端范围层：`stages/04-frontend/src/lib/scope.ts` 与三个视图。

> 「5 站 143 台」当前表示代码与清单能力，不表示数据库已导入、设备已在线或生产已发布。

## 二、唯一 fleet 清单

### 2.1 结构

清单为 `schema_version=1`、`fleet_id=pv-fleet-140-v1`、`sampling_seconds=5`。四个业务站字段一致：`scope=business`、`kind=pv`、`device_prefix=INV`、`rated_power_kw=100`、`profile=generic-pv-100kw`、`daylight_mode=real`、`group_size=8`；`ST-01` 为 `scope=baseline`，以 `explicit` 列出三台，`daylight_mode=loop`、`daylight_loop_minutes=60`。

### 2.2 展开规则

- 业务设备编码按 `PV-01-INV-01` 生成，名称按「场站名 + 序号 + 号逆变器」，分区每 8 台一组。
- 业务行输出完整台账字段：`device_code/name/dev_type/vendor/is_simulated/reference_vendor/reference_model/station_code/station_name/group_name/rated_power_kw/profile/scope`。
- 基线行只输出身份字段 `device_code/dev_type/station_code/station_name/scope`，不含 `vendor/is_simulated/profile`，确保原三台不会被改写成合成台账行。
- 下游必须按 `scope` 分流，不能假设每一行都有 `profile` 或 `vendor`。

### 2.3 校验边界

严格拒绝未知字段、缺字段、重复场站或设备编码、非法 `scope/kind/daylight_mode`、非正数或非整数的数量与功率、`loop` 缺少合法分钟数、`real` 携带分钟数、业务站带 `explicit`、基线站带业务字段、编码不满足 `[A-Za-z0-9_-]{1,64}`。

> 清单是唯一来源。模拟器、台账计划、后端场站元数据都从这里读取，不维护第二份 SQL VALUES 或前端名称表。

## 三、dry-run 台账计划

`plan-fleet-import.py` 用标准库实现，经 `sys.path` 复用 `fleet.py` 的加载、校验与展开逻辑，不复制 schema，也不引入数据库驱动。

- 默认只保留 `scope=business` 的 140 行，基线三台单列 `excluded_baseline`，身份完全不动；
- 输出含 `mode/read_only/fleet/existing/summary/stations/excluded_baseline/planned_devices/conflicts/existing_only`；
- `--existing` 接受离线设备快照，只比较快照中实际出现的字段（`station_code/name/dev_type/vendor/is_simulated`）；
- 退出码：0 无冲突、2 有冲突、1 输入非法，错误以 JSON 写入 stderr。

边界：快照只比较出现的字段，漏导出的字段差异发现不了；快照漏掉的已存在设备会被计划为 create。工具只生成计划，不校验软删除、唯一约束与真实写入结果。

## 四、模拟器共享客户端与错峰

### 4.1 配置构建

`build_device_configs(fleet, seed)` 生成 143 个冻结配置。业务设备取清单字段，`ST-01` 三台补齐 100 kW、`generic-pv-100kw`、`baseline`、`loop` 运行信息。

设备特征不再依赖 `DEVICES.index()`，改用 `hashlib.sha256` 从 seed、场站编码、设备编码派生；清单增删、排序变化或进程重启都不会重新洗牌设备特征。

同站共享日照、云量与相位，设备只叠加 0.96～1.0 稳定降额、温度/电压偏移与测量噪声；功率上限为额定功率，保持 `P=√3×U×I×0.99/1000`。

### 4.2 回执归属

`MidRegistry` 维护 `mid -> (device_code, seq)`：

- 重复 mid 返回 `False` 且不覆盖原归属；
- `on_publish` 仅在命中时确认对应 `(device, seq)`，断线清理后的旧 mid 不会误删队列；
- `on_disconnect` 只清空 registry，不删 outbox；
- `on_connect` 与 `flush_pending` 按上限补发，默认上限 1000。

`SharedPublisher` 让整个车队复用一个客户端，`client_id` 默认 `simulator-fleet` 且可配置；构造时不连接、不启动 loop，paho 延迟导入。

### 4.3 主循环接入

传入 `--fleet` 时加载清单、构建配置、启动共享客户端；不传时仍走原三台 `DEVICES` 与逐设备 `Publisher`，旧入口保持可用。

每台设备的 `daylight_loop_minutes` 来自配置，`ST-01` 保留 60 分钟白天循环与真实时间故障日程，业务站按真实昼夜生成。业务设备任何时刻不注入故障。

一个 5 秒周期内按设备顺序错开发送，把 143 条消息摊平，降低同一瞬间的发送峰值。

> 错峰只改变发送时点，不改变采样语义与序号；每台设备仍是 5 秒一条。

## 五、范围 API 与总览

### 5.1 清单加载与范围判定

- `FLEET_CONFIG_PATH` 读取清单，默认 `../01-data-chain/simulator/fleet.json`，从 `stages/02-backend` 工作目录解析；
- 加载失败只告警，不阻止旧 API 启动；未加载时 `all` 与 `station_code` 直接过滤仍可用，`business`/`baseline` 与 `/stations` 返回 503；
- `resolveScopeFilter` 统一规则：省略为 `all`，非法值 400，`scope` 与 `station_code` 矛盾 400，需要清单而缺失 503。

### 5.2 接口

- `GET /api/v1/devices`、`GET /api/v1/alarms` 新增 `scope`、`station_code`，旧形状与 `id ASC` 不变；
- 新增 `GET /api/v1/stations`，按清单顺序返回固定字段，`registered` 只统计未删除设备；
- 新增 `GET /api/v1/dashboard/overview`，按范围与可选场站输出聚合。

### 5.3 总览语义

总览只批量读取当前快照、当日电量与活动告警，不逐台拉取全天原始遥测。

- `partial` 在「登记数少于清单期望数」或「有效观测少于登记数」时为真；
- 缺失功率、电量、健康度返回 `null`，不补零；
- 场站行同时给出 `expected/registered/observed`，便于分辨「没导入」与「导入了没供数」；
- 响应带 `computed_at`，说明聚合时刻。

## 六、前端

- 新增 `scope.ts`：默认 `business`，三档标签，`allowsDailyCurve` 仅对 `baseline` 为真，`reconcileStation` 在切换范围时清除不属于新范围的场站。
- 运行总览使用 `/dashboard/overview`，展示期望/登记/已观测、`partial` 提示与场站对比；业务与全平台范围不逐台加载全天曲线，仅基线保留 `ST-01` 三台既有曲线。
- 设备列表、告警中心增加范围与场站筛选，场站选项来自 `/stations`；切换范围回到第一页。
- 请求返回时校验范围与场站条件，避免旧响应覆盖新范围；空值统一显示 `—`。

## 七、阶段 8/9 智能体工具

- 两端新增 `ScopeOnly` 与 `ScopeFilter` 参数基类，`list_devices`、`list_alarms` 继承分页加范围过滤。
- 新增 `list_stations`（仅 `scope`）与 `get_dashboard_overview`（`scope` + `station_code`），注册到 `MODELS` 与路由表，参数原样透传。
- `get_dashboard_summary` 保留原路由与语义，不带新参数。
- 描述中写明分页上限、`total`、只取一页的覆盖限制，以及 `partial`、`expected/registered/observed`、`null` 不等于 0。
- 两端四个业务工具的名称、schema、默认值与路由经比对一致。
- 业务证据识别已同步扩展：`list_stations` 与 `get_dashboard_overview` 会形成结构化业务事实，不再被判为「未查询」。

## 八、短测试

以下均为**短测试**，只覆盖各自声明范围：

- **Python fleet/simulator**：67 项通过，覆盖 143 台展开、分布 48/40/32/20/3、编码唯一、基线身份保持、非法清单拒绝、共享回执归属、错峰与稳定特征。
- **dry-run 计划**：14 项通过，覆盖 140 台、四站顺序、无基线、全 create / 全 same / 字段冲突、退出码。
- **Go fleet/API**：`internal/fleet` 与 `internal/api` 定向测试通过，覆盖范围归一化、非法组合、清单缺失 503、场站行顺序与总览聚合；未连接 PostgreSQL/Redis。
- **前端**：相关 Vitest 12 项通过，`npm run typecheck` 通过；未执行构建与浏览器联调。
- **阶段 8/9**：62 / 63 项通过，另加 1 项业务证据识别用例。

> 通过数量只说明这些用例在其范围内成立，不构成全链路回归、容量结论或生产验收。

## 九、分工与复核

- **Codex** 负责冻结契约、模拟器主循环整合、`/dashboard/overview` 聚合语义、跨模块复核、短测试执行与文档状态收口。
- **Workbuddy** 按任务分片交付清单与校验器、模拟器纯逻辑、共享 MQTT 组件、后端范围接口、前端适配、阶段 8/9 工具与 dry-run 计划。
- 每片交付均由 Codex 复核差异后才计入本轮结果；复核发现的问题（回调临界区、共享客户端生命周期、业务证据白名单、镜像构建输入）已在同一轮修复。
- 共享记忆只用于协作上下文，不作为验收证据；正式结论以源码、命令输出和本页记录为准。

## 十、未执行事项

以下均**未执行**，不得在任何页面写成已完成：

- 未向 PostgreSQL 写入或导入 140 台设备台账，未执行任何真实 SQL；
- 未连接真实 MQTT Broker，未启用 140 台实际供数；
- 未运行 Docker / Compose，未构建或推送镜像；
- 未发布生产、未切换生产指针、未做回退演练；
- 未做容量压测，未取得 200 台 / 2 核 4G 的任何资源数据；
- 未做长时间观察、全链路回归或模型回放；
- 未实现业务车队每日 1～3 台的持久化异常日程；
- 未实现 Redis 分时聚合，业务与全平台范围暂无逐设备曲线；
- 未做预测、电量、网关的有界并发优化；
- 未启用历史原始数据清理与结算保护。

> `FLEET_CONFIG_PATH` 默认值依赖从 `stages/02-backend` 启动；容器与发布环境必须显式装载唯一清单并设置路径，否则会降级为无清单模式。

## 十一、2026-10-21—22 告警解读 v2 与受控演示（已发布）

本批为扩容后的独立批次，设计见[[01-项目调研规划与开发建设记录/第三版-设备扩容/04-告警智能解读专项/09-告警智能解读重构计划与架构对照]]。已落地代码并于r4发布，清理和恢复证据见部署日志30，不覆盖旧扩容历史。

- **独立通道**：`stages/07-agent/agent/interpret_provider.py` 使用官方 `https://api.deepseek.com/chat/completions`、`deepseek-flash`、`thinking={"type":"disabled"}`、`response_format={"type":"json_object"}`、`stream=false`、`max_tokens=4096`，不发送 tools 与 `tool_choice`；独立密钥、独立暂停与独立探针。
- **v2 协议**：`interpret_v2.py` 定义 `facts` 与 `{template, fact_refs}` 结构，占位仅允许 `{{F1}}` 形式并绑定本次事实；模型不算数，Go 渲染。
- **Go 事实层**：`interpretation_v2.go` 归一化事实 ID、单位、时区与派生量，绑定 `evidence_hash`；事实不足时不生成派生事实。
- **调度**：每任务持久 `model_calls ≤ 2`、租约 60 s、15 min 截止、重试 30 s ± 20%；连续 3 次故障熔断 60/120/240/300 s，半开单请求；无备用模型。
- **受控演示**：`simulation.go` 与迁移 `009` 实现 143 台每天 09/11/13/15/17 五条 `simulation_scenario`，各站 1 台、稳定轮转、`(day, slot)` 唯一、10 min `simulation_demo_ended`、missed 跳过；真实设备与监控旁路。
- **前端**：事实与模型分析分层，类型检查通过，2 个文件 6 项定向测试通过。

**状态**：r4于2026-10-21 23:59:36通过完整发布门禁，10-22 00:01:33清空728条旧业务告警及关联记录。固定合成六例真实调用6/6通过；00:09:37最终只读核对通过，详见部署日志30，不外推长期100%。

## 相关笔记

- [[01-项目调研规划与开发建设记录/第三版-设备扩容/00-索引·第三版-设备扩容]]
- [[01-项目调研规划与开发建设记录/第三版-设备扩容/01-立项需求与架构/01-设备扩容立项背景与目标]]
- [[01-项目调研规划与开发建设记录/第三版-设备扩容/01-立项需求与架构/02-设备扩容需求范围与验收口径]]
- [[01-项目调研规划与开发建设记录/第三版-设备扩容/01-立项需求与架构/03-设备扩容技术架构调整]]
- [[05-设备扩容测试与交付]]
- [[07-设备扩容任务表与后续路线]]
