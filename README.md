# PowerSystem · 阶段一：跑通数据链路

本地链路：3 台 100 kW 光伏逆变器模拟器 → MQTT QoS 1 → EMQX → Go 网关 → TDengine。当前只实现阶段一；Redis、告警、前端和业务库属于后续阶段。

## 环境

- Windows 自带 PowerShell 5.1 或 PowerShell 7、Docker Desktop（显示 Engine running）、Python 3.12+、Go 1.24+。
- 首次运行需要网络拉取 `emqx:5.8.8`、`tdengine/tsdb:3.4.2.8` 及 Python/Go 依赖。
- 端口 `127.0.0.1:1883`（MQTT）、`127.0.0.1:18083`（EMQX 控制台）、`127.0.0.1:6041`（TDengine REST）。
- `.env` 仅在本机保存密码，已被 Git 忽略。首次预检会生成强随机密码。不要把 `.env` 或审查包上传到公开仓库。

## 从项目根目录执行

在当前 PowerShell 窗口进入 `C:\Users\23103\Desktop\新能源设备智能运维平台\PowerSystem`，确认提示符位于该目录，然后执行 Windows 自带的 `powershell.exe`：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\preflight-stage1.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\run-stage1-test.ps1
```

预检会启动 EMQX/TDengine、校验 TDengine 登录、准备 `.venv` 并编译 Go 网关。第二条命令运行约 1 小时，每台设备每 5 秒生成 1 条，合计每台 720 条、总计 2160 条。仿真时间加速 24 倍，因此 1 小时可覆盖完整日照周期；真实发布间隔仍为 5 秒。脚本约在第 20 分钟停 EMQX 30 秒并恢复，约在第 40 分钟重启网关。两次故障期间及恢复后的数据会继续自动入库。

脚本输出唯一运行编号（如 `stage1_20260927_140000_abcd`）和审查包绝对路径。无需人工持续监视，但应保持终端、电脑和 Docker Desktop 运行。日志持续写入 `artifacts/stage1/<运行编号>/`，包括 `metrics.csv`、`events.jsonl`、模拟器、网关和容器日志。退出时自动生成 `reconciliation.json`、`telemetry.csv`、`summary.md`、`review-bundle.zip`。

### 交给我审查

- **正常完成**：只需发该次 `review-bundle.zip` 的绝对路径。我会核对每台 720 条、序号连续、无重复、待发/待写队列清零和恢复耗时。完整 1 小时测试通过前，不将 `dev` 合并进 `main`。
- **脚本报告失败或主动中断**：发同一审查包的绝对路径、终端最后的错误文字及中断原因。
- **重启、断电或强制结束**：重新打开 Docker Desktop 后，在项目根目录运行 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\collect-stage1-test.ps1 -RunId <运行编号>`。发重新生成的审查包绝对路径及大约中断时间；如果 Docker Desktop 窗口显示未写入日志的错误，再附截图。运行编号可以从 `artifacts/stage1/` 下的文件夹名找回。

`collect-stage1-test.ps1` 可重复执行，会重做对账和压缩包；原始日志与 SQLite 数据保留。若 TDengine 当时不可用，对账 JSON 会记录查询错误，现有日志仍会打包。

## 短时开发验证

下面命令只供开发验证，不代表一小时 M1 验收：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\run-stage1-test.ps1 -SamplesPerDevice 60 -IntervalSeconds 1 -BrokerFaultAtSeconds 7 -GatewayFaultAtSeconds 45 -BrokerDowntimeSeconds 8
```

## 数据、配置与迁移

每次测试创建独立 TDengine 数据库，名与运行编号相同。SQL 模板在 `deploy/tdengine/init.sql`。SQLite 账本保留所有已生成样本，模拟器待发队列在 Broker 确认后清除；网关待写队列在 TDengine 成功响应后清除。网关以 `(device_id, seq)` 去重，TDengine 用设备子表和时间戳防重复。项目文件、生成物、Docker 卷、备份与清理方式详见 [[05-项目文件位置与迁移清单]]。

停止容器：`docker compose stop`。再次运行预检会启动容器。删除 Docker 卷会删除 EMQX 会话和 TDengine 数据，迁移前先按清单备份。
