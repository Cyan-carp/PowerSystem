# 阶段一 · 数据链路运行说明

> 本地链路：**3 台 100 kW 光伏逆变器模拟器 → MQTT QoS 1 → EMQX → Go 网关 → TDengine**。
>
> 当前只实现阶段一；Redis、告警、前端和业务库属于后续阶段。

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

在当前 PowerShell 窗口**先执行第一行切换目录**。看到提示符变为 `PS C:\Users\23103\Desktop\新能源设备智能运维平台\PowerSystem>` 后，再依次执行预检和正式测试：

```powershell
Set-Location -LiteralPath 'C:\Users\23103\Desktop\新能源设备智能运维平台\PowerSystem'
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
| 日志目录 | `artifacts/stage1/<运行编号>/`，含 `metrics.csv`、`events.jsonl`、模拟器/网关/容器日志 |
| 自动产物 | 退出时生成 `reconciliation.json`、`telemetry.csv`、`summary.md`、`review-bundle.zip` |

> 无需人工持续监视，但应保持终端、电脑和 Docker Desktop 运行。

## 五、交给我审查

| 情况 | 做法 |
| --- | --- |
| 正常完成 | 发该次 `review-bundle.zip` 的绝对路径 |
| 脚本失败 / 主动中断 | 发审查包绝对路径、终端最后错误文字及中断原因 |
| 重启 / 断电 / 强制结束 | 重新打开 Docker Desktop 后，在项目根目录运行 `collect-stage1-test.ps1 -RunId <运行编号>`，发重新生成的审查包绝对路径及大约中断时间；Docker Desktop 窗口若显示未写日志的错误，再附截图 |

我会核对：每台 720 条、序号连续、无重复、待发/待写队列清零和恢复耗时。首次一小时 M1 已于 2026-09-27 审查通过，结果见 [04-阶段一测试与交付](../../开发阶段1-数据链路/04-阶段一测试与交付.md)。

**运行编号找回**：从 `artifacts/stage1/` 下的文件夹名找回。

> `collect-stage1-test.ps1` 可重复执行，会重做对账和压缩包；原始日志与 SQLite 数据保留。若 TDengine 当时不可用，对账 JSON 会记录查询错误，现有日志仍会打包。

## 六、短时开发验证

下面命令只供开发验证，**不代表一小时 M1 验收**：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\stages\01-data-chain\scripts\run-stage1-test.ps1 -SamplesPerDevice 60 -IntervalSeconds 1 -BrokerFaultAtSeconds 7 -GatewayFaultAtSeconds 45 -BrokerDowntimeSeconds 8
```

## 七、数据、配置与迁移

| 项 | 说明 |
| --- | --- |
| 独立数据库 | 每次测试创建独立 TDengine 数据库，名与运行编号相同 |
| SQL 模板 | `stages/01-data-chain/deploy/tdengine/init.sql` |
| SQLite 账本 | 保留所有已生成样本 |
| 模拟器待发队列 | Broker 确认后清除 |
| 网关待写队列 | TDengine 成功响应后清除 |
| 去重 | 网关按 `(device_id, seq)`；TDengine 用设备子表和时间戳防重复 |

项目文件、生成物、Docker 卷、备份与清理方式详见 [05-项目文件位置与迁移清单](../../开发阶段1-数据链路/05-项目文件位置与迁移清单.md)。

**停止容器**：

```powershell
docker compose --project-name powersystem --env-file .env --file .\stages\01-data-chain\compose.yaml stop
```

> 再次运行预检会启动容器。删除 Docker 卷会删除 EMQX 会话和 TDengine 数据，迁移前先按清单备份。
