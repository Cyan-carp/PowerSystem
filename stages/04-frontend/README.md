# 源网智联 · 前端（第四阶段）

> **home-dev开发**：依赖已迁到远端；当前连接命令见[远程开发说明](../02-backend/deploy/home-dev/README.md)。本页原本地Compose/preflight仅适用于有意启动本地引擎的模式，不用于home-dev日常连接。前端可直接经隧道连接远端API。


新能源场站云边协同智能运维平台的前端：**浅色控制台**（灰蓝底、白卡片、蓝色主色、环图与面积图）。
Vue 3 + Vite + TypeScript，**不带 UI 组件库与图表库**——`Drawer` / `StatusChip` / `ToastHost` /
`PowerChart` / `DonutGauge` / `Icon` 都是本目录自研。

> 接口按 `stages/02-backend` 的 Gin 路由逐条对齐，本地跑同契约的假后端（合成数据，不连生产、不落盘）。
> 逐条对照见 [API-INVENTORY-20261029.md](./API-INVENTORY-20261029.md)，新旧能力差异与逐轮补齐见
> [PARITY-20261029.md](./PARITY-20261029.md)，视觉决策日志见 [DESIGN.md](./DESIGN.md)。
>
> **当前状态**：工作区候选，**未发布**；生产仍跑上一版前端。发布与指针回写见仓库 `notes/00-当前系统状态.md`。

## 快速开始

```bash
# 终端 1：假后端，监听 127.0.0.1:8080（刻意不入库，见 .gitignore 的 stages/04-frontend/dev/）
node dev/mock-server.mjs

# 终端 2：前端，监听 127.0.0.1:5173（已固定 5173，strictPort）
npm run dev
```

打开 <http://127.0.0.1:5173/>，用任意账号密码登录（假后端不校验账号，只签本地 token）。
`/api` 与 `/ws` 由 Vite 代理到 `VITE_API_TARGET`（默认 `http://127.0.0.1:8080`）。

```bash
npm run typecheck   # vue-tsc --noEmit
npm run build       # 先 typecheck 再 vite build，产物在 dist/
npm run test        # vitest（少量单测）
node --check dev/mock-server.mjs   # 改过假后端务必跑
```

需要 Node 20 以上；本机系统 Node 22.11 低于 Vite 8 要求时，用托管 Node 22.22 跑构建。

## 页面

| 路径 | 内容 |
| --- | --- |
| `/login` | 登录：账号密码 → token 与用户信息落本地，失败留在原页提示 |
| `/` | 场站总览：紧凑统计带、功率曲线（可选时段）、场站对比、设备状态环图、告警构成 |
| `/alarms` | 告警中心：状态/级别筛选、分页、详情抽屉、确认与归档、勾选批量归档；`?status=&level=&id=` 双向同步 |
| `/devices` | 设备台账：健康分排序、状态筛选、分页，点行进详情 |
| `/devices/:id` | 设备详情：档案、健康评分、最新遥测、历史曲线（自选指标与时段、导出 CSV）、该设备告警、失效风险 |
| `/health` | 健康排行：平均分与档位、排名、场站健康、评分依据与采样时间；`?state=scored\|unknown\|offline\|fault` |
| `/predictions` | 故障预测：概率条 + 阈值竖线、风险筛选、概率分布、特征贡献 |
| `/reports` | 统计报表：可用率 / 告警及时性双页签 + 设备级可用率表，CSV 走后端 `format=csv`；`?tab=timeliness` |
| `/schedules` | 定期任务：我的任务、方案预演（只读回算命中差异）、最近审查；新建与修改走审批，批准后才生效 |
| `/assistant` | 智能助手：运行事件流、动作审批弹窗、产物下载、来源阅读、任务记录、历史会话 |

页面标题只出现在顶栏一处，页内不再重复大标题。顶栏搜索把关键词带到 `/devices?q=`；
右侧「告警解读」打开抽屉（结论 / 关键事实 / 建议动作 / 局限 / 证据快照，模型不可用时退回模板并标明）。

## 接口契约

前端只调 `src/lib/api.ts` 里的方法，每个方法对应真后端 `internal/api/server.go` 的一条路由。

- **有信封**：绝大多数接口返回 `{ code, message, data }`，`code !== 0` 即失败（`request()`）。
- **裸 JSON**：`/agent/runs`、`/agent/schedules`、`/agent/rule-preview`、产物元数据由网关原样透出、
  不套信封（`rawRequest()`）。两类混用会直接白屏，改接口前先看 handler 是 `ok(c, …)` 还是 `c.Data(…)`。
- **鉴权**：除 `POST /auth/login`、`GET /ping` 外都要 `Authorization: Bearer`；401 只有一个出口
  （`lib/auth.ts` 清会话 → 通知 → 带 `redirect` 回登录页）。

| 端点 | 关键约束 |
| --- | --- |
| `POST /auth/login` | 免鉴权；失败回 401，前端不跳转、原地提示 |
| `GET /devices/:id/telemetry` | `metric` 必须合法；`start/end` RFC3339；窗口 ≤ 24h；点数 ≤ 5000 |
| `GET /devices/:id/telemetry/export` | 窗口 ≤ 7 天；CSV 带 UTF-8 BOM、固定 11 列表头 |
| `GET /alarms` | 只返回 `archived = FALSE`；支持 `status/level/origin/device_id/start/end/station_code` |
| `POST /alarms/:id/ack`、`/archive`、`POST /alarm-archive` | 409 = 已被别人处置、404 = 已归档、503 = 维护窗口 |
| `GET /agent/interpretations` | 异步任务：`task_status` 有 `pending/running/completed/degraded`，`attention=true` 只回未读且仍值得看的 |
| `POST /ws-ticket` → `GET /ws/realtime?ticket=` | ticket 48 位 hex、60 秒内一次性消费 |
| 任意分页接口 | `page_size` 上限 100，超出按 100 处理 |

## 目录

```text
├── index.html
├── vite.config.ts        # 端口 5173，代理 /api 与 /ws 到 VITE_API_TARGET（默认 8080）
├── dev/mock-server.mjs   # 假后端（与真后端同契约；不入库）
├── src/
│   ├── main.ts           # 路由 10 条 + 401 唯一出口
│   ├── App.vue           # 图标轨 + 顶栏 + 告警解读/活动告警/账号弹层
│   ├── styles/
│   │   ├── tokens.css    # 色板、文字层级、圆角、壳层尺寸
│   │   └── shell.css     # 壳层与通用类（card/btn/chip/kv/page-head…）
│   ├── lib/              # api/auth/store/format/data + 运行、产物、审批、任务、解读
│   ├── components/       # 20 个：Drawer/StatusChip/ToastHost/Icon/PowerChart/DonutGauge/RunAnswer…
│   └── views/            # 10 个页面
├── PARITY-20261029.md    # 新旧能力对照与逐轮补齐
├── API-INVENTORY-20261029.md  # 前端调用接口逐条对照（51 条）
└── DESIGN.md             # 视觉方向决策日志
```

## 假后端

`dev/mock-server.mjs` 按真后端路由实现，不是「只要界面能跑」的桩：

1. **设备与告警**：143 台逆变器 / 5 个场站 / 26 条告警，种子固定（`mulberry32(20261026)`），每次启动一致。
2. **异步时序**：运行、方案预演、告警解读都按真实时序推进（`accepted → progress → tool_* → artifact → completed`），
   `task_status` 有 `pending` 起点——否则界面的「生成中」等状态在本地永远验不到。
3. **写接口事务**：`POST/PUT /agent/schedules` 只登记请求并回 202，**批准时**才落任务表（对齐真后端 `on_accept`）。
4. **时间口径**：凡「按小时算日照」都用北京时间（`beijingHour()`），不依赖进程时区。

`/ws/realtime` 每 5 秒推一帧遥测；`ws-ticket` 一次性消费，重连要重新取票。

## 数据与边界

- 全部为**合成数据**，不作为真实场站运维依据。
- 告警接口只返回 `device_id`，设备编码与名称由前端按台账补齐（`src/lib/data.ts` 的 `deviceIndex()`，
  逐页 100 条翻到底并缓存 60 秒）。
- 时间统一按 `Asia/Shanghai` 显示；设备详情与定期任务的时间输入按 `+08:00` 解析，不跟随浏览器时区。
- 「方案预演」只对历史采样回算阈值命中差异，**不改线上规则、不构成收益或漏报率**；「告警解读」的结论来自后端模型任务的
  `result`，不可用时退回按指标与级别的模板并明确标注。
- 已知待办：移动端精细适配、会话长内容可读性、`prediction_risk` / `platform_monitor` 两类解读尚无落点。
