<div align="center">

# 源网智联 · 新能源设备智能运维平台

> **本地开发入口**：请先阅读 [home-dev 开发手册](stages/02-backend/deploy/home-dev/README.md)，按手册选择远端 API 或本机后端；原有的本地 Docker 预检脚本已不适用。


新能源场站的设备监测、告警解读与受控运维一体化平台

An integrated operations platform for renewable-energy assets — monitoring, alarm interpretation and guarded automation.

<p><i>合成数据演示 · 生产能力按权限开放 · 发布证据可回溯</i></p>

[English](#english) | 中文说明

![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux-495057?style=flat-square)
![Stack](https://img.shields.io/badge/stack-Vue%203%20%C2%B7%20Go%20%C2%B7%20Python-1c7ed6?style=flat-square)
![Devices](https://img.shields.io/badge/fleet-143%20devices-2f9e44?style=flat-square)
[![Release](https://img.shields.io/github/v/release/Cyan-carp/PowerSystem?style=flat-square&color=e8590c)](https://github.com/Cyan-carp/PowerSystem/releases)
[![notes-check](https://img.shields.io/github/actions/workflow/status/Cyan-carp/PowerSystem/notes-check.yml?style=flat-square&label=notes-check)](https://github.com/Cyan-carp/PowerSystem/actions/workflows/notes-check.yml)

</div>

## 系统概述

面向新能源场站的一体化设备运维平台：接入光伏逆变器遥测数据并写入时序数据库，提供告警判定、健康评分与故障预测；由大模型解释告警成因，受控操作须经人工批准后执行。

平台支持浏览器访问，并提供 Windows 桌面客户端与 Android 客户端。桌面端复用同一站点与账号体系，并补充托盘驻留、系统通知与原生文件保存能力；Android 客户端采用 WebView 外壳，界面与网页同源。

平台以 **采集 → 入库 → 判警 → 解读 → 行动 → 复盘** 为主线，而非若干独立脚本的集合。各阶段拥有独立源码目录与验收里程碑；笔记库与代码物理隔离，发布、回退与恢复过程均可追溯。

> **当前状态**：生产指针、阶段状态、运行开关与已知限制统一见 [当前系统状态](notes/00-当前系统状态.md)，本文不再复述。

> **数据说明**：仓库内全部场站、设备与告警均为合成数据；公网演示环境同样使用合成数据与独立演示账号。本文档中的任何结论均不构成真实场站运维依据。

## 主要功能

| 能力 | 说明 |
| --- | --- |
| 数据接入 | 设备模拟器 → MQTT QoS 1 → EMQX → Go 网关 → TDengine；5 秒采样一次，链路中断后自动补传 |
| 链路状态 | 区分设备上报中断与平台处理延迟，查看待处理积压、7 天外滞留与历史电量覆盖 |
| 运行总览 | 全平台 143 台设备的状态分布、实时功率曲线与场站对比，经 WebSocket 秒级推送 |
| 告警闭环 | 支持阈值与自然规则判警、去重合并、恢复跟踪与值班筛选；AI 解读失败时保留事件事实 |
| 现场记录 | 在设备与告警详情追加症状、处置措施与结果，分页留存 |
| 健康评分 | 按可用率、告警密度与遥测质量综合评分；数据不足时显式标记为「未知」，不计为 0 分 |
| 故障预测 | XGBoost 模型输出未来 60 分钟故障概率与风险等级，故障前一小时起进入前兆爬升；页面区分「已过期」与「无结果」 |
| 模型质量 | 独立页面展示评估指标、混淆矩阵、增益曲线、漂移观察与版本台账；人工复核反馈单独统计，数据不足或分析失败均有明确提示 |
| 换版闭环 | 页面一键重训 → 候选入台账 → 试用 → 确认或回退；推理服务热加载，切换期间不中断服务 |
| AI 辅助解读 | 解读模型漂移原因、对比试用版本；结论仅作建议，不触发训练或换版动作 |
| 智能助手 | 以自然语言查询设备状态、告警与报表，可引用站内设备说明书、巡检表与日志附件；答案分层为「直接结论 / 已核验事实 / 未核验解释」 |
| 定期任务 | 日报、巡检等周期任务，支持预演与交付位置设定；写操作须人工批准 |
| 受控运维 | 具备独立审查与人工批准流程；生产开关与实际开放范围以当前系统状态为准 |
| 账号与权限 | 管理员可在站内注册账号并分配「管理员 / 常规运维人员」权限；「原始 user」管理员可停用其他管理员账号 |
| 桌面访问 | Windows 客户端复用网站业务，支持托盘显隐、系统通知、原生另存为与连接恢复，并按屏幕物理像素宽度自适应内容密度；完全退出后须重新登录 |
| 移动访问 | 手机浏览器响应式布局，另提供 Android 客户端（WebView 外壳）；界面与网页同源，网页更新后无需重新发版 |
| 模型通道 | 按功能展示 5 项模型配置与可查询余额；09/10 兼容网关未接入余额查询并显式标注，详见[专题](notes/01-项目调研规划与开发建设记录/展示业务功能补充/06-模型通道与额度面板/01-模型通道与额度面板.md) |
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
      └──► WebSocket ──► Vue 3 控制台（浏览器 / 桌面端 / 移动端）
                             └──► 智能体（问答 / 运维 / 安全审查）
```

| 层 | 选型 |
| --- | --- |
| 前端 | Vue 3 · Vite · TypeScript · 自研界面与图表组件 |
| 桌面端 | Electron · TypeScript · NSIS（Windows x64 安装程序） |
| 移动端 | Android · WebView 外壳（复用 Web 界面，无原生业务代码） |
| 后端 | Go 1.24 · Gin · GORM · WebSocket |
| 智能体 | Python 3.12 · 大模型 API · 检索增强（RAG）· FastAPI（模型服务） |
| 数据 | TDengine · PostgreSQL · Redis · EMQX |
| 运维 | Docker Compose · Nginx · Prometheus + Alertmanager · FRP · 飞书机器人 |

## 快速开始

### 直接使用

浏览器访问 [平台网站](https://www.cyancarp.cn/)，使用已有账号登录。Windows 用户可安装团队交付包中的 `PowerSystem-Desktop-0.2.0-x64-Setup.exe`，安装后以同一账号访问平台，无需在本机部署后端或安装开发工具；Android 用户可按[阶段十二说明](stages/12-android-client/README.md)自行构建并安装 APK，首次启动时确认服务器地址即可。

本机团队包位于项目根目录 `PowerSystem-Desktop-0.2.0-Team.zip`，内含安装程序、校验摘要与 `TEAM-GUIDE.md`；安装、升级与回退按包内指南执行。客户端使用说明见 [平台使用手册](notes/03-平台使用运维与二次开发手册/平台使用手册.md)。

### 本地开发前置条件

| 依赖 | 版本 |
| --- | --- |
| Windows PowerShell | 5.1 或 7（开发脚本以 PowerShell 编写） |
| Docker | 本地模式需 Docker Desktop 引擎运行；home-dev 模式需 Docker CLI、SSH 与远端引擎 |
| Go | 1.24+ |
| Python | 3.12 |
| Node.js | 20.19+ 或 22.12+ |

### 本地开发

```powershell
# 1. 克隆仓库
git clone https://github.com/Cyan-carp/PowerSystem.git
cd PowerSystem
# 2. 按 home-dev 手册准备 API 与中间件连接，然后启动前端
cd stages\04-frontend
npm ci
npm run dev                       # http://127.0.0.1:5173
```

后端环境、配置模板、隧道以及 `/api`、`/ws` 代理方式以 [home-dev 开发手册](stages/02-backend/deploy/home-dev/README.md) 与 [前端开发说明](stages/04-frontend/README.md) 为准；本机独立 Docker 环境按对应阶段手册准备。

### 常用命令

以下命令均在仓库根目录执行。

| 用途 | 命令 |
| --- | --- |
| 前端类型检查 | `npm --prefix stages/04-frontend run typecheck` |
| 前端单元测试 | `npm --prefix stages/04-frontend test` |
| 前端生产构建 | `npm --prefix stages/04-frontend run build` |
| 开发环境连接与停止 | 见 `stages/02-backend/deploy/home-dev/README.md` |
| 笔记治理校验 | `python tools\note-governance\check_notes.py` |

## 疑难解答

| 现象 | 处理 |
| --- | --- |
| 前端可打开，接口全部返回 404 | 按 home-dev 手册核对当前 API 地址与 Vite 代理目标 |
| `npm run dev` 提示 Node 版本不支持 | Vite 要求 Node.js 20.19+ 或 22.12+，18.x 与 22.11 均不满足 |
| 本机开发脚本在中文路径下失败 | 少数 Python 虚拟环境无法在含中文的路径下创建；按对应阶段手册改用纯 ASCII 路径或已装好依赖的解释器 |
| 设备显示「离线」或「健康未知」，是否为设备故障 | 否。离线按最后上报时间判定；健康未知表示样本不足或遥测异常。可用 `?state=offline` 或 `?state=unknown` 单独筛选，二者均不等同于设备故障 |
| 登录后刷新仍在，关闭标签页即退出 | JWT 存放于 `sessionStorage`，随标签页生命周期失效；此为有意设计，避免共用设备遗留登录态 |
| 关闭桌面窗口后程序仍在运行 | 点击 × 仅隐藏至托盘；如需完全退出，请从托盘菜单选择「退出」 |
| 桌面端未收到系统通知 | 先核对客户端通知设置、Windows 免打扰与网络状态；通知不保证必达，告警事实以站内记录为准 |
| 能否用 `docker compose down -v` 清理环境 | 不建议。`-v` 会删除数据库数据卷；开发环境的停止方式见 home-dev 手册 |
| 报表日期不一致 | 报表按北京时间自然日聚合，`end_date` 不含当日；跨时区查看数据前请先对齐时区 |
| 端口被占用但 `netstat` 查不到 | Windows 上 Hyper-V / WSL 会预留动态端口段，落在区间内的端口无法绑定。可用 `netsh interface ipv4 show excludedportrange protocol=tcp` 确认 |
| Android 客户端无法连接或白屏 | 确认手机网络与「服务器设置」中的站点地址；客户端仅承载网页，离线不可用。长按桌面图标可重新进入服务器设置 |

## 开发文档

| 目标 | 入口 |
| --- | --- |
| 初次使用平台 | [平台使用手册](notes/03-平台使用运维与二次开发手册/平台使用手册.md) |
| 修改代码或新增功能 | [平台运维与二次开发手册](notes/03-平台使用运维与二次开发手册/平台运维与二次开发手册.md) |
| 了解目录划分依据 | [项目目录导航](notes/00-项目目录导航.md) |
| 按阶段排查遥测离线与队列问题 | [修复专题](notes/01-项目调研规划与开发建设记录/专项修复-遥测链路可靠性/00-索引·遥测链路可靠性修复.md) · [新对话接续](notes/01-项目调研规划与开发建设记录/专项修复-遥测链路可靠性/03-跨对话交接/00-TR当前接续入口.md) · [S01交接](notes/01-项目调研规划与开发建设记录/专项修复-遥测链路可靠性/02-分阶段实施/01-S01监控与失败状态/04-TR-S01阶段交接.md) · [S02交接](notes/01-项目调研规划与开发建设记录/专项修复-遥测链路可靠性/02-分阶段实施/02-S02维护门禁与超时/04-TR-S02阶段交接.md) · [S03交接](notes/01-项目调研规划与开发建设记录/专项修复-遥测链路可靠性/02-分阶段实施/03-S03公平调度与可重试机制/04-TR-S03阶段交接.md) · [S04交接](notes/01-项目调研规划与开发建设记录/专项修复-遥测链路可靠性/02-分阶段实施/04-S04容量恢复与实时通道/04-TR-S04阶段交接.md) |
| 查看当前发布、镜像与开关 | [当前系统状态](notes/00-当前系统状态.md)（明细分卷在 `notes/06-当前系统状态/`） |
| 按阶段阅读源码说明（一至五） | [阶段一](stages/01-data-chain/README.md) · [阶段二](stages/02-backend/README.md) · [阶段三](stages/03-ai-prediction/README.md) · [阶段四](stages/04-frontend/README.md) · [阶段五](stages/05-feishu-notification/README.md) |
| 按阶段阅读源码说明（六至十） | [阶段六](stages/06-security-recovery/README.md) · [阶段七](stages/07-agent/README.md) · [阶段八](stages/08-agent-rag/README.md) · [阶段九](stages/09-ops-agent/README.md) · [阶段十](stages/10-security-agent/README.md) |
| 开发 Windows 桌面客户端（阶段十一） | [源码、构建与测试](stages/11-desktop-client/README.md) |
| 查阅桌面版设计、验收与交接 | [第五版总索引](notes/01-项目调研规划与开发建设记录/05-第五版-Windows桌面客户端/00-索引·第五版-Windows桌面客户端.md) · [团队交付](notes/01-项目调研规划与开发建设记录/05-第五版-Windows桌面客户端/02-分阶段实施/03-S03联合验收与团队交付/02-V5-S03测试与交付.md) · [正式交接](notes/01-项目调研规划与开发建设记录/05-第五版-Windows桌面客户端/03-跨对话交接/V5-S03-阶段交接.md) |
| 构建安卓安装包（阶段十二） | [源码、构建与安装](stages/12-android-client/README.md) |
| 查阅手机端与安卓版设计、验收与交接 | [第八版总索引](notes/01-项目调研规划与开发建设记录/08-第八版-移动端与安卓客户端/00-索引·第八版-移动端与安卓客户端.md) · [安卓测试与交付](notes/01-项目调研规划与开发建设记录/08-第八版-移动端与安卓客户端/02-分阶段实施/02-S02安卓客户端/02-V8-S02测试与交付.md) · [正式交接](notes/01-项目调研规划与开发建设记录/08-第八版-移动端与安卓客户端/03-跨对话交接/V8-S02-阶段交接.md) |
| 部署到自有服务器 | [部署说明](stages/04-frontend/deploy/README.md) · [上传服务器规范](notes/02-工程规范边界与验收治理/项目工程规范/04-生产发布与恢复/01-上传服务器与分级发布规范.md) |
| 查阅工程规范与产物目录 | [按目的整理的工程规范](notes/02-工程规范边界与验收治理/项目工程规范/00-索引·项目工程规范.md) · [运行产物说明](notes/02-工程规范边界与验收治理/运行产物说明/00-索引·运行产物说明.md) · [代码工作区导航](notes/05-代码工作区导航与链接映射/00-索引·代码工作区导航.md) |
| 查阅入库积压与死信出口 | [专项索引](notes/01-项目调研规划与开发建设记录/专项修复-入库容错与死信出口/00-索引·入库容错与死信出口.md)；实际生效阶段见[当前系统状态](notes/00-当前系统状态.md) |
| 提升开发、测试与发布效率 | [分级流程与本次优化记录](notes/04-系统部署发布备份与变更记录/部署与运维日志/05-发布工具链与文档治理/01-发布机制与门禁/54-开发测试发布与备份流程提效.md) |
| 查阅历史发布与恢复记录 | [部署与运维日志索引](notes/04-系统部署发布备份与变更记录/部署与运维日志/00-索引·部署与运维日志.md) |
| 核对里程碑与待办 | [项目验收与收尾清单](notes/02-工程规范边界与验收治理/项目验收与收尾清单/00-索引·项目验收与收尾清单.md) |

各阶段的验收结论、测试证据与遗留项记录在该阶段笔记的「测试与交付」页，本文不再汇总里程碑表。

> 笔记库是独立的 Obsidian Vault。请仅打开 `PowerSystem/notes`；若打开仓库根目录，全库双链与图谱将失效。

## 数据与使用边界

仓库公开的内容包括源码、配置模板与笔记。`artifacts/` 下的模型、测试账本与截图，以及服务器私有 `.env`、数据卷与签名文件均不随 Git 提供；首次部署须自行生成模型并准备私有配置。演示环境与本文档中的所有数字均来自合成数据。

桌面团队包作为独立交付附件分发，不随 Git 源码克隆提供；其连接正式 HTTPS 网站，不具备离线业务能力。当前为内部未签名安装包，不含自动更新，系统通知受 Windows 设置、网络与休眠状态影响。Android 安装包同样使用自签名密钥，构建产物不入库，仅供内部安装。具体已验证范围、证据缺口与版本收尾结论统一见 [当前系统状态](notes/00-当前系统状态.md) 及相应阶段交接。

## 目录

- [系统概述](#系统概述)
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
│   ├── 03-ai-prediction/   故障预测、模型训练与质量评估
│   ├── 04-frontend/        Vue 控制台、移动端布局与部署脚本
│   ├── 05-feishu-notification/  飞书告警
│   ├── 06-security-recovery/    公网安全与备份恢复
│   ├── 07-agent/           告警解读与值班问答
│   ├── 08-agent-rag/       检索增强问答
│   ├── 09-ops-agent/       运维动作智能体
│   ├── 10-security-agent/  安全审查与门禁
│   ├── 11-desktop-client/  Windows 桌面客户端
│   └── 12-android-client/  Android 客户端（WebView 外壳）
├── notes/          Obsidian 笔记库（设计、验收、部署日志、手册）
├── artifacts/      阶段快照与批次证据（不入库，说明见 notes/02-工程规范边界与验收治理/运行产物说明/）
├── deploy/         边缘服务器部署配置
├── tools/          笔记治理与知识图谱校验
├── shared-memory/  跨会话过程记录（不作验收依据）
└── backups/        备份登记表（包体不入库）
```

## English

**PowerSystem** is an integrated operations platform for renewable-energy assets: telemetry ingestion, time-series storage, alarm lifecycle, health scoring, failure prediction, LLM-based alarm interpretation, and guarded automation.

Devices publish over MQTT to EMQX; a Go gateway writes to TDengine and fans out to PostgreSQL, Redis and WebSocket clients; a Vue 3 console renders the data; Python agents answer questions, run scheduled jobs and review high-risk actions with a separate security model.

The platform is available in a browser and through an Electron-based Windows desktop client, which adds tray controls, system notifications and native file saving. A WebView-based Android client reuses the same web interface.

**Highlights**

- End-to-end chain: ingest → store → detect → explain → act → review
- 143 synthetic devices across 5 stations, 5-second sampling, replay on reconnect
- Layered answers that separate verified facts from unverified explanation
- Sensitive assistant actions require explicit approval and leave an audit trail; enabled actions are listed on the current status page
- Windows and Android clients that reuse the same web interface
- Releases are traceable by batch and evidence; the checks and backup status actually performed are recorded per batch

**Quick start**

To use the platform, open the [website](https://www.cyancarp.cn/) or install the Windows team package and sign in with an existing account. Desktop installation does not require the development tools below; installation and upgrade instructions are included in `TEAM-GUIDE.md`.

For local development, read the [home-dev guide](stages/02-backend/deploy/home-dev/README.md) first and choose either the remote API or the local backend workflow; then start the Vue frontend.

Development requirements depend on the selected workflow. The frontend requires Node.js 20.19+ or 22.12+; API and WebSocket proxy targets are configured as described in the home-dev guide.

**Notes**

All fleet data in this repository is synthetic and must not be used as a basis for real plant operations. Runtime status and enabled capabilities are maintained only on the linked current status page. Source code, configuration templates and notes are public; models, credentials, data volumes and evidence bundles are not. Start from the [current system status](notes/00-当前系统状态.md) and the [project directory guide](notes/00-项目目录导航.md).

Desktop packages and Android APKs are produced separately from the source repository. The internal Windows package is unsigned, has no automatic updates and requires the online platform; system notification delivery is best effort. Desktop source and development instructions are in [stage eleven](stages/11-desktop-client/README.md).

Release and rollback records for the telemetry reliability work are documented in [遥测可靠性发布与前滚门禁](notes/04-系统部署发布备份与变更记录/部署与运维日志/05-发布工具链与文档治理/01-发布机制与门禁/53-遥测可靠性修复发布与前滚门禁.md); current runtime status is maintained on the [current system status](notes/00-当前系统状态.md) page.
