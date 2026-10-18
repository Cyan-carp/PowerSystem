---
tags: [源网智联, Agent, RAG, 源码运行]
---

# 阶段8 通用 Agent 与向量 RAG

> 2026-10-14当前：SEC-M4本机限定签收及r6追加准入通过；MA-C32～C35和最终文档门槛已完成，MA-M4及联合阶段四在声明范围正式技术签收。服务器r6/v5、恢复维护修订3；本机唯一批准动作保留，写窗口关闭，生产仅登记R0。

> 2026-10-13历史路线：MA-M1～M3及SEC-M1既有签收保留；SEC-M2本机限定技术验收签收、窗口已关闭并实测阻断，可进入SEC-M3开发准备但未自动启动。安全前三阶段后先SEC-M4本地集成，再评估MA-M4发布。服务器保持既有发布。最新真相入口：[[notes/01-项目调研规划与开发建设记录/第二版-智能体升级/多智能体协作建设/05-安全审查模型/05-功能开发与阶段交付/02-强制门禁集成/03-SEC-M2测试与交付]]。

2026-10-11 开始重建。新问答独立于阶段7旧规划器与回答审核器，使用 DeepSeek 自主调用知识库、博查及平台只读工具。首次自由版本允许普通技术和生活直接回答；BND-M2在本地及指定公网增加日常入口边界，明确生活/闲聊不进入模型，普通技术继续开放。指定公网当前发布为BND-M2。正文仍为Markdown，不恢复固定分栏或逐条审核。当前验收状态见阶段笔记。

## 架构与目录

FastAPI 接收 Go 代理注入的当前用户身份；LangGraph 组织模型与工具循环；Qdrant 保存本地 E5 向量；独立 PostgreSQL 数据库持久保存会话；Redis 保护请求与会话并发。阶段7继续承载历史自动告警解读及页面帮助来源，不导入其问答规则。

## 本地运行

运行配置保存在被忽略的 `artifacts/stage8-agent-rag/<运行编号>/private/local.env`，模板见 `.env.example`。先安装 `requirements.txt`，启动 `compose.yaml` 的 Qdrant，再运行资料导入及服务脚本。

```powershell
docker compose -f stages/08-agent-rag/compose.yaml up -d
stages/08-agent-rag/scripts/start-local.ps1 -ConfigFile artifacts/stage8-agent-rag/rebuild-20261011/private/local.env
```

服务监听本地8093。Go 的 `AGENT_CHAT_URL` 指向它；原 `AGENT_URL` 继续指向8092的旧事件解读服务。清空 `AGENT_CHAT_URL` 可恢复旧问答代理，但须配套恢复旧前端源码。

## 知识资料维护

`knowledge/sources.json` 是初始资料清单，不自动扫描仓库。个人 Word/PDF 等资料使用忽略目录内的独立清单，绝对路径只放私有清单。支持 Markdown、TXT、DOCX、文本型PDF；扫描PDF需要另外OCR，不伪装成导入成功。

```powershell
stages/08-agent-rag/scripts/import-knowledge.ps1 -ConfigFile artifacts/stage8-agent-rag/rebuild-20261011/private/local.env
```

导入器将资料按章节和段落拆分，最多400个模型token、重叠60token，使用本地 `intfloat/multilingual-e5-small` 和384维 Cosine 向量。默认查询返回5片，由回答模型判断是否有用。首次下载完成后向量化无需联网。

导入或增量更新都构建新集合，计数及查询校验成功后原子切换 `powersystem_rag_active` 别名，保留旧集合。未变化资料采用稳定内容ID；新集合重建避免旧片段残留。删除资料须先从清单移除或设 `disabled:true`，再执行导入。CLI `--delete-document` 可做单次排除；若不修改清单，下次重建会重新导入。`--dry-run` 仅输出提取报告，不修改向量库。

## 接口与持久化

- `POST /api/v1/agent/chat`：`message`、可选UUID `session_id`；返回Markdown答案、实际工具来源、调用记录和提示。
- `POST /api/v1/agent/sessions/:id/verify`：`parent_turn_id`、`device_id`、`checks`、UUID `verification_id`；用户确认后只读核验，幂等追加记录，保留原回答。checks限定latest_telemetry/current_alarms/prediction，不调用模型。
- `GET /api/v1/agent/sessions?offset=0`：当前用户会话，分页50项。
- `GET /api/v1/agent/sessions/:id`：当前用户完整问答历史。
- `DELETE /api/v1/agent/sessions/:id`：删除本人会话及其问答，忙碌会话返回409。
- `GET /api/v1/agent/rag/sources/:id`：向量库章节详情。

请求身份由JWT认证的Go服务注入，Agent内网接口验证服务token。历史库表为 `rag_sessions`、`rag_turns`，自动应用幂等迁移 `deploy/001_sessions.sql`；与平台业务库分离。历史持续保存，删除只能由本人执行，不保存跨会话偏好。模型上下文只装入资源预算内的完整旧轮次，完整历史不截断。旧阶段7临时会话不迁移、不删除。

边界第一期增加`deploy/002_verification.sql`，保留旧行，新增kind、关联原轮次及会话内唯一核验UUID。聊天新增turn_id、verification_available和可选evidence。取证状态只统计本轮实际业务工具；时间直接取查询时间、ts_ms及received_at，预测使用stale。历史核验记录不能当作后续实时事实。

集中参数见`.env.example`：BND_EVIDENCE_ENABLED关闭取证展示，BND_VERIFY_ENABLED关闭新核验，已有记录仍可读；采样过期120秒、未来容差60秒、每种告警状态最多10页，同时受原120秒及16次工具调用上限保护。当前告警分页未完整取回时显式提示，不据此判定无告警。两项开关不关闭身份、密钥或只读保护。

## 参数与故障

资源上限集中在配置：单轮120秒、模型60秒、工具15秒、最多8轮/16工具调用、输出8192tokens、输入32000字符。Go代理125秒；接纳每分钟10次、同用户1并发、全站4并发，租约覆盖请求超时。工具错误交给模型继续分析；模型故障显示degraded，保留已取得的工具结果，不调用备用模型。来源只列实际工具结果，不做逐条语义审核。

密钥使用私有文件，输入、输出与日志脱敏；Markdown使用HTML清理渲染。平台工具只允许固定的GET接口。第三方内容作为资料而非系统指令。Qdrant仅绑定回环；数据库和向量数据不进入Git。

## 测试与回退

新服务测试位于 `tests/`，真实联调入口见 `scripts/smoke.py`。证据保存在 `artifacts/stage8-agent-rag/`。回退须恢复工作区基线源码和旧问答前端，并清空问答独立地址；保留会话数据库和Qdrant卷。不要使用Docker卷清理命令回退。

## BND-M1最终运行版本（2026-10-11）

最终R3已在https://8.138.42.212/agent发布；本机/最终Linux Agent32项、前端17项、Go全包、类型构建、公网23项、重启7项、实际基线回退与5分钟固定版本观察通过。基线及运行证据在runtime/bnd-m1-20261011；具体镜像摘要、失败批次和限制见[[notes/01-项目调研规划与开发建设记录/第二版-智能体升级/问答及调度业务主智能体/智能体边界建设/04-功能开发与阶段交付/01-取证提示与主动核验/03-阶段1测试与交付]]。文档同步不等于知识导入，当前向量集合保持原98片段/6份资料；知识更新另行执行导入/回归，不自动收录边界笔记。

## BND-M2日常问题边界（2026-10-11）

BND-M2已在R730XD验收发布，公网入口8.138.42.212。本机及最终Linux Agent98项、前端19项、类型构建和Go全包通过；最终公网11类/19项、重启历史10项、实际公网浏览器9项、源码/镜像实际回退、最终加密备份隔离恢复及300秒固定镜像观察通过。五页阶段二笔记已同步，未提交或推送Git。正式记录见[[notes/01-项目调研规划与开发建设记录/第二版-智能体升级/问答及调度业务主智能体/智能体边界建设/04-功能开发与阶段交付/02-日常问题调用边界/00-索引·日常问题调用边界]]、[[notes/01-项目调研规划与开发建设记录/第二版-智能体升级/问答及调度业务主智能体/智能体边界建设/04-功能开发与阶段交付/02-日常问题调用边界/03-阶段2测试与交付]]和[[notes/01-项目调研规划与开发建设记录/第二版-智能体升级/问答及调度业务主智能体/智能体边界建设/04-功能开发与阶段交付/02-日常问题调用边界/04-问题记录与解决方案·02-日常问题调用边界]]。

BND_DAILY_GATE_ENABLED默认true；false恢复原问答入口，历史保留。入口判断不使用Embedding或第二模型；被拦截的请求不会创建Engine任务。程序答复response_mode为preset/boundary、model_called=false、model为空；生活拦截out_of_scope、礼貌答复answered。模型实际尝试后model_called=true，即便故障。boundary记录动作、原因、子规则和版本。新增字段在response JSON，无新SQL迁移。

测试前使用独立本机测试数据库配置，不能将生产/普通历史库误作测试库；测试fixture会删除其自建会话。Windows pytest临时目录应指定本批可写目录，Vite/Go需要正常本机子进程权限。以下命令从仓库根运行：

```powershell
. stages/08-agent-rag/scripts/common.ps1
Set-RAGEnvironment artifacts/stage8-agent-rag/<批次>/private/test.env
& $RAGPython -X utf8 -m pytest stages/08-agent-rag/tests -q --basetemp=artifacts/stage8-agent-rag/<批次>/pytest-temp
```

验收既有本机或指定公网账号；配置沿用私有文件，不能输出密码或JWT。program-only仅提交固定/范围答复，先要求目标Agent开关开启，不执行模型、向量或搜索查询，并列出未执行项目：

```powershell
. stages/08-agent-rag/scripts/common.ps1
Set-RAGEnvironment artifacts/stage8-agent-rag/rebuild-20261011/private/local.env
& $RAGPython -X utf8 stages/08-agent-rag/scripts/accept-daily.py --program-only --output artifacts/stage8-agent-rag/boundary/<独立批次>/program
```

去掉program-only会执行真实模型、知识片段、博查和设备聊天，可能把内部内容提交到现有外部模型服务；本轮最初被审批拒绝；用户随后明确授权上述数据及目的地，已用独立批次完成11类/10项真实回归。后续复验仍须确认实际数据及外部发送范围，外部发送授权与服务器发布授权分别记录；本轮后续用户已明确授权同步R730XD。

实际Agent重启之后可运行只读历史核对，仍使用独立输出目录：

```powershell
& $RAGPython -X utf8 stages/08-agent-rag/scripts/accept-daily.py --program-only --history-file artifacts/stage8-agent-rag/boundary/<原批次>/program/history-before.json --output artifacts/stage8-agent-rag/boundary/<新批次>/restart
```

本期后续已执行服务器发布和源码/镜像完整回退；禁用开关的受控验证仍不能代替实际回退。阶段7事件解读及知识集合保持原有归属，边界笔记不自动导入。

## 多智能体最初建档历史（2026-10-12实施前）

规划入口：[[notes/01-项目调研规划与开发建设记录/第二版-智能体升级/多智能体协作建设/00-索引·多智能体协作建设]]。阶段8未来负责对话、RAG、联网和任务委派，平台GET由独立运维服务执行；这段记录描述2026-10-12实施前，随后本机平台GET已迁移，不能用作当前源码判定。主动核验将保留程序固定流程、原接口及幂等追加语义。当次建档时阶段9尚未创建；随后MA-M1～M3已实现并签收，当前状态见顶部。本轮仅文档，不导入知识或发布。

## MA-M1本机约定补充（历史）（2026-10-12，阶段二启动前）

> 下列为当次阶段补充；当前路线见总索引，签收历史见[[notes/01-项目调研规划与开发建设记录/第二版-智能体升级/多智能体协作建设/01-项目规划与需求/05-规划演进与历史记录]]。

当前Supervisor正常运行工具仅search_knowledge、web_search、dispatch_ops_task、get_ops_task；独立运维不可用时明确错误，不调用平台直查回退。MA身份签名、首次UUID、关联修复、固定核验和删除屏障配置与命令见阶段9README。旧fixture兼容测试不构成运行能力开放。

## 三智能体规划与Supervisor职责（2026-10-13）

本机正常Supervisor工具已为知识/联网/委派/任务查询，平台GET由阶段9执行。安全Agent尚未实现；未来仅通过程序触发必要审查，Supervisor不增加审批、自由动作或安全旁路工具。

普通聊天与固定主动核验保持既有行为，核验零模型。SEC-M2已同批加入默认空security_reviews及security_events，旧历史缺项兼容。MA-M4延期，SEC本地签收不自动更新公网或知识索引。

## 五分区目录兼容（2026-10-13本机修复）

knowledge/sources.json的path改为新手册/资产路径，title保留原逻辑文档标识；生产Compose的/documents挂载与清单对应。现有向量集合、来源内容与会话未重新导入，本次不调用knowledge.py构建或发布知识。

boundary-release.py prepare优先新规范/内容目录，也兼容尚未迁移的服务器；候选包包含scripts/knowledge更新时复制到镜像，旧增量包不要求携带未变文件。服务器runtime路径保持，不替换为本机资产/runtime归档。真实服务器未发布，MA-M4仍延期。

离线兼容检查（仅fixture、无Docker/SSH/模型调用）：

```powershell
python -X utf8 -m unittest discover -s stages/08-agent-rag/tests -p test_repository_layout.py -v
```

13项通过；测试只在artifacts/repository-layout-test-tmp创建并清理自身随机目录，Git与Docker调用模拟。需要Git Bash时使用本机已有安装。修复范围与实际部署限制见[[notes/04-系统部署发布备份与变更记录/部署与运维日志/02-目录重构兼容修复]]。

## SEC-M2本机集成

SEC-M2本机候选的Supervisor只从受鉴权model-summary取得受审摘要，发送前check许可与载荷；失败保留完整本人卡并程序提示。敏感报告不进入可重放model_messages，历史旧轮同样最小化，再次解释重新审查。普通聊天/RAG/BND及固定核验规则保持。

当前状态见[[notes/01-项目调研规划与开发建设记录/第二版-智能体升级/多智能体协作建设/05-安全审查模型/05-功能开发与阶段交付/02-强制门禁集成/03-SEC-M2测试与交付]]；本阶段统一运行命令见[阶段10源码README](../10-security-agent/README.md)。历史“尚未接入”段落指建设前状态，不能作当前源码判定。

## 2026-10-11本地交付记录

真实12项完整批次通过，新Agent16项、前端15项、Go全量与真实PG/Redis回归、旧Agent113项通过。实际旧入口回退后已恢复新入口；模型、向量和会话都留在忽略目录。

本地交付时，末轮浏览器附加复核受工具URL策略阻挡；已完成的页面截图、复制与多轮工具展开证据保留。自由生成模式仍可能出现阈值推断错误，不能将工具来源视为逐句核验；具体限制见新阶段4正式交付笔记。当时服务器发布尚未执行，后续服务器状态以独立正式交付页为准。

## 服务器部署

2026-10-11按用户指定的`https://8.138.42.212/agent`入口发布；业务仍在同一R730XD，另一台公网机不部署本轮代码。Linux采用Python3.12及CPU PyTorch，本地开发为Python3.13，完整依赖版本固定。生产叠加`deploy/production.compose.yaml`，私有环境、UID10002密钥副本、本地模型与独立审计目录均在Git之外，Qdrant不映射主机端口。

`deploy/switch-release.py`执行API/前端切换与回退，保留两代镜像和源码、新会话库及向量卷。平台`backup.sh`已增加RAG数据库、向量一致性快照与别名、Embedding模型及私有配置；`deploy/restore-rag.py`在隔离数据库与Qdrant中核对会话、向量和语义查询。真实公网验收脚本为`scripts/server-accept.py`，通过私有文件读取既有账号凭据，不输出密码或JWT。

发布过程及最终判定见[[notes/01-项目调研规划与开发建设记录/第二版-智能体升级/问答及调度业务主智能体/当前方案-Agent与RAG重建/03-功能开发与阶段交付/04-完整Agent与会话/05-RAG服务器发布测试与交付]]。

## 边界第一期运行与验收

功能测试包含`tests/test_boundary.py`；实际状态见[[notes/01-项目调研规划与开发建设记录/第二版-智能体升级/问答及调度业务主智能体/智能体边界建设/04-功能开发与阶段交付/01-取证提示与主动核验/03-阶段1测试与交付]]。边界笔记不在知识清单中，不自动导入。正文仍由模型自由生成，查询成功不等于全文已核验。

```powershell
. stages/08-agent-rag/scripts/common.ps1
Set-RAGEnvironment artifacts/stage8-agent-rag/rebuild-20261011/private/local.env
& $RAGPython -m pytest stages/08-agent-rag/tests -q
```

`scripts/accept-boundary.py`通过私有密码文件验收本地或指定公网接口，日志不输出凭据。`deploy/boundary-release.py`仅允许运行在R730XD，prepare保存当前源码、配置、镜像及会话库，build基于已有镜像离线构建，activate/rollback仅切换API/前端/通用Agent；数据库及向量卷保留。

```bash
python3 stages/08-agent-rag/deploy/boundary-release.py prepare --release /opt/powersystem/runtime/<独立运行编号>
python3 stages/08-agent-rag/deploy/boundary-release.py build --release /opt/powersystem/runtime/<独立运行编号> --revision=-r2
python3 stages/08-agent-rag/deploy/boundary-release.py activate --release /opt/powersystem/runtime/<独立运行编号>
python3 stages/08-agent-rag/deploy/boundary-release.py rollback --release /opt/powersystem/runtime/<独立运行编号>
```

prepare须在新运行目录执行，禁止覆盖旧基线；candidate.tar.gz放入该目录后才build。回退不删除新增会话与核验记录。完整模型身份、专业诊断和整机恢复不由本期查询展示证明。

## BND-M2指定公网验收与增量发布

accept-daily.py允许本机及本轮指定的HTTPS入口；公网模式须显式给出内网健康地址。服务器临时验收容器使用用户0读取权限600的只读密码文件，设置PYTHONPATH=/app；生产Agent仍使用原UID10002。只读密码文件不得下载、输出或放宽权限。完整模式仍会向既有模型与博查发送约定样例，需要实际数据及目的地授权；program-only列出未执行项目。

```bash
docker run --pull never --rm --user 0 -e PYTHONPATH=/app --network powersystem-stage4_default \
  -v /opt/powersystem/stages/08-agent-rag/scripts:/qa:ro \
  -v /opt/powersystem/runtime/<独立批次>/acceptance:/evidence \
  -v /etc/powersystem/demo-password:/run/secrets/demo:ro \
  -v /etc/powersystem/p0-admin-password:/run/secrets/admin:ro \
  --entrypoint python powersystem-bnd-agent-rag:<冻结标签> /qa/accept-daily.py \
  --base-url https://8.138.42.212 --username demo-operator \
  --password-file /run/secrets/demo --admin-password-file /run/secrets/admin \
  --gate-health-url http://agent-rag:8093/health --output /evidence
```

输出目录须预先建立。仅程序检查加--program-only；实际重启后另建目录，并加--history-file指向只读挂载的原history-before.json。最终公网完整11类/19项；程序检查7类/15项；重启历史10项。脚本新增BND-M1幂等、原回答保留及RAG来源读取检查，并验证未认证、注册、内部接口和跨用户隔离。

boundary-release.py的候选标签为<独立发布目录名><revision>，基线标签为baseline-<独立发布目录名>；prepare保存当前实际compose环境及候选覆盖的公开文档/源码，不能在旧基线目录重新prepare。backup.sh沿实际RAG_ENV_FILE备份Agent配置。最终运行代码包保持冻结，交付文档使用独立摘要包同步，不触发知识导入。

## 联合阶段四与原边界兼容

Supervisor保持聊天/澄清/知识和联网，对平台查询和基础设施需求委派Operation；必要Security独立审查及程序硬门禁约束执行。固定主动核验仍零模型，BND历史时间/所有权规则保持。当前r6/v5配套服务器结果见[[notes/01-项目调研规划与开发建设记录/第二版-智能体升级/多智能体协作建设/06-联合发布验收/02-服务器发布与恢复验收/03-MA-M4测试与交付]]；原BND-M2是回退基线和历史签收，不再作为当前在线候选名称。未导入新笔记或新增规则编号。

## 2026-10-16 · 严格取证回答可读性

[[notes/01-项目调研规划与开发建设记录/第二版-智能体升级/问答及调度业务主智能体/当前方案-Agent与RAG重建/04-增量优化与补充交付/01-回答可读性与任务结果展示/00-索引·回答可读性与任务结果展示]]维护问题、展示模板与边界；实现仅在阶段四前端，普通问答Markdown保持。程序事实来自实际步骤返回，模型结论分段、研判和建议编号，允许类审查默认折叠、异常项突出，历史回执与原文保留。实际安装及发布证据见[[notes/01-项目调研规划与开发建设记录/第二版-智能体升级/问答及调度业务主智能体/当前方案-Agent与RAG重建/04-增量优化与补充交付/01-回答可读性与任务结果展示/03-测试与交付]]，不为本次展示修改模型提示词或重复运行原业务测试。

## 相关笔记

- [[notes/01-项目调研规划与开发建设记录/第二版-智能体升级/问答及调度业务主智能体/当前方案-Agent与RAG重建/00-索引·Agent与RAG重建]] · [[notes/02-工程规范边界与验收治理/项目工程规范]]
- [[notes/01-项目调研规划与开发建设记录/第二版-智能体升级/问答及调度业务主智能体/废案-旧智能体方案/00-废案声明与弃用原因]]
- [[notes/01-项目调研规划与开发建设记录/第二版-智能体升级/问答及调度业务主智能体/智能体边界建设/00-索引·智能体边界建设]]：独立边界阶段记录与实际验收状态。

## 2026-10-15 API故障恢复与现行收尾

17:33基础编排重建导致API版本回退及助手配置遗漏；本次按已签收r6镜像恢复API并加载阶段7/8/9完整配置，保留当前前端、通知与模拟数据。两公网入口的会话/任务列表及详情、核心只读业务、HTTPS/WSS和一次技术模型问答已复验。完整恢复、备份及观察证据见[[notes/04-系统部署发布备份与变更记录/部署与运维日志/06-API版本回退恢复记录]]；现行待办按[[notes/04-系统部署发布备份与变更记录/部署与运维日志/09-项目收尾核对]]核对，历史阶段未勾选项不覆盖后来签收。
