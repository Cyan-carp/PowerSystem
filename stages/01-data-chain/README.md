# 阶段一 · 数据链路运行说明

> 供数现状：共享发布锁修复已于 2026-10-21 07:39 发布（`fleet-mqtt-lockfix-20261021-r1`，真实 Broker 探针 1430/1430），首轮有效观测恢复为 all=143、business=140、baseline=3。当前生产指针、设备口径与长观察结论见[当前系统状态](../../notes/00-当前系统状态.md)；容量与稳定性证据见[容量与长时间测试记录](../../notes/01-项目调研规划与开发建设记录/03-第三版-设备扩容/03-容量与稳定性验证/08-容量与长时间测试记录.md)。

> 本地链路：**3 台 100 kW 光伏逆变器模拟器 → MQTT QoS 1 → EMQX → Go 网关 → TDengine**。
>
> 本说明只覆盖阶段一运行与 M1 验收；Redis、告警、前端和业务库由后续阶段实现，运行入口见仓库根 [README](../../README.md)。

## 模拟器遥测窗口开关

模拟器的`make_sample`与CLI默认`window_mode/window-mode=off`，输出正常合成曲线；fleet示例显式设置`--window-mode on`以生成下述合法规则窗口。`--fault-mode`仅为旧命令兼容保留，不能代替窗口开关。

显式`--window-mode on`开启五个固定遥测窗口，使用通过接入物理范围与电气一致性校验的数值；旧越界目标已替换。实际部署开关和告警表现只见[当前系统状态](../../notes/00-当前系统状态.md)。
## 一、环境要求

| 依赖 | 要求 |
| --- | --- |
| PowerShell | Windows 自带 5.1 或 PowerShell 7 |
| Docker | Docker Desktop（显示 Engine running） |
| Python | 3.12+ |
| Go | 1.24+ |

- 首次运行需要网络拉取 `emqx:5.8.8`、`tdengine/tsdb:3.4.2.8` 及 Python/Go 依赖。
- 端口只绑定本机回环：`127.0.0.1:1883`（MQTT）、`127.0.0.1:18083`（EMQX 控制台）、`127.0.0.1:6041`（TDengine REST）。

## 二、密码与安全

| 项 | 约定 |
| --- | --- |
| 密码存放 | `.env` 仅在本机保存，已被 Git 忽略 |
| 首次预检 | 生成强随机密码 |
| 安全纪律 | 不要把 `.env` 或审查包上传到公开仓库 |

## 三、从项目根目录执行

先在 PowerShell 中进入克隆后的 `PowerSystem` 仓库根目录，确认该目录下能看到 `stages/` 和 `.env.example`，再依次执行预检和正式测试：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\stages\01-data-chain\scripts\preflight-stage1.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\stages\01-data-chain\scripts\run-stage1-test.ps1
```

### 预检做了什么

启动 EMQX/TDengine、校验 TDengine 登录、准备 `.venv` 并编译 Go 网关。

### 正式测试做了什么

| 项 | 说明 |
| --- | --- |
| 运行时长 | 约 1 小时 |
| 数据节奏 | 每台设备每 5 秒生成 1 条 |
| 数据量 | 每台 720 条、总计 **2160 条** |
| 时间加速 | 仿真时间加速 24 倍，1 小时覆盖完整日照周期；真实发布间隔仍为 5 秒 |
| 故障注入 | 约第 20 分钟停 EMQX 30 秒并恢复；约第 40 分钟重启网关 |

> 两次故障期间及恢复后的数据会继续自动入库。

## 四、脚本输出与日志

| 项 | 说明 |
| --- | --- |
| 运行编号 | 脚本输出唯一编号（如 `stage1_20260927_140000_abcd`） |
| 审查包 | 输出绝对路径 |
| 日志目录 | `artifacts/stage1-数据链路/<运行编号>/`，含 `metrics.csv`、`events.jsonl`、模拟器/网关/容器日志 |
| 自动产物 | 退出时生成 `reconciliation.json`、`telemetry.csv`、`summary.md`、`review-bundle.zip` |

> 无需人工持续监视，但应保持终端、电脑和 Docker Desktop 运行。

## 五、验收材料与复核

| 情况 | 做法 |
| --- | --- |
| 正常完成 | 记录并提供该次 `review-bundle.zip` 的绝对路径 |
| 脚本失败 / 主动中断 | 保留审查包、终端最后错误文字及中断原因 |
| 重启 / 断电 / 强制结束 | 重新打开 Docker Desktop 后，在项目根目录运行 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\stages\01-data-chain\scripts\collect-stage1-test.ps1 -RunId <运行编号>`，记录重新生成的审查包路径及大约中断时间；Docker Desktop 窗口若显示未写日志的错误，再附截图 |

审查者应核对每台 720 条、序号连续、无重复、待发/待写队列清零和恢复耗时。首次一小时 M1 已于 2026-09-27 审查通过，结果见 [04-阶段一测试与交付](../../notes/01-项目调研规划与开发建设记录/01-第一版-平台主体/05-功能开发与阶段交付/01-数据链路/04-阶段一测试与交付.md)。

**运行编号找回**：从 `artifacts/stage1-数据链路/` 下的文件夹名找回。

> `collect-stage1-test.ps1` 可重复执行，会重做对账和压缩包；原始日志与 SQLite 数据保留。若 TDengine 当时不可用，对账 JSON 会记录查询错误，现有日志仍会打包。

## 六、短时开发验证

### 线上演示的白天循环

阶段四演示部署启用 `--continuous --daylight-loop-minutes 60 --fault-mode scheduled`：每 60 分钟循环一次模拟的 08:00–16:00 发电过程，夜间也能观察发电曲线；故障温升独立按真实北京时间的日程注入，每设备每日一至两个窗口。遥测使用真实当前时间，每台设备的持久化序号继续递增，原有历史和待发队列保留。2026-10-20 已恢复该日程，此前显式关闭注入的时段作为历史保留；正式生效及单次在线告警事实以原专题笔记为准。

不传 `--daylight-loop-minutes`（或设为 `0`）时，连续模式仍按北京时间自然日模拟，夜间功率为零。阶段一有限样本测试不启用此选项。

`--fault-mode` 独立控制故障：`off` 关闭注入，`scheduled` 按真实采样日期/时间执行确定性日程，不使用白天循环的压缩时钟。默认值与现行演示配置均为 `scheduled`，保留原自然昼夜和有限样本口径；后续开关调整在完整候选中显式指定，经统一发布入口更新模拟器。关闭注入不会关闭 AI 预测或告警规则，也不保证所有模型窗口永远低风险。

### 自然规则告警的五窗口注入

显式`window_mode=on`时，在真实北京时间的五个窗口内生成异常遥测，供后端普通告警规则判定。它只改payload指标值，不写fault_code、不置fault_active、不直接创建告警；窗口结束自然回落，错过时段不追补。既有规则仍决定是否触发，实际运行状态见[唯一状态页](../../notes/00-当前系统状态.md)。

| 北京时间 | 场站 | 指标 | 注入目标 | 对应规则 |
| --- | --- | --- | --- | --- |
| 09:00–09:10 | PV-01 | temperature | 65 °C | `>55` |
| 11:00–11:10 | PV-02 | voltage | 445 V | `>430` |
| 13:00–13:10 | PV-03 | temperature | 82 °C | `>80` |
| 15:00–15:10 | PV-04 | temperature | 65 °C | `>55` |
| 17:00–17:10 | ST-01 | temperature | 82 °C | `>80` |

- 每个窗口只作用于当天按日期和场站稳定轮换选中的一台设备；window-mode与fault-mode独立。
- 温度窗口保持电压/电流/功率/状态不变；电压窗口重算电流，维持三相电气一致性。温度不超过85℃、电流不超过160.4A、功率不超过110kW。
- 默认off可用于单独观察正常曲线；车队模式按`--fleet`清单定位设备，不创建台账或告警规则。
- 两天十时槽实际输出经过Go接入校验与独立PG规则触发/恢复测试；这是隔离验证，生产自然触发记录见唯一状态页及部署日志。

### 4 场站 140 台车队模式（本地代码）

`simulator/fleet.json` 是车队唯一清单：4 个业务场站共 140 台合成逆变器，并显式引用 `ST-01` 三台既有设备。传入 `--fleet simulator/fleet.json` 时，模拟器加载 143 台配置、复用一个 MQTT 客户端，并在每个 5 秒周期内错开发送；业务站按真实昼夜生成正常曲线，`ST-01` 保留 60 分钟白天循环和既有真实时间故障日程。不传 `--fleet` 时仍保持原三设备入口。

本轮只完成代码和离线单元测试，未连接真实 Broker、未启用 140 台供数，也未做容量验收。离线校验命令：

```powershell
python -m unittest stages/01-data-chain/simulator/test_fleet.py stages/01-data-chain/simulator/test_simulator.py -v
```

仅运行模拟器专项检查（需先安装该目录的 `requirements.txt`）：

```powershell
python -m unittest discover -s stages/01-data-chain/simulator -p test_simulator.py -v
```

2026-10-18 候选中10项无网络隔离检查通过；使用当前固定预测模型回放75个关闭注入的合成窗口，无高风险，阈值未改。原专题统一维护设计、失败/边界及服务器生效结果：[[notes/04-系统部署发布备份与变更记录/部署与运维日志/04-供数与业务批次/42-白天循环与故障日程（原补充业务02）]]。不据此重签 M1 或宣称真实设备精度通过。

下面命令只供开发验证，**不代表一小时 M1 验收**：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\stages\01-data-chain\scripts\run-stage1-test.ps1 -SamplesPerDevice 60 -IntervalSeconds 1 -BrokerFaultAtSeconds 7 -GatewayFaultAtSeconds 45 -BrokerDowntimeSeconds 8
```

## 七、数据、配置与迁移

### 设备扩容最小离线冒烟（7 台，约两分钟）

从仓库根目录运行，输出目录必须是尚不存在的新目录：

```powershell
python stages/01-data-chain/simulator/smoke_fleet.py --output artifacts/stage2/fleet-minimal-<运行编号>
```

夹具为 `simulator/fixtures/fleet-smoke-7.json`：`PV-01` 两台、`PV-04` 两台、`ST-01` 三台。脚本运行真实模拟器主循环，每台 24 个五秒样本，共 168 条；MQTT 客户端替换为异步离线回执，完全不连接 Broker。输出 SQLite 账本、故障标签与 `summary.json`；通过条件是每台 24 条、待发为 0、载荷身份与序号对应。它不修改默认 143 台清单。

| 项 | 说明 |
| --- | --- |
| 独立数据库 | 每次测试创建独立 TDengine 数据库，名与运行编号相同 |
| SQL 模板 | `stages/01-data-chain/deploy/tdengine/init.sql` |
| SQLite 账本 | 保留所有已生成样本 |
| 模拟器待发队列 | Broker 确认后清除 |
| 网关待写队列 | TDengine 成功响应后清除 |
| 去重 | 网关按 `(device_id, seq)`；TDengine 用设备子表和时间戳防重复 |

阶段一目录整理与 M1 证据位置见[阶段一目录迁移记录](../../notes/01-项目调研规划与开发建设记录/01-第一版-平台主体/05-功能开发与阶段交付/01-数据链路/05-阶段一目录迁移记录.md)；当前生成物、Docker 卷和备份边界见[运行产物说明](../../notes/02-工程规范边界与验收治理/运行产物说明/00-索引·运行产物说明.md)与[运维与二次开发手册](../../notes/03-平台使用运维与二次开发手册/平台运维与二次开发手册.md)。

**停止容器**：

```powershell
docker compose --project-name powersystem --env-file .env --file .\stages\01-data-chain\compose.yaml stop
```

> 再次运行预检会启动容器。删除 Docker 卷会删除 EMQX 会话和 TDengine 数据，迁移前先按清单备份。

> 白天循环是特定环境演示扩展，设计及运行边界见 [[notes/04-系统部署发布备份与变更记录/部署与运维日志/04-供数与业务批次/42-白天循环与故障日程（原补充业务02）]]。具体运行命令继续在源码 README 维护，原阶段签收范围不变。
