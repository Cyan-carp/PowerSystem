# 阶段三 · 未来一小时故障风险

本阶段用离线合成数据训练可解释的 XGBoost 模型，经 FastAPI 服务供阶段二 Go 后端调用。训练数据和模型来自合成逆变器；评估结果只说明该合成场景。阶段二的 M2 代码、验收库及记录保持独立。

对应笔记：[00-索引·开发阶段3-AI预测模块](../../开发阶段3-AI预测模块/00-索引·开发阶段3-AI预测模块.md)。

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

首次定时检查约在 API 启动 15 秒后，随后每 5 分钟检查。演示可设置 `AI_POLL_SECONDS=10`，正式运行保持 300。

新路由 `GET /api/v1/devices/{id}/prediction` 使用阶段二 Bearer JWT，返回 `code/message/data`。预测结果含概率、阈值、风险等级、特征依据、模型版本、窗口结束时间、数据来源及 `stale`。无预测返回 404；超过 15 分钟的旧结果会标记过期。

FastAPI 的 `POST /predict` 只监听本机 127.0.0.1:8090；部署时也可用本目录的 Dockerfile 与 compose.yaml。Go 仅在最新遥测及 30 分钟窗口足够新且完整时调用预测服务。服务不可用时保留上次结果并在查询中标记过期，不产生新高风险告警。

可用以下命令向现有 MQTT 链路发送带前兆的演示数据，使用已登记的 `INV-1001`；先确认没有同设备的其他模拟器并行上报：

```powershell
$env:PYTHONPATH = "$(Resolve-Path .\artifacts\stage3\pydeps);$(Resolve-Path .\stages\03-ai-prediction)"
python -m prediction.replay --lead-minutes 3 --full-cycle
```

它补入截至当前分钟的 30 个历史点，随后按分钟发送故障及恢复后的正常点，整轮约 43 分钟。演示时将轮询间隔设为 10 秒，检查**故障点发送前**是否已有高风险预测与告警，以及后续是否恢复。阶段二遥测有 ±48 小时时间戳约束，因此多天离线数据不能整批回放进运行库。

需要形成可复核的 M3 证据时，使用 `verify-m3.py`。它先检查 Docker Engine、六个端口、模型及 MQTT Python 依赖，再新建独立的 `INV-M3-*` 测试设备、自动回放、用 JWT 查询预测与告警、核对 PostgreSQL 唯一记录和 WebSocket 事件，最后重启阶段二服务验证去重。运行中不要再手动执行上面的 `prediction.replay`，也不要向同一设备并行发送模拟遥测。完整运行约 43 分钟，报告写入被 Git 忽略的目录：

```powershell
$python = 'C:\path\to\python3.12.exe' # 换成本机可用的 Python 3.12 路径
$env:PYTHONPATH = "$(Resolve-Path .\artifacts\stage3\pydeps);$(Resolve-Path .\stages\03-ai-prediction)"
$run = Join-Path .\artifacts\stage3 ("integration-" + (Get-Date -Format 'yyyyMMdd-HHmmss'))
& $python .\stages\03-ai-prediction\scripts\verify-m3.py --output $run --preflight-only
& $python .\stages\03-ai-prediction\scripts\verify-m3.py --output $run
```

`environment.json` 是预检证据；完整运行还写 `summary.json`、`websocket.jsonl` 和 `replay.stderr.log`。退出码含义：

| 退出码 | 含义 |
| --- | --- |
| 2 | 预检失败 |
| 1 | 完整运行中断或断言失败 |

`--skip-restart` 仅供诊断且不能算 M3 通过。该脚本会调用阶段二已实测的 WebSocket 烟测客户端和本机 Docker CLI；阶段一脚本也支持发现用户目录中的 Docker Desktop CLI。

若完整回放已经结束、仅重启步骤因脚本或环境问题中断，可补做一次新鲜窗口的独立重启去重检查，无需再等待 43 分钟。它新建设备并只发送 30 条前兆历史点，验证重启前后各有一条预测和 AI 告警；**此模式不发送真正故障，不能替代上面的完整回放**。测试设备可能留下一条未恢复的合成告警，应按设备编号识别：

```powershell
& $python .\stages\03-ai-prediction\scripts\verify-m3.py --output .\artifacts\stage3\integration-restart --restart-check
```

停止 AI 服务：`powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\stages\03-ai-prediction\scripts\stop-ai.ps1`。阶段二服务使用自己的 `stop-stage2.ps1`。

## 三、评估与公开数据

训练报告在 `artifacts/stage3/model/evaluation.md` 和 `evaluation.json`。模型输入只含窗口结束前的电压、电流、温度、功率；`status` 和 `fault_code` 不进入特征。正样本定义为窗口结束后 60 分钟内首次发生故障，故障中及恢复后的过渡窗口排除。训练、验证、测试按日期顺序切分。

从已保存模型和相同的测试日期生成全局特征贡献排序：

```powershell
$env:PYTHONPATH = "$(Resolve-Path .\artifacts\stage3\pydeps);$(Resolve-Path .\stages\03-ai-prediction)"
python -m prediction.report_model --data .\artifacts\stage3\synthetic --model .\artifacts\stage3\model --out .\artifacts\stage3\model\importance.json
```

结果采用测试窗平均绝对 TreeSHAP 贡献，详细解释见 [阶段三总结](../../开发阶段3-AI预测模块/07-阶段三总结.md)。它是全局解释，不等于 `/predict` 响应中单个窗口的 `top_factors`。

下载公开光伏数据后先做字段盘点：

```powershell
python -m prediction.inspect_external 'C:\path\to\downloaded-dataset' --out .\artifacts\stage3\external-audit.json
```

该脚本只列出实际 CSV/MAT 字段与数组，不擅自宣称能预测未来故障。只有核实独立故障起点与因果窗口后，才单独运行未来风险外部验证；否则结果只能称为故障识别对照。可参考 [16 天光伏故障数据](https://github.com/clayton-h-costa/pv_fault_dataset) 与 [GPVS-Faults](https://data.mendeley.com/datasets/n76t439f65/1)。

本轮已审计前者的两份 MAT 文件。可从作者仓库的固定提交下载并校验哈希，然后重跑标签变化、缺失和 SHA-256 检查：

```powershell
python .\stages\03-ai-prediction\scripts\download-pv-fault.py --output .\artifacts\stage3\external\pv_fault_dataset
python -m prediction.inspect_external .\artifacts\stage3\external\pv_fault_dataset --out .\artifacts\stage3\external\pv_fault_dataset\inventory.json
python -m prediction.audit_pv_fault .\artifacts\stage3\external\pv_fault_dataset --out .\artifacts\stage3\external\pv_fault_dataset\audit.json
```

数据来源、许可、字段映射和无法验证提前一小时预测的原因见 [公开数据审计](../../开发阶段3-AI预测模块/08-公开数据审计.md)。原始数据留在被忽略的 `artifacts/`，按原作者许可证和引用要求自行获取。

同一审计还取得 [OpenCEM](https://github.com/OpenCEM-platform/opencem-dataset) 的两个 CSV 分片。取得对应分片后可核对列数、时间间隔、部分关键字段缺失及故障标签列：

```powershell
python -m prediction.audit_opencem .\artifacts\stage3\external\opencem\2025-07-a.csv .\artifacts\stage3\external\opencem\2026-04-a.csv --support-zip .\artifacts\stage3\external\opencem\opencem-v1.0.0-support.zip --out .\artifacts\stage3\external\opencem\audit.json
```

OpenCEM 数据许可为 CC BY 4.0，当前分片未给出可核对的故障起点；其时间戳不能单独构成故障标签。

## 四、检查与工程边界

```powershell
$env:PYTHONPATH = "$(Resolve-Path .\artifacts\stage3\pydeps);$(Resolve-Path .\stages\03-ai-prediction)"
python -m unittest discover -s .\stages\03-ai-prediction\tests -v
Push-Location .\stages\02-backend
go test ./...
Pop-Location
```

完整 M3 需在 Docker/EMQX/TDengine/PostgreSQL/Redis 可用时执行，验证以下链路：

- 预测在故障发生前产生
- 结果存库、JWT 查询
- AI 告警确认与恢复、WebSocket 推送
- 服务重启去重

当前合成测试成绩不应表述为真实场站准确率。智能体只规划只读诊断工具，见 [05-智能体接口设计](../../开发阶段3-AI预测模块/05-智能体接口设计.md)。
