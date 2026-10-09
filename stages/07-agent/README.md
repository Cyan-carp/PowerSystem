# 第二版智能体 · 基础服务、告警解读与值班问答

当前正式版本为v2.0.0，V2-M4限定范围技术签收见[测试与交付](../../第二版-智能体升级/开发阶段4-测试、安全复核与交付/05-V2-M4测试与交付.md)。V2-M3历史索引见[使用引导与值班问答](../../第二版-智能体升级/开发阶段3-使用引导与值班问答/00-索引·使用引导与值班问答.md)，前三阶段验收保留各自核对日期。

> **阶段三历史状态（2026-10-05）**：V2-M3 合成演示与公开资料辅助建议范围技术验收通过，可以进入 V2-M4。最终候选为 `runtime/manual-advice-20261005T004409Z/`；专业来源审核按所有者授权暂缓，知识补齐与真实设备适用性尚未验证，第二版整体安全复核和最终 Release 归 V2-M4。

V2-M2 设计与交付进入[告警智能解读与大屏联动](../../第二版-智能体升级/开发阶段2-告警智能解读与大屏联动/00-索引·告警智能解读与大屏联动.md)。以下阶段一命令继续有效，阶段二附加配置与复现见文末。

本阶段实现 V2-T01～T04：独立 Agent、可配置模型 API、只读工具、当前用户鉴权、会话和审计。阶段索引见[基础服务与只读工具链](../../第二版-智能体升级/开发阶段1-基础服务与只读工具链/00-索引·基础服务与只读工具链.md)，验收见[V2-M1 测试与交付](../../第二版-智能体升级/开发阶段1-基础服务与只读工具链/05-V2-M1测试与交付.md)。

> 模拟模型仅用于协议与后端联调；真实模型 `deepseek-v4.1-flash` 已完成取证问答、续聊与空数据验证，V2-M1 本机合成演示范围已验收。当前已登录用户可读全部演示设备，尚无逐设备授权；Agent 不直连业务库，不确认告警、不建工单、不远控。

## 1. 本机准备

在仓库根目录执行。使用 PowerShell 7.2+、Python 3.12/3.13、Go 与 Docker Desktop；沿用根目录私有 `.env` 和四个既有容器，不重建数据卷。

```powershell
docker start powersystem-postgres-1 powersystem-redis-1 powersystem-tdengine-1 powersystem-emqx-1
.venv/Scripts/python.exe -m pip install --target artifacts/stage7-智能体/pydeps -r stages/07-agent/requirements.txt
Copy-Item stages/07-agent/.env.example stages/07-agent/.env
```

> 已有依赖目录时不重复安装；升级依赖前停止本阶段进程。不要把原 `.env` 覆盖成新密码，以免与数据卷不匹配。

## 2. 自行接入模型 API

编辑忽略的 `stages/07-agent/.env`：

| 配置 | 含义 |
| --- | --- |
| `AGENT_LLM_BASE_URL` | 兼容 API 基地址，通常以 `/v1` 结尾，不含 `/chat/completions` |
| `AGENT_LLM_MODEL` | 供应商提供的模型名称，必须支持原生工具调用 |
| `AGENT_LLM_API_KEY_FILE` | 密钥文件路径；文件仅含密钥，不含变量名、引号或 Bearer 前缀 |
| `AGENT_SERVICE_TOKEN_FILE` | Go 与 Agent 共用的 32 字符以上随机令牌文件 |
| `AGENT_AUDIT_DIR` | 本机审计目录，默认在忽略的 artifacts 内 |

把模型密钥放入 `artifacts/stage7-智能体/private/model-api-key`；本机脚本将相对路径解析到仓库根。服务令牌只需初始化一次：

```powershell
$private = Join-Path (Get-Location) 'artifacts/stage7-智能体/private'
New-Item -ItemType Directory -Path $private -Force | Out-Null
$tokenFile = Join-Path $private 'service-token'
if (-not (Test-Path -LiteralPath $tokenFile)) {
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    [IO.File]::WriteAllText($tokenFile, [Convert]::ToHexString($bytes))
}
```

本地 Ollama 可设 `http://127.0.0.1:11434/v1`，不需要密钥时删除 `AGENT_LLM_API_KEY_FILE` 行。远端 API 必须使用 HTTPS。配置示例与运行日志禁止保存真实密钥。

## 3. 启动与预检

先停止原阶段二启动脚本管理的 API／网关，确保 8080 空闲，再执行：

```powershell
stages/07-agent/scripts/start-local-backend.ps1
stages/07-agent/scripts/preflight-stage7.ps1 -ProbeModel
stages/07-agent/scripts/start-agent.ps1
```

Go API 为 8080，Agent 为回环 8092。预检分别测试普通响应和原生工具调用，不把“能聊天”等同于“能运行智能体”。`/health` 只表示服务可响应，不声明模型已经验收。

结束时：

```powershell
stages/07-agent/scripts/stop-agent.ps1 -IncludeBackend
```

只停止本阶段记录的 PID 与启动时间匹配的进程，不停止 Docker 或其它阶段服务。

## 4. API 与只读工具

`POST /api/v1/agent/chat`，使用用户 Bearer JWT。请求：

```json
{"message":"查询设备1的预测与当前温度，附证据。"}
```

继续对话时携带上次返回的 `session_id`（UUID）。成功使用既有 `code/message/data` 包络，`data` 含 `request_id/session_id/status/conclusion/evidence/suggestions/limitations/model`。结论和建议为 `{text,evidence_ids}`；证据由程序生成，含来源接口、查询时间、数据时间、模型原值。`status` 为 `answered/unable_to_determine/degraded`。鉴权失败为 401／403，会话并发为 409，组件不可用为 503。

六个工具：`list_devices/get_telemetry/get_prediction/list_alarms/get_alarm_detail/get_dashboard_summary`。分页默认 20、上限 100；历史最多 24 小时、5000 点，模型输入最多 100 个实际采样点并附全窗口统计。预测 15 分钟过期，最新遥测按接收时间 15 秒过期。列表完整性与汇总时间口径明确标记。

限制：最多 5 轮、10 次工具执行；工具 5 秒、模型每次 20 秒、Agent 全程 40 秒、代理 45 秒。会话 TTL 24 小时，最近 10 轮，跨用户拒绝。单次证据累计上限 64 KiB、模型请求上限 160 KiB，超限明确降级。模型数值及引用校验不能证明自然语言根因正确，建议仍须人工复核。

## 5. 测试与证据

离线测试：

```powershell
stages/07-agent/scripts/test-offline.ps1
```

真实模型联调前，把当前测试操作员 JWT 保存到忽略的私有文件，在当前 PowerShell 设置 `STAGE7_USER_TOKEN_FILE` 为该文件绝对路径；脚本不输出 JWT：

```powershell
$env:STAGE7_USER_TOKEN_FILE = Join-Path (Get-Location) 'artifacts/stage7-智能体/private/test-user-token'
stages/07-agent/scripts/run-stage7-test.ps1
```

完整证据在 `artifacts/stage7-智能体/<运行编号>/`。需要同时复核审计、正常问答、异常场景和后端短回归后才能签收，脚本不自动宣布里程碑通过。

2026-09-30 旧密钥terra模型超时，失败批次 `stage7_20260930_191610_5bef` 保留。新密钥现配置 `deepseek-v4.1-flash`，修复证据时间误拒绝后，22项离线与25项真实模型联调通过；正常问答约6.462秒、续聊约4.919秒，空数据返回无法判断。批次 `stage7_20260930_193510_00e1` 含审计及独立 `review.json`；V2-M1本机合成范围技术验收通过，未部署服务器。

所有者允许在超过20秒但正常返回时先解除单次模型限制；本轮新模型未触发条件，20秒模型、40秒Agent及45秒代理保持原值。预检失败也会保存 `run.json`，该文件不会自动签收里程碑。

本地协议模拟器位于 `tests/fixture_model.py`，模型名固定为 `stage7-test-fixture`，回环端口 8093；不打包进 Agent 镜像。测试配置路径见交付笔记，`--fixture` 标志会创建两个本机操作员测试账号，不可对服务器执行。

## 6. Compose 与后续接口

`compose.yaml` 提供本地 Agent 容器，使用 `host.docker.internal` 访问本机 Go API；Redis URL 的主机需自行配置为可达宿主机，密钥文件使用绝对路径。容器模式下 Ollama 无密钥时也要提供一个空密钥文件并设置其绝对路径。Windows 上准备可写 audit 目录后使用 `docker compose ... config --quiet`，不要打印展开后的配置。

`deploy/agent.compose.yaml` 是阶段四统一编排的可选 overlay，必须显式添加并启用 `agent` profile；没有公网端口，没有 API 到 Agent 的启动依赖。截至 V2-M1，上述服务器部署与备份恢复仍留到后续；2026-10-01 的 V2-M2 已实施服务器增量，当前结果见阶段二交付页。

阶段一只预留方向；V2-M2 已实现 `/internal/agent/interpret`，只接收 Go 的事件证据，不创建用户会话。历史 V2-M1 的“未部署”结论保持其核对日期，当前发布事实见 V2-M2 交付页。

## 7. V2-M2 本机与服务器

本机增加 `AGENT_INTERPRET_ENABLED=true` 与独立 `AGENT_MONITOR_TOKEN_FILE`。保留原私有文件，新增令牌至少 32 字符；本机脚本会解析相对路径。

- Go 每秒扫描新告警，两工作协程使用持久租约；首次启用不回填历史告警。
- 用户接口提供解读列表、详情、已读；管理员模型状态和检测需 admin，检测一分钟最多一次。
- 事件解读不使用用户 JWT，Go 提供不可变触发证据、历史曲线、告警及触发预测。
- 未配置、密钥失效、明确无额度时保留事件事实；恢复配置并重启 Agent 后，由管理员检测恢复。

本机验收脚本在已有四个容器、Go 和 Agent 就绪后执行：

```powershell
. stages/07-agent/scripts/stage7-common.ps1
Set-Stage7Environment -ConfigFile artifacts/stage7-智能体/private/v2m2-real.env
& $Stage7Python stages/07-agent/scripts/smoke-v2m2.py --real --output artifacts/stage7-智能体/v2m2/<运行编号>
```

> 本机脚本只调用回环入口，使用根私有配置签发短期本机测试 JWT；禁止对服务器执行。创建 `INV-V2M2-*` 合成设备、31 点曲线和测试事件，结束后恢复告警并软删除测试设备；原证据和表保留。协议测试去掉 `--real`，同时启动回环模型模拟器及对应私有配置；它不计真实 LLM 验收。

服务器沿用统一 Compose 加本目录 overlay 和 `agent` profile，监控配置追加独立 receiver，飞书保留原路径。文件配置、命令、备份与回退见[部署与故障隔离](../../第二版-智能体升级/开发阶段2-告警智能解读与大屏联动/04-服务器部署与故障隔离.md)。

`--groups 10` 采集三类各十个事件；可显式使用 `--allow-validation-degradation` 统计真实模型安全拒绝，但脚本仍要求每类至少三个成功回答，并分别记录成功数与拒绝数。最新 `real-20261001-final-guard30` 为 30/30 回答、87/87 检查、端到端 P95 27.84 秒，不把整批 66.07 秒当成单事件延迟。

`fault-v2m2.py` 与 `check-unconfigured.py` 是受控本机专项，需按交付笔记的进程/配置顺序执行。`deploy/acceptance.py` 只用于明确的 `/opt/powersystem` 服务器合成验收；`deploy/observe.py` 默认只读观察 30 分钟，不请求模型。测试源码不进入 Agent 镜像。

## 8. V2-M3 知识、页面帮助与问答

登录后打开 `/agent`。聊天保持原请求格式；响应增加 `knowledge_status/web_status/notices/miss_record_status`，证据增加 `kind/document_version/chapter/url`。`GET /api/v1/agent/knowledge/:id` 使用用户 JWT，只接受固定片段 ID。

#### 步骤 1 · 重建发布知识资产

在仓库根目录执行，修改来源后必须重建；不要将运行日志或用户原问题写到来源清单。

```powershell
$env:PYTHONUTF8 = '1'
.venv/Scripts/python.exe stages/07-agent/scripts/build-knowledge.py
stages/07-agent/scripts/test-offline.ps1
```

索引入 Git，服务启动时验证集合哈希。六个业务页面的帮助位于前端 `lib/page-help.ts`，与使用手册同步；模型不可用时帮助仍可阅读。

#### SUN2000-100KTL-M2 仿真参考手册

`INV-1001`、`INV-1002`、`INV-1003` 的仿真参考型号为 `Huawei SUN2000-100KTL-M2`，厂商身份仍是 `synthetic`。原创适用性与章节摘要在 `设备知识/SUN2000-100KTL-M2-仿真参考.md`，公开版本与 SHA-256 记录在 `设备知识/手册来源清单.json`。华为原始 PDF 仅保存在本机 Git 忽略目录 `artifacts/stage7-智能体/private/manuals/`；服务器先上传到私有临时位置，再以 `stages/07-agent/deploy/install-reference-manual.sh <上传后的 PDF>` 验证并安装到 `/opt/powersystem/runtime/manuals/`，目录权限 700，原件权限 600。现有加密备份会将该服务器私有目录和来源清单一并纳入，隔离恢复时重新核对哈希。部署新知识索引前保留上一版索引与来源清单以便回退；不把 PDF 加入发布包或 Git。

设备表增加字段后须重启长期运行的 `gateway`，使其重新建立 PostgreSQL 查询计划；发布脚本已执行此步骤。若发布期间发现 `telemetry_inbox` 未处理行持续增加、TDengine 最新时间停止前进，先确认网关恢复写入，再运行 `python3 stages/07-agent/deploy/reconcile-telemetry-inbox.py` 预览待回放范围；核对后使用 `--execute --max-id <预览输出的 max_id>` 将三台仿真设备收件箱中的原始样本幂等回放到 TDengine，待 API 处理完后再次检查未处理行和最新值时间。该工具不修改真实设备数据，也不直接改变告警或预测结果。

手册的指示灯、通信、保护、原生故障码均未由模拟器生成。第 7.4 节指向单独的《逆变器 告警参考》，本批未收录该文档。智能体可解释本批已核对章节；问三台设备当前状态必须查询业务工具。无可核验来源时只给明确标注的假设，页面显示按句统计的陈述中带来源的比例，该比例不是正确率，也不表示来源必然支持模型的每个措辞。真实设备接入时按实际完整型号、硬件和软件版本重新建立文档关联。

#### 步骤 2 · 可选搜索配置

`AGENT_SEARCH_PROVIDER=generic` 保持原 POST `{query,max_results:5}` 与 `{results:[{title,url,summary,published_at?}]}` 协议。选择 `bocha` 时固定调用 `https://api.bocha.cn/v1/web-search`，使用 Bearer 私有密钥及 `{query,count:5,summary:true,freshness:"noLimit"}`。`AGENT_SEARCH_URL` 留空或填该官方地址，其他地址拒绝启动；`AGENT_SEARCH_KEY_FILE` 指向忽略目录中的密钥文件。服务器使用 `/etc/powersystem/agent-search-key`，确保 Agent 用户可通过 secret 读取，文件不对其他普通用户开放。

博查从 `data.webPages.value` 提取 `name/url/summary/datePublished`，摘要缺失回退 `snippet`，不使用 `dateLastCrawled`。严格保留链接过滤，带查询参数的来源不尝试修复；去重后最多五条。HTTP 与业务码均校验，明确区分认证、余额、限流、超时、无结果及响应异常；全过程五秒、不重试、响应最多 64 KiB。搜索故障不会修改模型暂停状态。

真实联调先按本页 V2-M2 步骤加载现有私有模型配置，再设置以下非敏感变量并运行；不会启动本机业务服务或创建业务设备：

```powershell
$env:AGENT_SEARCH_PROVIDER = 'bocha'
$env:AGENT_SEARCH_URL = 'https://api.bocha.cn/v1/web-search'
$env:AGENT_SEARCH_KEY_FILE = Join-Path (Get-Location) 'artifacts/stage7-智能体/private/agent-search-key'
& $Stage7Python stages/07-agent/scripts/smoke-bocha.py --output artifacts/stage7-智能体/v2m3/<新批次>/local-real
```

每批五题顺序执行，成功与失败目录独立保留。脚本仍使用原 `validate_answer`；失败不自动重试、不放宽校验。服务器发布追加搜索 overlay，仅构建 Agent 与前端，实际回退恢复后再开始三十分钟观察。

服务器博查发布、验收与实际回退的维护入口如下（发布包须先按明确清单审核并记录 SHA256；不包含私有文件）：

```bash
bash stages/07-agent/deploy/bocha-release.sh /opt/powersystem/runtime/bocha-release.tar.gz <SHA256>
python3 stages/07-agent/deploy/acceptance-bocha.py --output runtime/<本次博查发布>/server-real
python3 stages/07-agent/deploy/acceptance-v2m3.py --search-configured --output runtime/<本次博查发布>/regression
bash stages/07-agent/deploy/verify-bocha-release.sh /opt/powersystem/runtime/<本次博查发布>
```

五题脚本将协议检查写为 `protocol_passed`，并生成 `source-review-template.json`。逐条打开来源核对每条结论和建议后，人工填写每项 `supported=true/false`、`review_note` 和 `reviewer`，保留原始模板及失败批次，再运行 `python3 stages/07-agent/deploy/finalize-bocha-review.py --run runtime/<本次博查发布>/server-real --review <人工审查文件>`。脚本按回答与引用来源摘要的 SHA256 核对，任何未审查、来源变更或协议失败均不能正式通过。原数字、概率、阈值与引用校验继续执行；以上为 2026-10-03 历史专业审核口径；2026-10-04 本轮按所有者授权的开发阶段暂缓专业审核口径执行，见第 9 节。

当前状态类问题会要求对应业务工具证据并在结论中引用；知识题的三十二题集同时通过检索器和实际编排入口。服务器混合题在 `acceptance-v2m3.py` 中必须回答，并同时引用预测与文档证据。原 49/49 属于旧断言批次，不能代表这些新增检查已通过。

入口安全补强的前一发布目录为 `runtime/route-fix-20261003T111105Z/`，只重建 Agent；该目录保留更新前源码、镜像及回退文件。当时本机 Agent 测试 67/67，服务器七题严格回归 67/67，实时告警专项 16/16。需要单题定位时，`acceptance-v2m3.py --case 4` 只运行混合题，`--case 6` 只运行实时告警题；输出仍保留独立批次。当时联网五题仅 2/5 协议通过，另三题的具体工程操作建议被护栏拒绝。历史三十分钟观察属于更早的博查 Agent 镜像。

**历史批次（2026-10-03）**：上述 `111105Z` 是前一补丁批次。回答文本实时断言补强后的该日发布目录为 `runtime/route-fix-20261003T124153Z/`：本机 Agent **68/68**，服务器七题严格回归本批 **65/65**，五题联网 **1/5 协议通过**，四题具体工程操作建议被护栏拒绝。实际源码与镜像回退恢复均健康；当时 Agent 镜像 `0be751696661…`。该批的新观察、隔离恢复和人工来源审查尚未取得，不把前批结果算入该批签收。

`verify-bocha-release.sh` 保留当前镜像，实际切回第三阶段无联网镜像及私有配置，再恢复博查镜像；失败时通过 trap 恢复，不清理知识缺口。它还使用明确无效的测试密钥调用搜索接口，确认认证失败未改变 Redis 模型暂停状态。恢复完成后再执行本页观察命令；加密备份和隔离恢复使用原统一部署脚本，不新增端口。注意 `.env` 回退基线属于私有材料，权限 600。


新环境只在已取得接口文档与凭据后追加 `deploy/search.compose.yaml`。秘密文件放私有目录，不进入命令参数或浏览器。2026-10-03 早期批次曾仅证明供应商聊天鉴权，不能据此签收真实搜索；后续真实博查五题的最终技术结果见 V2-M3/V2-M4 正式交付页，专业来源审核仍待完成。

#### 步骤 3 · 汇总与审核知识缺口

**作者本机只读入口**：双击 Git 忽略目录 `artifacts/stage7-智能体/private/查看远程知识缺口.cmd`。它通过已有 SSH 私钥和严格主机密钥校验读取 R730xd 运行中 Agent 的 `/knowledge-misses` 挂载，自动打开 `artifacts/stage7-智能体/private/reports/knowledge-misses.html`。每次重新读取并覆盖报告；连接或解析失败时删除旧报告并显示错误。报告可搜索、筛选审核状态、按问题去重，展开查看逐条记录。入口、报告和原始问题都不得提交或分享；本地报告可能含脱敏规则未识别的自由文本信息。能够使用本机账号与已授权私钥的人也能使用该入口，脚本不提供额外身份验证。

仓库通用脚本 `scripts/view_remote_knowledge_misses.py` 不含服务器地址或私钥，需显式提供 `--host`、`--port`、`--user` 和 `--identity`；`--no-open` 只生成报告，不打开浏览器。它仅读取当前 Agent 的宿主挂载与 JSONL，不修改服务器记录、审核状态或服务。新电脑需要在自己的 Git 忽略目录建立专用入口，不复制作者凭据。此入口用于查看，不代替下列离线审核命令。

Windows 64 位收件人可使用忽略目录 `artifacts/stage7-智能体/distribution/PowerSystem-Knowledge-Misses-share.zip` 中的单文件程序。双击时按提示填入自己获授权的 SSH 地址、端口、用户名及本机私钥；打包版把报告写入当前用户的 `%LOCALAPPDATA%/PowerSystem/knowledge-misses/`。程序不含作者连接参数或私钥，仍需收件人具备服务器授权、已核验主机密钥、Windows OpenSSH、远端 Python3 和 Docker 读取权限。分发包仅含 `.exe` 与使用说明，构建缓存、私有入口和报告均不分享；产物位置见 [[运行产物说明]]。

```powershell
.venv/Scripts/python.exe stages/07-agent/scripts/knowledge-misses.py artifacts/stage7-智能体/v2m3/knowledge-misses --output artifacts/stage7-智能体/v2m3/knowledge-summary.json
```

审核某条时使用 `--review-id <汇总中的ID> --status documented --note <人工说明>`；状态为 `pending/documented/dismissed`。原 JSONL 不修改。`--cleanup` 只清理指定目录内命名匹配且超过九十天的日志，审核状态保留；服务每次写入也执行保留期清理。

#### 步骤 4 · 服务器增量发布与验收

`deploy/v2m3-release.sh <受审发布包> <SHA256>` 先保存源码与镜像基线，再构建并增量启动 API、前端和 Agent；默认项目目录遵循既有服务器约定。知识缺口宿主目录可通过 `AGENT_MISSES_HOST_DIR` 指定，属主 10001、权限 700。发布前先运行统一加密异地备份。

```bash
python3 stages/07-agent/deploy/acceptance-v2m3.py --output runtime/<本次发布目录>/acceptance
python3 stages/07-agent/deploy/observe.py --url https://<管理员提供的入口> --seconds 1800 --output runtime/<本次发布目录>/observation.json
```

停止 Agent 的受控故障检查增加 `--fault-check`，检查结束立即按既有 Compose 恢复；不得留下停止状态。观察是只读采样，不调用模型。缺口记录、索引和可选搜索密钥进入统一加密备份；隔离恢复核对索引哈希和 JSONL。对任一新候选而言，没有真实搜索五例及完整服务器证据时不得签收。

## 9. 人工设备操作建议与本轮签收（2026-10-04）

设备操作建议可以展示，程序添加 `equipment_operation`，两处界面默认醒目提醒并逐条展开来源。模型输出格式、原数字及引用校验、六工具白名单保持；专业审核暂缓不等于已补库或真实机型适用。

#### 步骤 1 · 本机校验

Windows 使用已有依赖并显式 UTF-8：

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) "artifacts/stage7-智能体/pydeps") + ";" + (Join-Path (Get-Location) "stages/07-agent")
python -X utf8 -m unittest discover -s stages/07-agent/tests -v
python -X utf8 stages/07-agent/scripts/build-knowledge.py
```

#### 步骤 2 · 本轮真实联网与开发阶段来源签收

先完成五题实际回答、引用与持久留档，再显式记录本阶段专业审核暂缓；人工审核入口继续保留。

```bash
python3 stages/07-agent/deploy/acceptance-bocha.py --output runtime/<本轮发布>/server-real
python3 stages/07-agent/deploy/finalize-bocha-review.py --run runtime/<本轮发布>/server-real --mode development-assumed --authorization "项目所有者明确授权：开发阶段来源审核默认通过，实际设备手册和专业工程师校对后补"
```

`source-review-deferred.json` 保存逐条内容哈希、授权与 pending 状态；汇总签收为 `assumed_pass_development`，不会写成工程师已审核。知识缺口按原步骤汇总，新增来源、联网状态、关联请求与补库待办。

#### 步骤 3 · 部署、回退与收尾

使用 `deploy/manual-advice-release.sh <受审发布包> <SHA256>` 增量部署，`deploy/manual-advice-verify.sh <本轮发布目录>` 执行 Agent 停止故障检查及本轮源码／镜像回退与恢复。原统一脚本完成更新前与最终加密备份、隔离恢复，最后执行本页三十分钟观察命令。V2-M3 只在本轮实际门槛通过后签收，最终 Release 归 V2-M4。

## 10. 历史固定候选与状态（2026-10-04）

最终本轮候选运行目录为 `runtime/manual-advice-20261004T143910Z/`。本机 Agent 71/71、前端 13/13、类型与构建、Go 全量通过；服务器完整七题 73/73、五题 5/5、三类原事件快照、实际源码镜像回退、最终加密备份及隔离恢复通过。保守识别仅对完整明确的资料短句免操作提醒，混合及未知措辞默认提醒。

该历史候选的来源专业审核按所有者授权暂缓，知识补库仍待人工整理和专业校对。固定镜像观察已完成（1800.52 秒、60 次采样全部通过，重启为零），当时实际浏览器仍待已有账号登录，因此该候选未正式签收。该批门槛、失败历史与镜像全文见第二版阶段三测试与交付页第十节；当前正式结论见第十一节，第二版最终 Release 留到 V2-M4。


## 11. 当天资料 JSON 修正与页面验收（2026-10-05）

当天候选 `runtime/manual-advice-20261005T004409Z/` 仅补强资料回答的简短纯 JSON 提示，原 1500 输出预算与所有护栏保留。本机 Agent 71/71；服务器严格七题 60/60、原五题 5/5、三类原事件、实际回退通过；已有账号实际浏览器五题、两处面板、六类指南、章节阅读、取消和新会话通过。当天最终加密备份、隔离恢复及独立三十分钟观察也已完成，阶段三测试交付页第十一节为正式结论。来源专业审核暂缓与待补库状态保留。


### 第二版阶段三正式签收（2026-10-05）

V2-M3 合成演示与公开资料辅助建议范围技术验收通过，可以进入 V2-M4；专业来源审核按所有者授权暂缓，真实设备适用性尚未验证。

最终候选 `runtime/manual-advice-20261005T004409Z/`：本机 Agent 71/71，前端 13/13、类型／构建及 Go 全量通过；服务器严格七题 60/60、原五题联网 5/5，实际浏览器完整五题及两处面板、六类指南、来源阅读、取消与新会话通过。实际回退、最终加密备份与隔离恢复通过；独立观察 1800.52 秒、60 次采样全部通过，业务服务健康且重启为零。来源与待补知识不虚报为专业已审核。证据见 [[第二版-智能体升级/开发阶段3-使用引导与值班问答/05-V2-M3测试与交付#十一、当天最终候选与真实页面验收（2026-10-05）]]。


## 12. V2-M4 整体测试与发布

阶段索引见[测试、安全复核与交付](../../第二版-智能体升级/开发阶段4-测试、安全复核与交付/00-索引·测试、安全复核与交付.md)，正式结论见[测试与交付](../../第二版-智能体升级/开发阶段4-测试、安全复核与交付/05-V2-M4测试与交付.md)。V2-M4 在合成演示与公开资料辅助建议范围正式技术签收，发布版本 v2.0.0；专业审核及真实设备适用性保持待办。最终十五项账本位于本轮 `final-delivery/`；缺失页面的旧账本保留。

### 步骤1 · 本机基础套件

使用已有 Python 和 Node24，不升级锁文件。`--node`指定Node24可执行文件，`--npm-cli`指定现有npm入口。套件创建仅绑定回环16379的临时Redis，结束时仅停止自身容器。已有Redis、数据库和历史卷保留。

```powershell
python -X utf8 stages/07-agent/scripts/v2m4-suite.py --output artifacts/stage7-智能体/v2m4/<运行编号>/local-suite --node <Node24路径> --npm-cli <npm-cli.js路径>
```

### 步骤2 · 本机真实模型与隔离测试

旧私有配置若仍指向迁移前目录，为本轮创建独立配置，明确指定 `AGENT_PYTHON_EXECUTABLE`；不覆盖原密钥。所有测试设备、告警和规则带本轮编号，业务通知关闭。

```powershell
stages/07-agent/scripts/start-local-backend.ps1 -ConfigFile <本轮私有配置>
stages/07-agent/scripts/start-agent.ps1 -ConfigFile <本轮私有配置>
python -X utf8 stages/07-agent/scripts/smoke-v2m2.py --real --groups 10 --output artifacts/stage7-智能体/v2m4/<运行编号>/real-interpretations
```

故障脚本 `fault-v2m2.py deposit|verify --output <本轮目录>` 的前后两步使用同一目录。阶段签收仍须完成主链路专项、来源抽查、服务器和页面门槛。

主链路专项使用现有 PowerShell 7，停止后实测 Agent 不可达，恢复后实测健康；不能只根据命令退出码认定故障。占满测试仅为隔离环境的 Go 接纳池设置四个本轮租约，验证429及告警链路，不计为四次真实模型调用。

```powershell
python -X utf8 stages/07-agent/scripts/main-chain-v2m4.py --config <本轮私有配置> --output artifacts/stage7-智能体/v2m4/<运行编号>/main-chain
python -X utf8 stages/07-agent/scripts/performance-v2m4.py --interpretations <真实解读批次/interpretations.json> --audit <本轮审计目录> --replay --output artifacts/stage7-智能体/v2m4/<运行编号>/performance.json
python -X utf8 stages/07-agent/scripts/source-access-v2m4.py --review <最终联网批次/source-review-template.json> --output artifacts/stage7-智能体/v2m4/<运行编号>/source-access.json
python -X utf8 stages/07-agent/scripts/v2m4-audit.py --config .env <本轮私有配置> --output artifacts/stage7-智能体/v2m4/<运行编号>/credential-scan.json
python -X utf8 stages/07-agent/scripts/v2m4-package.py --commit <候选提交> --base <前三阶段提交> --output artifacts/stage7-智能体/v2m4/<运行编号>/package
```

### 步骤3 · 受审发布与实际回退

固定候选提交后打包公开文件，先扫描秘密及链接，再沿用既有目标完成更新前加密备份和云端摘要核对。服务器没有Git元数据，核对文件清单及镜像标签。

```bash
export POWERSYSTEM_PUBLIC_URL=https://<管理员提供的入口>
bash stages/07-agent/deploy/v2m4-release.sh <受审包> <SHA256>
bash stages/07-agent/deploy/v2m4-verify.sh /opt/powersystem/runtime/v2m4-<本轮编号>
```

本轮服务器测试与匿名安全检查统一指定输出目录。事件脚本最多安排七个通知事件；首次失败于监控接纳之前时，`--resume-state <原批次/state.json>`核对归属并复用原两条业务告警，不再次触发，原失败证据保留。已接纳监控的批次不自动续跑，避免重复通知。

```bash
python3 stages/07-agent/deploy/limits-v2m4.py --url "$POWERSYSTEM_PUBLIC_URL" --output runtime/v2m4-<本轮编号>/limits
python3 stages/07-agent/deploy/acceptance-v2m3.py --search-configured --url "$POWERSYSTEM_PUBLIC_URL" --output runtime/v2m4-<本轮编号>/chat
python3 stages/07-agent/deploy/acceptance-bocha.py --output runtime/v2m4-<本轮编号>/search
python3 stages/07-agent/deploy/finalize-bocha-review.py --run runtime/v2m4-<本轮编号>/search --mode development-assumed --authorization <所有者开发验收授权依据>
python3 stages/07-agent/deploy/events-v2m4.py --url "$POWERSYSTEM_PUBLIC_URL" --output runtime/v2m4-<本轮编号>/events
python3 stages/07-agent/deploy/inventory-v2m4.py --output runtime/v2m4-<本轮编号>/inventory.json
python3 stages/07-agent/deploy/security-v2m4.py --url "$POWERSYSTEM_PUBLIC_URL" --output runtime/v2m4-<本轮编号>/public-security
```

限额验收后等待滚动窗口恢复再跑真实问答，不能靠新会话绕过额度。TCP接连异常必须结合独立外部视角、协议与主机映射复核；共享端口不据端口号推断项目暴露。扫描仅输出位置和计数，不打印凭据原文。双链示例和代码中的`[[proxies]]`不作为缺失笔记。

性能报告分开记录事件等待至快照采集、Agent审计处理时间与九例保存快照的独立真实模型重放；模型重放不替代原事件处理时间，也不证明浏览器渲染。来源可达性只检查HTTPS状态，每次重定向重新核对公共DNS；本机代理返回非公共DNS时记为未验证，转独立服务器视角，保留原批次。HTTP200只证明可达，专业审查仍为pending。

### 步骤4 · 完整观察与证据汇总

在最终代码、配置、知识和镜像固定后执行。输出路径必须未存在，不复用中断批次。

```bash
python3 stages/07-agent/deploy/observe.py --url "$POWERSYSTEM_PUBLIC_URL" --seconds 1800 --images runtime/v2m4-<本轮编号>/current-images.json --output runtime/v2m4-<本轮编号>/observation-final.json
```

`v2m4-evidence.py --run <本轮目录> --gate <门槛> --result passed|failed|interrupted|skipped --evidence <文件...>`登记本轮文件SHA256；不带`--gate`核对所有门槛，缺项或摘要变化退出非零。真实来源的专业审核仍为pending，不因技术签收自动补库。

最后仅修改测试脚本和状态文档时，可把最终固定提交重新打包，使用下面入口同步公开源码。它先核对包与所有文件摘要，再对比本轮运行候选manifest；运行资产变更、服务器运行文件漂移或非批准路径均拒绝，不重启服务。旧受审包、基线和回退证据保留，最终文件摘要另存。

```bash
python3 stages/07-agent/deploy/sync-v2m4-source.py --root /opt/powersystem --archive <最终公开包> --manifest <candidate.json> --runtime-manifest runtime/v2m4-<本轮编号>/manifest.json --output runtime/v2m4-<本轮编号>/final-source-proof.json
```
