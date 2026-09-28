# 阶段三 · 未来一小时故障风险

本阶段用离线合成数据训练可解释的 XGBoost 模型，经 FastAPI 服务供阶段二 Go 后端调用。训练数据和模型来自合成逆变器；评估结果只说明该合成场景。阶段二的 M2 代码、验收库及记录保持独立。

对应笔记：[[开发阶段3-AI预测模块/00-索引·开发阶段3-AI预测模块]]。

## 一、准备与快速训练

需要 Python 3.12、PowerShell，以及阶段二的 Go 与 Docker 运行环境。以下从仓库根目录执行；`-Python` 可传本机 Python 可执行文件的完整路径：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\stages\03-ai-prediction\scripts\build-model.ps1 -Python python -Days 21
```

脚本安装锁定版本的依赖到 `artifacts/stage3/pydeps/`，离线生成 21 天、3 台设备的分钟级遥测及故障起点，再按日期切分训练、验证、测试集。输出：

| 路径 | 内容 |
| --- | --- |
| `artifacts/stage3/synthetic/` | `telemetry.csv.gz`、`fault-events.jsonl`、生成参数 |
| `artifacts/stage3/model/` | XGBoost 模型、阈值、特征契约及评估报告 |

默认生成器固定种子 2026；改种子需直接调用 `python -m prediction.generate --seed ...`。所有运行产物都被 Git 忽略。模型由本项目合成数据训练，不是公开数据集上的预训练权重。

## 二、启动预测链路

先启动 AI 服务，健康检查应返回模型版本：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\stages\03-ai-prediction\scripts\start-ai.ps1 -Python python
Invoke-RestMethod http://127.0.0.1:8090/health
```

阶段二服务需要按原 README 预检并启动；仅本次启用预测轮询：

```powershell
$env:AI_ENABLED = 'true'
$env:AI_URL = 'http://127.0.0.1:8090'
$env:AI_POLL_SECONDS = '300'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\stages\02-backend\scripts\start-stage2.ps1
```

首次定时检查约在 API 启动 15 秒后，随后每 5 分钟检查。演示可设置 `AI_POLL_SECONDS=10`，正式运行保持 300。新路由 `GET /api/v1/devices/{id}/prediction` 使用阶段二 Bearer JWT，返回 `code/message/data`。预测结果含概率、阈值、风险等级、特征依据、模型版本、窗口结束时间、数据来源及 `stale`。无预测返回 404；超过 15 分钟的旧结果会标记过期。

FastAPI 的 `POST /predict` 只监听本机 127.0.0.1:8090；部署时也可用本目录的 Dockerfile 与 compose.yaml。Go 仅在最新遥测及 30 分钟窗口足够新且完整时调用预测服务。服务不可用时保留上次结果并在查询中标记过期，不产生新高风险告警。

可用以下命令向现有 MQTT 链路发送带前兆的演示数据，使用已登记的 `INV-1001`；先确认没有同设备的其他模拟器并行上报：

```powershell
$env:PYTHONPATH = "$(Resolve-Path .\artifacts\stage3\pydeps);$(Resolve-Path .\stages\03-ai-prediction)"
python -m prediction.replay --lead-minutes 3 --full-cycle
```

它补入截至当前分钟的 30 个历史点，随后按分钟发送故障及恢复后的正常点，整轮约 43 分钟。演示时将轮询间隔设为 10 秒，检查**故障点发送前**是否已有高风险预测与告警，以及后续是否恢复。阶段二遥测有 ±48 小时时间戳约束，因此多天离线数据不能整批回放进运行库。

停止 AI 服务：`powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\stages\03-ai-prediction\scripts\stop-ai.ps1`。阶段二服务使用自己的 `stop-stage2.ps1`。

## 三、评估与公开数据

训练报告在 `artifacts/stage3/model/evaluation.md` 和 `evaluation.json`。模型输入只含窗口结束前的电压、电流、温度、功率；`status` 和 `fault_code` 不进入特征。正样本定义为窗口结束后 60 分钟内首次发生故障，故障中及恢复后的过渡窗口排除。训练、验证、测试按日期顺序切分。

下载公开光伏数据后先做字段盘点：

```powershell
python -m prediction.inspect_external 'C:\path\to\downloaded-dataset' --out .\artifacts\stage3\external-audit.json
```

该脚本只列出实际 CSV/MAT 字段与数组，不擅自宣称能预测未来故障。只有核实独立故障起点与因果窗口后，才单独运行未来风险外部验证；否则结果只能称为故障识别对照。可参考 [16 天光伏故障数据](https://github.com/clayton-h-costa/pv_fault_dataset) 与 [GPVS-Faults](https://data.mendeley.com/datasets/n76t439f65/1)。

## 四、检查与工程边界

```powershell
python -m unittest discover -s .\stages\03-ai-prediction\tests -v
Push-Location .\stages\02-backend
go test ./...
Pop-Location
```

完整 M3 需在 Docker/EMQX/TDengine/PostgreSQL/Redis 可用时执行：验证预测在故障发生前产生、结果存库、JWT 查询、AI 告警确认与恢复、WebSocket 推送及服务重启去重。当前合成测试成绩不应表述为真实场站准确率。智能体只规划只读诊断工具，见 [[开发阶段3-AI预测模块/05-智能体接口设计]]。
