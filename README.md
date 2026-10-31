<div align="center">

# 源网智联 · 新能源设备智能运维平台

> **home-dev开发环境**：本机Docker Desktop可关闭；远端中间件与原生API的连接、两种开发方式及旧脚本限制见[后端远程开发说明](stages/02-backend/deploy/home-dev/README.md)。以下本地启动命令仅适用于有意在本机启动Docker的独立环境。


新能源场站的设备监测、告警解读与受控运维一体化平台

An integrated operations platform for renewable-energy assets — monitoring, alarm interpretation and guarded automation.

<p><i>合成数据演示 · 生产以受限只读档运行 · 全部发布证据可回溯</i></p>

[English](#english) | 中文说明

![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux-495057?style=flat-square)
![Stack](https://img.shields.io/badge/stack-Vue%203%20%C2%B7%20Go%20%C2%B7%20Python-1c7ed6?style=flat-square)
![Devices](https://img.shields.io/badge/fleet-143%20devices-2f9e44?style=flat-square)
[![Release](https://img.shields.io/github/v/release/Cyan-carp/PowerSystem?style=flat-square&color=e8590c)](https://github.com/Cyan-carp/PowerSystem/releases)
[![notes-check](https://img.shields.io/github/actions/workflow/status/Cyan-carp/PowerSystem/notes-check.yml?style=flat-square&label=notes-check)](https://github.com/Cyan-carp/PowerSystem/actions/workflows/notes-check.yml)

</div>

## 这是什么

一个从零写起的新能源设备运维平台：把光伏逆变器的遥测数据接进来，落成时序库，做告警、健康评分和故障预测，再让大模型把"这条告警到底怎么了"讲清楚，并在人批准后执行受控操作。

平台可通过浏览器访问，也提供 Windows 桌面客户端。桌面端连接同一网站与账号，在现有业务页面上提供托盘驻留、系统通知和原生文件保存。

它不是单点脚本，而是一条完整链路：**采集 → 入库 → 判警 → 解读 → 行动 → 复盘**。各阶段各自有独立的源码目录与验收里程碑，笔记库与代码物理隔离，发布、回退、恢复都有记录可查。

> **当前状态**：生产指针、阶段状态、开关与已知限制统一见 [当前系统状态](notes/00-当前系统状态.md)，本 README 不复述。

> **数据说明**：仓库内全部场站、设备与告警均为合成数据；公网演示同样使用合成数据与独立演示账号。任何结论都不构成真实场站运维依据。

## 主要功能

| 能力 | 说明 |
| --- | --- |
| 数据接入 | 设备模拟器 → MQTT QoS 1 → EMQX → Go 网关 → TDengine，5 秒一个采样点，断链自动补传 |
| 运行总览 | 143 台设备的状态分布、实时功率曲线、场站对比，秒级 WebSocket 推送 |
| 告警闭环 | 阈值与自然规则判警、去重合并、恢复跟踪、值班筛选；AI 解读失败时保留事件事实 |
| 健康评分 | 按可用率、告警密度与遥测质量打分，缺数据显式标"未知"而不是记 0 分 |
| 故障预测 | XGBoost 模型输出未来 60 分钟故障概率与风险等级，页面区分"过期"与"无结果" |
| 智能助手 | 自然语言问设备状态、告警与报表；答案分层为"直接结论 / 已核验事实 / 未核验解释" |
| 定期任务 | 日报、巡检等周期任务，带预演与交付位置；写动作须人工批准 |
| 受控运维 | 重启采集、限功率等动作经独立安全模型审查后才执行，全程留审计 |
| 桌面访问 | Windows 客户端复用网站业务，支持托盘显隐、系统通知、原生另存为和连接恢复；完全退出后须重新登录 |
| 可追溯发布 | 按风险选择验证与备份，保留批次、镜像来源及回退/前滚依据；操作入口统一 |

## 技术架构

```
场站设备 / 模拟器
      │  MQTT QoS 1
      ▼
    EMQX
      │
      ▼
  Go 网关 / 后端
      ├──► TDengine      时序库（遥测与历史）
      ├──► PostgreSQL    业务、告警、定期任务
      ├──► Redis         最新值缓存
      └──► WebSocket ──► Vue 3 控制台（浏览器 / Windows 桌面端）
                             └──► 智能体（问答 / 运维 / 安全审查）
```

| 层 | 选型 |
| --- | --- |
| 前端 | Vue 3 · Vite · TypeScript · Element Plus · ECharts |
| 桌面端 | Electron · TypeScript · NSIS（Windows x64 安装程序） |
| 后端 | Go 1.24 · Gin · GORM · WebSocket |
| 智能体 | Python 3.12 · 大模型 API · 检索增强（RAG）· FastAPI（模型服务） |
| 数据 | TDengine · PostgreSQL · Redis · EMQX |
| 运维 | Docker Compose · Nginx · Prometheus + Alertmanager · FRP · 飞书机器人 |

## 快速开始

### 直接使用

浏览器访问 [平台网站](https://www.cyancarp.cn/)，使用已有账号登录。Windows 用户也可安装团队交付包中的 `PowerSystem-Desktop-0.2.0-x64-Setup.exe`，安装后使用同一账号访问平台，无需在本机部署后端或安装开发工具。

本机整理好的团队包位于项目根目录 `PowerSystem-Desktop-0.2.0-Team.zip`，内含安装程序、摘要与 `TEAM-GUIDE.md`；安装、升级、必要回退及反馈办法按包内指南操作。客户端使用说明见 [平台使用手册](notes/03-平台使用运维与二次开发手册/平台使用手册.md)。

### 本地开发前置条件

| 依赖 | 版本 |
| --- | --- |
| Windows PowerShell | 5.1 或 7（脚本为 PowerShell 编写） |
| Docker | 本地模式需Desktop引擎running；home-dev模式需Docker CLI、SSH及远端引擎 |
| Go | 1.24+ |
| Python | 3.12 |
| Node.js | 20.19+ 或 22.12+ |

### 本地四步启动

```powershell
# 1. 克隆并准备本机配置（.env 已被 Git 忽略，密码只留本机）
git clone https://github.com/Cyan-carp/PowerSystem.git
cd PowerSystem
Copy-Item .env.example .env       # 至少填 TDENGINE_ROOT_PASSWORD

# 2. 起数据链路：EMQX + TDengine + 模拟器 + 网关（预检约 2 分钟）
cd stages\01-data-chain
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\preflight-stage1.ps1
cd ..\..

# 3. 起后端：PostgreSQL / Redis / API / WebSocket
cd stages\02-backend
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\preflight-stage2.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-stage2.ps1 -SkipPreflight
cd ..\..

# 4. 起前端
cd stages\04-frontend
npm ci
npm run dev                       # http://127.0.0.1:5173
```

Vite 把 `/api` 与 `/ws` 代理到 `127.0.0.1:8080`。本地跨端口调试需在 `.env` 设 `WS_ALLOWED_ORIGINS=http://127.0.0.1:5173`（精确 Origin）。

### 常用命令

以下命令均在仓库根目录执行。

| 目的 | 命令 |
| --- | --- |
| 前端类型检查 | `npm --prefix stages/04-frontend run typecheck` |
| 前端单元测试 | `npm --prefix stages/04-frontend test` |
| 前端生产构建 | `npm --prefix stages/04-frontend run build` |
| 停止后端进程（保留数据） | `.\stages\02-backend\scripts\stop-stage2.ps1` |
| 完整跑一次一小时 M1 验收 | `.\stages\01-data-chain\scripts\run-stage1-test.ps1` |
| 笔记治理校验 | `python tools\note-governance\check_notes.py` |

## 疑难解答

| 现象 | 处理 |
| --- | --- |
| 前端能打开，接口全 404 | 后端没起，或不在 8080。先 `curl http://127.0.0.1:8080/api/v1/summary`，再查 Vite 代理目标 |
| `npm run dev` 报 Node 版本不支持 | Vite 要求 Node 20.19+ / 22.12+，18 与 22.11 都不行 |
| 预检脚本在中文路径下失败 | 少数 Python 虚拟环境在含中文的路径下创建失败。把仓库放到纯 ASCII 路径，或改用已装好依赖的解释器 |
| 设备显示"离线"或"健康未知"，是不是坏了 | 不是。离线按最后上报时间判定，健康未知表示样本不足或遥测故障，页面用 `?state=offline` 或 `?state=unknown` 可单独筛，二者都不等于设备故障 |
| 登录后刷新还在，关掉标签页就掉线 | JWT 存在 `sessionStorage`，按标签页生命周期，这是有意的，避免共用电脑留登录态 |
| 关闭桌面窗口后程序还在 | 点击 × 会隐藏到托盘；需要完全关闭时，从托盘菜单选择“退出” |
| 桌面端没有收到系统通知 | 先核客户端通知设置、Windows 免打扰和网络状态；通知不保证必达，告警事实以站内记录为准 |
| 能不能 `docker compose down -v` 清一下 | 不要。`-v` 会连阶段一、阶段二的 TDengine / PostgreSQL / Redis 数据卷一起删。停止用 `stop-stage2.ps1` |
| 报表日期对不上 | 报表按北京时间自然日聚合，`end_date` 不含当日；跨时区看数据前先把时区对齐 |
| 端口被占但 `netstat` 查不到 | Windows 上 Hyper-V / WSL 会预留动态端口段，落在区间内的端口 bind 直接失败。`netsh interface ipv4 show excludedportrange protocol=tcp` 可确认 |

## 开发文档

| 想做什么 | 从哪里进 |
| --- | --- |
| 先把平台用起来 | [平台使用手册](notes/03-平台使用运维与二次开发手册/平台使用手册.md) |
| 改代码、加功能 | [平台运维与二次开发手册](notes/03-平台使用运维与二次开发手册/平台运维与二次开发手册.md) |
| 弄清目录为什么这样分 | [项目目录导航](notes/00-项目目录导航.md) |
| 分阶段修复遥测离线与队列问题 | [修复专题](notes/01-项目调研规划与开发建设记录/专项修复-遥测链路可靠性/00-索引·遥测链路可靠性修复.md) · [新对话接续](notes/01-项目调研规划与开发建设记录/专项修复-遥测链路可靠性/03-跨对话交接/00-TR当前接续入口.md) · [S01交接](notes/01-项目调研规划与开发建设记录/专项修复-遥测链路可靠性/02-分阶段实施/01-S01监控与失败状态/04-TR-S01阶段交接.md) · [S02交接](notes/01-项目调研规划与开发建设记录/专项修复-遥测链路可靠性/02-分阶段实施/02-S02维护门禁与超时/04-TR-S02阶段交接.md) · [S03交接](notes/01-项目调研规划与开发建设记录/专项修复-遥测链路可靠性/02-分阶段实施/03-S03公平调度与可重试机制/04-TR-S03阶段交接.md) · [S04交接](notes/01-项目调研规划与开发建设记录/专项修复-遥测链路可靠性/02-分阶段实施/04-S04容量恢复与实时通道/04-TR-S04阶段交接.md) |
| 看当前发布、镜像与开关 | [当前系统状态](notes/00-当前系统状态.md) |
| 按阶段读源码说明（一至五） | [阶段一](stages/01-data-chain/README.md) · [阶段二](stages/02-backend/README.md) · [阶段三](stages/03-ai-prediction/README.md) · [阶段四](stages/04-frontend/README.md) · [阶段五](stages/05-feishu-notification/README.md) |
| 按阶段读源码说明（六至十） | [阶段六](stages/06-security-recovery/README.md) · [阶段七](stages/07-agent/README.md) · [阶段八](stages/08-agent-rag/README.md) · [阶段九](stages/09-ops-agent/README.md) · [阶段十](stages/10-security-agent/README.md) |
| 开发 Windows 桌面客户端（阶段十一） | [源码、构建与测试](stages/11-desktop-client/README.md) |
| 查桌面版设计、验收与交接 | [第五版总索引](notes/01-项目调研规划与开发建设记录/05-第五版-Windows桌面客户端/00-索引·第五版-Windows桌面客户端.md) · [团队交付](notes/01-项目调研规划与开发建设记录/05-第五版-Windows桌面客户端/02-分阶段实施/03-S03联合验收与团队交付/02-V5-S03测试与交付.md) · [正式交接](notes/01-项目调研规划与开发建设记录/05-第五版-Windows桌面客户端/03-跨对话交接/V5-S03-阶段交接.md) |
| 部署到自己的服务器 | [部署说明](stages/04-frontend/deploy/README.md) · [上传服务器规范](notes/02-工程规范边界与验收治理/项目工程规范/05-上传服务器规范.md) |
| 高效率开发、测试与发布 | [分级流程与本次优化记录](notes/04-系统部署发布备份与变更记录/部署与运维日志/05-发布工具链与文档治理/54-开发测试发布与备份流程提效.md) |
| 查历史发布与恢复记录 | [部署与运维日志索引](notes/04-系统部署发布备份与变更记录/部署与运维日志/00-索引·部署与运维日志.md) |
| 核对里程碑与待办 | [项目验收与收尾清单](notes/02-工程规范边界与验收治理/项目验收与收尾清单/00-索引·项目验收与收尾清单.md) |

各阶段的验收结论、测试证据与遗留项写在该阶段笔记的「测试与交付」页，本 README 不再汇总里程碑表。

> 笔记库是独立 Obsidian Vault。**只打开 `PowerSystem/notes`**，不要打开仓库根目录，否则全库双链与图谱都会失效。

## 数据与使用边界

仓库公开的是源码、配置模板与笔记。`artifacts/` 下的模型、测试账本与截图，以及服务器私有 `.env`、数据卷和签名文件，都不随 Git 提供；首次部署须自行生成模型并准备私有配置。演示环境与本文档中的所有数字都来自合成数据。

桌面团队包是独立交付附件，不随 Git 源码克隆提供；连接正式 HTTPS 网站，不具备离线业务能力。当前为内部未签名安装包，无自动更新，系统通知受 Windows 设置、网络与休眠影响。具体已验范围、证据缺口和版本收尾结论统一见 [当前系统状态](notes/00-当前系统状态.md) 及相应阶段交接。

## 目录

- [这是什么](#这是什么)
- [主要功能](#主要功能)
- [技术架构](#技术架构)
- [快速开始](#快速开始)
- [疑难解答](#疑难解答)
- [开发文档](#开发文档)
- [数据与使用边界](#数据与使用边界)
- [English](#english)

```
PowerSystem/
├── stages/         阶段源码与各自 README
│   ├── 01-data-chain/      数据链路（模拟器 → MQTT → 网关 → TDengine）
│   ├── 02-backend/         后端主体（API、告警、WebSocket）
│   ├── 03-ai-prediction/   功率预测
│   ├── 04-frontend/        Vue 控制台与部署脚本
│   ├── 05-feishu-notification/  飞书告警
│   ├── 06-security-recovery/    公网安全与备份恢复
│   ├── 07-agent/           告警解读与值班问答
│   ├── 08-agent-rag/       检索增强问答
│   ├── 09-ops-agent/       运维动作智能体
│   ├── 10-security-agent/  安全审查与门禁
│   └── 11-desktop-client/  Windows 桌面客户端
├── notes/          Obsidian 笔记库（设计、验收、部署日志、手册）
├── deploy/         边缘服务器部署配置
├── tools/          笔记治理与知识图谱校验
├── shared-memory/  跨会话过程记录（不作验收依据）
└── backups/        备份登记表（包体不入库）
```

## English

**PowerSystem** is an operations platform for renewable-energy assets, built end to end: telemetry ingestion, time-series storage, alarm lifecycle, health scoring, failure prediction, LLM-based alarm interpretation, and guarded automation.

Devices publish over MQTT into EMQX; a Go gateway writes TDengine and fans out to PostgreSQL, Redis and WebSocket clients; a Vue 3 console renders it; Python agents answer questions, run scheduled jobs and review risky actions with a separate security model.

The same platform is available in a browser and through an Electron-based Windows desktop client, which adds tray controls, system notifications and native file saving.

**Highlights**

- End-to-end chain: ingest → store → detect → explain → act → review
- 143 synthetic devices across 5 stations, 5-second sampling, replay on reconnect
- Layered answers: verified facts separated from unverified explanation
- Every action that writes goes through human approval and an audit trail
- Windows desktop access with tray controls, native saving and connection recovery
- Releases are traceable: batch id, backup, isolated restore and observation window

**Quick start**

To use the platform, open the [website](https://www.cyancarp.cn/) or install the Windows team package and sign in with an existing account. Desktop installation does not require the development tools below; installation and upgrade instructions are included in `TEAM-GUIDE.md`.

For local development, follow the [four-step setup](#本地四步启动) above to start the data chain, backend and frontend in order.

Requires Windows PowerShell 5.1+, Docker CLI (Desktop engine locally or via home-dev), Go 1.24+, Python 3.12 and Node 20.19+/22.12+. The console runs at `http://127.0.0.1:5173` and proxies `/api` and `/ws` to `127.0.0.1:8080`.

**Notes**

All fleet data in this repository is synthetic and must not be used as a basis for real plant operations. Runtime status and enabled capabilities are maintained only in the linked current system status page. Source, configuration templates and notes are public; models, credentials, data volumes and evidence bundles are not. Start reading from the [current system status](notes/00-当前系统状态.md) and the [project directory guide](notes/00-项目目录导航.md).

Desktop installers are distributed separately from the source repository. The internal Windows package is unsigned, has no automatic updates and requires the online platform; system notification delivery is best effort. Desktop source and development instructions are in [stage eleven](stages/11-desktop-client/README.md).

遥测修复的发布、冻结退路与恢复验证边界见[遥测可靠性发布与前滚门禁](notes/04-系统部署发布备份与变更记录/部署与运维日志/05-发布工具链与文档治理/53-遥测可靠性修复发布与前滚门禁.md)；当前运行状态统一见[当前系统状态](notes/00-当前系统状态.md)。
