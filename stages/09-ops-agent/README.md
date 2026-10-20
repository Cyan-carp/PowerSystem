---
tags: [源网智联, 多智能体, 源码运行]
---

# 阶段9 · 双智能体、基础设施诊断与受控演练

> 2026-10-19当前生产：运维与安全审查型号均为 `deepseek-v4.1-flash`，策略 `sec-m4-policy/v6`、许可 `sec-m4-project-readonly/v6`，接口及各自密钥保持。生产只读，写窗口关闭；旧许可和任务不升级，新任务使用新配置。门禁、设备查询和观察通过；基础设施完整任务仍有能力不可用及超时失败，正式事实见 [模型切换发布记录](../../notes/04-系统部署发布备份与变更记录/部署与运维日志/22-运维与安全审查模型切换.md)。下文旧型号及各阶段批次保留为历史范围。

> 2026-10-14当前：SEC-M4本机限定签收及r6追加准入通过；MA-C32～C35和最终文档门槛已完成，MA-M4及联合阶段四在声明范围正式技术签收。服务器r6/v5、恢复维护修订3；本机唯一批准动作保留，写窗口关闭，生产仅登记R0。

> 2026-10-13当前路线：MA-M1～M3及SEC-M1既有签收保留；SEC-M2本机限定技术验收签收、窗口已关闭并实测阻断，可进入SEC-M3开发准备但未自动启动。安全前三阶段后先SEC-M4本地集成，再评估MA-M4发布。服务器保持既有发布。最新真相入口：[[notes/01-项目调研规划与开发建设记录/第二版-智能体升级/多智能体协作建设/05-安全审查模型/05-功能开发与阶段交付/02-强制门禁集成/03-SEC-M2测试与交付]]。

> MA-T01～MA-T16源码入口。MA-M1平台GET闭环保留；MA-M2新增独立无模型适配服务及受限远端采集。运维模型进程不持SSH、数据库诊断凭据或Docker socket。当前正式状态以[MA-M2测试与交付](../../notes/01-项目调研规划与开发建设记录/第二版-智能体升级/多智能体协作建设/04-基础运维模型/02-功能开发与阶段交付/02-基础设施只读诊断/03-MA-M2测试与交付.md)为MA-M2历史签收依据；MA-M3当前本机签收见下文。下文M1命令保留为历史环境入口。

## MA-M3签收入口（2026-10-12历史）

管理员审批、唯一R2演练重启、独立broker与防重已实现。108项阶段9、100项阶段8/BND、113项阶段7离线、Go全包及前端30项/类型/独立构建通过。真实双模型委派、当前Go身份、真实Docker与Chrome页面通过，**MA-M3本机技术验收通过**；当次工程判定可进入MA-M4，2026-10-13路线调整为延期等待SEC集成验收后重新评估。写窗口已关闭，默认不注册写工具。

正式事实：[MA-M3测试与交付](../../notes/01-项目调研规划与开发建设记录/第二版-智能体升级/多智能体协作建设/04-基础运维模型/02-功能开发与阶段交付/03-受控运维执行/03-MA-M3测试与交付.md)。完整五页：[索引](../../notes/01-项目调研规划与开发建设记录/第二版-智能体升级/多智能体协作建设/04-基础运维模型/02-功能开发与阶段交付/03-受控运维执行/00-索引·受控运维执行.md)。不提交/推送、不导入知识库，不更新服务器。

### 步骤1 · 准备独立批次

> 下列准备命令仅用于新本机演练批次；当前m3-20261012-r1已准备，不能重复setup或清空旧fixture。使用现有阶段9虚拟环境，从仓库根运行。只创建ma_m3专库/账号和新Compose项目；私有配置不输出值。

```powershell
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/09-ops-agent/scripts/prepare-m3.py --source artifacts/stage9-ops-agent/m2-20261012-r1/private/local.env --run artifacts/stage9-ops-agent/m3-20261012-r1
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/09-ops-agent/scripts/setup-m3-drill.py --run artifacts/stage9-ops-agent/m3-20261012-r1
go -C stages/02-backend build -o ../../artifacts/stage9-ops-agent/m3-20261012-r1/ma-local-api.exe ./cmd/ma-local-api
go -C stages/02-backend build -o ../../artifacts/stage9-ops-agent/m3-20261012-r1/ma-m3-fixture.exe ./cmd/ma-m3-fixture
```

在当前进程按下面方式加载local.env，再从stages/02-backend工作目录运行本轮ma-m3-fixture.exe。严格限ma_m3_business_test，只建立ma_m3_admin/operator和合成设备；不能从根目录运行相对迁移入口。

```powershell
foreach($maLine in Get-Content artifacts/stage9-ops-agent/m3-20261012-r1/private/local.env -Encoding utf8){if($maLine -match '^([A-Z][A-Z0-9_]*)=(.*)$'){Set-Item -Path "Env:$($Matches[1])" -Value $Matches[2]}}
```

### 步骤2 · 启动、导入及离线规划演练

> API、模型、适配均只监听回环；仅broker持Docker socket。演练容器无卷/业务凭据、非root、只读根、internal网络，固定完整ID。脚本只停止processes.json中PID/启动时间一致的本轮进程，新日志独立命名。

```powershell
stages/09-ops-agent/scripts/start-m3.ps1 -ConfigFile artifacts/stage9-ops-agent/m3-20261012-r1/private/local.env -Frontend -ProgramFixture
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/09-ops-agent/scripts/import-m3-assets.py --env artifacts/stage9-ops-agent/m3-20261012-r1/private/local.env --output artifacts/stage9-ops-agent/m3-20261012-r1/remote/import-NEW.json
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/09-ops-agent/scripts/accept-m3-program.py --env artifacts/stage9-ops-agent/m3-20261012-r1/private/local.env --output artifacts/stage9-ops-agent/m3-20261012-r1/program-NEW --label '本轮程序演练'
```

ProgramFixture显式标为fixture-m3-offline-planner，不访问外部模型。页面http://127.0.0.1:15177，Go18103、Supervisor18095、Ops8098、adapter8099、broker8097、认证Redis16377。登录仅本轮专用测试账号，密码在private文件；严禁打印。

浏览器明确批准/拒绝/取消后，读取程序证据：

```powershell
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/09-ops-agent/scripts/check-m3-program.py --env artifacts/stage9-ops-agent/m3-20261012-r1/private/local.env --seed artifacts/stage9-ops-agent/m3-20261012-r1/program-NEW/records.json --output artifacts/stage9-ops-agent/m3-20261012-r1/program-check-NEW --expected succeeded
```

拒绝或取消用--expected cancelled。每次output必须新目录。probe-m3-role.py仅暂变并恢复ma_m3_business_test里的ma_m3_admin角色，检查旧JWT/current operator403及can_decide=false，不得用作普通账号管理。

### 步骤3 · 软件回归和真实模型门槛

> 清理fixture的pytest只用ops-test.env，不能用local.env；独立RAG回归用rag-test.env，Go用business-test.env。新增审批表已加入专库清理清单。真实模型验收已在所有者明确授权范围内独立运行；后续批次仍需核对目的地及载荷授权，下述入口不能扩大授权。

```powershell
foreach($maLine in Get-Content artifacts/stage9-ops-agent/m3-20261012-r1/private/ops-test.env -Encoding utf8){if($maLine -match '^([A-Z][A-Z0-9_]*)=(.*)$'){Set-Item -Path "Env:$($Matches[1])" -Value $Matches[2]}}
$env:PYTHONPATH="$PWD/stages/09-ops-agent;$PWD/stages/08-agent-rag"
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 -m pytest stages/09-ops-agent/tests -q -p no:cacheprovider
```

本轮MA_DRILL_ACCEPTANCE_ONLY=true在程序上限制Supervisor只委派/查任务、Operation只读资产/容器安全快照及演练重启，禁止知识检索、联网和业务读取进入外发链路。该开关默认false，开启仅允许本轮ma_m3_rag_test/ma_m3_ops_test与Go18103；不能用于普通服务。真实模型为deepseek-v4.1-flash与deepseek-v4-flash-0731，网关https://hgapi.dieqiyun.top/v1，授权仅含本机演练需求和脱敏字段。real-prepare/execution-check/reject/cancel分别11/12/13/11项通过；program-*仍明确离线fixture。

真实模型脚本accept-m3.py提供--mode prepare/execute/reject/cancel和--task；使用local.env并启动非fixture Operation，仅在授权现有网关及载荷、重新限定演练窗口后运行。execute会通过程序审批接口批准一个演练任务；不能当只读命令使用。无阶段内批准授权时停在prepare。未过期pending可跨启动恢复，新审批JWT只在内存。

### 步骤4 · 关闭本轮写窗口

```powershell
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/09-ops-agent/scripts/close-m3-window.py --run artifacts/stage9-ops-agent/m3-20261012-r1
stages/09-ops-agent/scripts/start-m3.ps1 -ConfigFile artifacts/stage9-ops-agent/m3-20261012-r1/private/local.env -Frontend
```

broker每次写前读取窗口文件，立即拒绝新写；API重启后加载OPS_WRITE_ENABLED=false，健康返回write_tools_registered=false。默认配置与服务器写能力保持关闭。容器、SQLite防重卷、专库、历史和审计保留，不运行compose down -v或清理其他项目。

### 契约与不确定结果

004_approvals只用于ma_ops；平台业务库不增加审批表。独立ma-write-v1签名绑定审批、策略、目标、参数、步骤、call、epoch及截止；R0契约不变。重启宽限10秒、单步15秒；批准消费和尝试插入同事务，broker请求前SQLite持久防重。动作已消费后缺结果保守execution_uncertain，失败不可自动补执行；取消不代表回滚。

## MA-M2历史运行入口（当前服务器仍保持原发布）

本轮使用独立批次`artifacts/stage9-ops-agent/m2-20261012-r1/`，私有文件仅按路径引用。准备脚本严格限定ma_m2_*数据库及ma_m2_test账号；回归分别使用ops-test.env、rag-test.env、business-test.env，真实验收使用local.env，禁止混用。Redis回环16378：验收Ops/RAG为DB4/5，回归DB6/7，Go为DB8；前缀见私有配置。不要重复创建、清空或替换已有fixture。

```powershell
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/09-ops-agent/scripts/prepare-m2.py --source artifacts/stage9-ops-agent/m1-20261012-r1/private/local.env --run artifacts/stage9-ops-agent/m2-20261012-r1
go -C stages/02-backend build -o ../../artifacts/stage9-ops-agent/m2-20261012-r1/ma-local-api.exe ./cmd/ma-local-api
go -C stages/02-backend build -o ../../artifacts/stage9-ops-agent/m2-20261012-r1/ma-m2-fixture.exe ./cmd/ma-m2-fixture
```

fixture程序从`stages/02-backend/`运行，需先仅在当前进程加载local.env，确保POSTGRES_DB为ma_m2_business_test；加载文件时不得输出值。Redis首次准备/空fixture替换使用prepare-m2-redis.py的明确expected-id参数，只允许本轮已核验空对象；现有本轮fixture已准备，无需再运行。

```powershell
stages/09-ops-agent/scripts/start-m2.ps1 -ConfigFile artifacts/stage9-ops-agent/m2-20261012-r1/private/local.env -Frontend
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/09-ops-agent/scripts/setup-m2-collector.py --help
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/09-ops-agent/scripts/import-assets.py --env artifacts/stage9-ops-agent/m2-20261012-r1/private/local.env --output artifacts/stage9-ops-agent/m2-20261012-r1/remote/import-new.json
```

安装需已有本轮明确授权、固定主机密钥、离线Python3.11 wheels及私有文件；参数由脚本help维护。导入必须当前admin身份，目录已存在的证据不得覆盖。Ops8096、Supervisor18094、Go18082、前端15176及适配8095仅监听回环；启动脚本只操作本批次PID与启动时间相符的进程。

```powershell
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/09-ops-agent/scripts/accept-collector.py --config artifacts/stage9-ops-agent/m2-20261012-r1/private/executor.json --baseline artifacts/stage9-ops-agent/m2-20261012-r1/remote/preflight.json --output artifacts/stage9-ops-agent/m2-20261012-r1/tests/native-new
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/09-ops-agent/scripts/accept-m2.py --env artifacts/stage9-ops-agent/m2-20261012-r1/private/local.env --output artifacts/stage9-ops-agent/m2-20261012-r1/real-new
```

每轮固定源码及资产版本，失败保留，新建批次重跑。collector验收中的合成grant检验远端权限，不替代真实任务授权链；accept-m2走真实Go身份、双模型与一次领取。测试总入口仍为pytest本目录、阶段8/BND、阶段7、Go全包及前端test/typecheck，具体证据范围见正式页。

## MA-M2契约与撤销范围

2026-10-12签收后SSH专项：当前采集连接已改为`8.138.42.212:7070`，setup-m2-collector默认使用新云；accept-collector的独立管理员对照从同一私有transport读取目标，不再硬编码旧入口。主机公钥仍严格校验，资产身份/版本、采集凭据和模型服务没有变更。新云22/R730XD7070连接与出口来源维护命令统一见[独立云实施步骤](../../notes/04-系统部署发布备份与变更记录/独立云服务器部署/02-实施步骤.md)。原MA-M2证据保留，新增复验批次`artifacts/stage9-ops-agent/ssh-new-cloud-20261012-r1/`保存23项权限、真实主机诊断及连接/回退历史。

公开`ma/v1`兼容扩展asset_id/asset_version与默认空infrastructure_evidence；基础设施事实不计入平台业务已取证。`ops_contract.py`维护11项R0工具、日志安全事件投影及结果字段；慢查询返回capability_unavailable。执行grant为短期HMAC `ma-exec/v1`，绑定任务/代次/步骤/调用/工具/参数摘要/资产版本/截止时间，适配层回查Ops并核验当前Go身份一次领取。远端再核对象及版本，禁止自由Shell/SQL/URL/文件路径/环境覆盖。

运行库只执行001、002迁移；003_diagnostics.sql仅经授权在登记的业务PostgreSQL建立固定聚合函数。诊断角色不能读业务表、正文或终止会话；waiting是当前wait_event_type非空计数，含idle客户端等待，不等同锁阻塞。主机/容器指标为当前短样本，无基础设施历史查询能力。

远端新增对象及归属由私有安装记录维护：powersystem-ops-read身份、root所有的专用目录/固定入口/sudo规则/密钥，及本轮数据库角色与ma_ops_monitor函数。撤销先停止本轮适配入口并撤销本轮authorized_keys/sudo规则，再按安装归属移除专用目录/账号；数据库先撤销诊断登录与函数授权，再删除本轮函数/专用schema和角色（不得CASCADE到业务对象）。仅本轮对象可清理，不改原账号/业务镜像/服务。密钥或权限变更仍需明确授权，不能从阅读README推导执行权限。

阶段笔记：[索引](../../notes/01-项目调研规划与开发建设记录/第二版-智能体升级/多智能体协作建设/04-基础运维模型/02-功能开发与阶段交付/02-基础设施只读诊断/00-索引·基础设施只读诊断.md) · [设计](../../notes/01-项目调研规划与开发建设记录/第二版-智能体升级/多智能体协作建设/04-基础运维模型/02-功能开发与阶段交付/02-基础设施只读诊断/01-方案设计·02-基础设施只读诊断.md) · [实现](../../notes/01-项目调研规划与开发建设记录/第二版-智能体升级/多智能体协作建设/04-基础运维模型/02-功能开发与阶段交付/02-基础设施只读诊断/02-实现记录·02-基础设施只读诊断.md) · [交付](../../notes/01-项目调研规划与开发建设记录/第二版-智能体升级/多智能体协作建设/04-基础运维模型/02-功能开发与阶段交付/02-基础设施只读诊断/03-MA-M2测试与交付.md) · [问题](../../notes/01-项目调研规划与开发建设记录/第二版-智能体升级/多智能体协作建设/04-基础运维模型/02-功能开发与阶段交付/02-基础设施只读诊断/04-问题记录与解决方案·02-基础设施只读诊断.md)。

## 一、职责与身份

主智能体保留知识检索、联网、dispatch_ops_task和get_ops_task；平台GET只由本服务Executor调用。运维模型为用户确认的`deepseek-v4-flash-0731`，网关`https://hgapi.dieqiyun.top/v1`。原计划型号`deepseek-v4-flash`在该网关返回404，探测失败保留于私有批次；主智能体配置保持现状。

Go验证当前JWT与用户表，签署身份上下文；阶段8用另一条独立密钥签署信封。签名是`base64url(UTF-8规范JSON).HMAC-SHA256十六进制`，JSON键升序、无空白、中文不转义，固定kid `ma-v1`、aud `ma-ops`/`ma-envelope`、450秒有效期。身份包括user_id、role、session_id、trace_id、原请求UTF-8 SHA256。信封签名覆盖整个HTTP JSON，不依赖日期字段重序列化。私有头是X-PowerSystem-Token、X-MA-Actor、X-MA-Envelope和Authorization；均不存入数据库或模型消息。

首次会话UUID由Go生成，阶段8按ID建会话。所有任务读取、取消及绑定按本人归属，admin无例外。工具执行前再次向Go验证当前用户；固定目标与路径、禁止跳转。任务有严格ma/v1输入与ResultEnvelope输出，原始业务data字段保留可扩展结构。

## 二、环境与配置

依赖版本锁在requirements.txt；独立环境不安装Embedding。阶段8提供受限RAG检索，向量知识无需重复导入。配置模板为[.env.example](.env.example)。密钥仅通过文件路径引用，禁止终端打印私有配置。启用前需同时配置阶段8和Go的OPS_URL、OPS_SERVICE_TOKEN_FILE、MA_ACTOR_KEY_FILE；阶段8另需MA_ENVELOPE_KEY_FILE。

默认监听127.0.0.1:8094；OPS_ENABLED、OPS_WRITE_ENABLED关闭。MA-M1/M2历史代码即使开启也不注册写工具；MA-M3限定实现见本页当前入口。一个数据库仅允许一个服务进程，禁止uvicorn多worker。独立运行库ma_ops由非超级用户ma_ops_runtime拥有；验收、回归会话及业务fixture使用独立逻辑库，不能混用普通历史库。

预算默认任务300秒、模型60秒、工具15秒、8轮/16次调用、每用户1活动任务、全局2运行、等待20、租约50秒、心跳10秒。R0网络/5xx最多重试一次，每次占调用预算并单独存证。新任务与outbox同事务；Redis仅存协议与task_id；数据库锁和epoch负责容量、领取及删除竞态。

## 三、本机开发与验收命令

以下从仓库根目录运行；现有阶段8环境和本地基础依赖需已准备。配置准备脚本只创建指定的新逻辑库、账号和私有配置，不启动业务写链、导入知识或修改旧账号。ops-llm-key、ops-token、actor-key、envelope-key、ops-db-password应事先放于批次private目录，权限和授权由操作者确认。

```powershell
python -m venv artifacts/stage9-ops-agent/venv
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -m pip install -r stages/09-ops-agent/requirements.txt
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/09-ops-agent/scripts/prepare-local.py --source artifacts/stage8-agent-rag/rebuild-20261011/private/local.env --run artifacts/stage9-ops-agent/m1-20261012-r1
```

本轮配置生成local.env（只读验收开启）、ops-test.env、rag-test.env、business-test.env及runtime-disabled.env（ma_ops未启用）。重新运行不轮换已有密钥/密码，不清理数据库。

```powershell
go -C stages/02-backend build -o ../../artifacts/stage9-ops-agent/m1-20261012-r1/ma-local-api.exe ./cmd/ma-local-api
stages/09-ops-agent/scripts/start-acceptance.ps1 -ConfigFile artifacts/stage9-ops-agent/m1-20261012-r1/private/local.env -Frontend
```

隔离代理监听18081，主智能体18093，前端15173；代理不启动MQTT/预测/通知Worker，业务GET继续读已有合成平台。启动脚本仅重启本批次processes.json中ID和启动时间匹配的进程，并等待健康就绪。前端VITE_API_TARGET仅用于本机代理；当前WebSocket仍由已有业务API提供。

仅启动运维服务：

```powershell
stages/09-ops-agent/scripts/start-local.ps1 -ConfigFile artifacts/stage9-ops-agent/m1-20261012-r1/private/runtime-disabled.env
```

专用回归Redis使用127.0.0.1:16379，本轮自建容器ma-m1-regression-redis（redis:7.4.5-alpine），保留供后续测试；不能用已有业务Redis替代。首次独立环境可执行：

```powershell
docker run --detach --name ma-m1-regression-redis --publish 127.0.0.1:16379:6379 redis:7.4.5-alpine
```

执行测试前在当前进程加载相应私有env，不能打印env或将JWT传为命令参数。脚本使用测试专库，不使用local.env运行会清理fixture的pytest。

```powershell
foreach($line in Get-Content artifacts/stage9-ops-agent/m1-20261012-r1/private/ops-test.env -Encoding utf8){if($line -match '^([A-Z][A-Z0-9_]*)=(.*)$'){Set-Item -Path "Env:$($Matches[1])" -Value $Matches[2]}}
$env:PYTHONPATH="$PWD/stages/09-ops-agent;$PWD/stages/08-agent-rag"
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 -m pytest stages/09-ops-agent/tests -q
```

本机开发批次m1-20261012-r1保持29/100/113项、Go全包及真实双模型14项证据；收尾m1-closeout-20261012-r1核对64份后端源码未变，只重跑受影响前端24项、类型和独立构建。MA-C10真实Chrome原生可见性、秒数/HTTP日期Retry-After、非JSON429、实际连接重置、恢复及终态已通过，最终交付审计通过，MA-M1本机技术验收已签收。

真实模型验收使用local.env，仅在确认发送范围后运行，每次指定新的output：

```powershell
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/09-ops-agent/scripts/accept-local.py --output artifacts/stage9-ops-agent/NEW-BATCH/real
```

Go全包测试在后端模块执行，阶段8/BND pytest使用rag-test.env；阶段7unittest使用阶段7 PYTHONPATH。前端npm test、npm run build包含类型检查。Go MA-C19集成测试仅接受ma_m1_business_test和回环Redis16379，TDengine/推理/通知为本机HTTP fixture，不连真实飞书。

## MA-C10隔离本机故障验收

测试入口为tests/ma_c10_gateway.py与ma_c10_probe.js，只服务独立前端和合成任务/账号，不调用模型、数据库、Redis或业务服务，不转发请求。观察探针只记录真实原生事件、可信性、页面渲染及时间；不模拟事件或设置document可见性。

每次使用全新批次，先在前端目录构建到批次dist，再从仓库根启动。端口只允许15174或15175且绑定回环，禁止借用已有服务端口。以下NEW-C10-BATCH必须替换为尚未运行的批次名。

```powershell
# 从stages/04-frontend目录构建，包含独立类型检查。
npm run typecheck
node node_modules/vite/bin/vite.js build --outDir ../../artifacts/stage9-ops-agent/NEW-C10-BATCH/dist
# 以下从仓库根执行。
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/09-ops-agent/tests/ma_c10_gateway.py serve --run artifacts/stage9-ops-agent/NEW-C10-BATCH --static-root artifacts/stage9-ops-agent/NEW-C10-BATCH/dist --port 15175
```

页面合成登录为ma_c10 / fixture-only，不能用于真实业务接口。控制命令只改变该fixture，私有控制密钥由程序写入该批次private文件，不能打印。

```powershell
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/09-ops-agent/tests/ma_c10_gateway.py control --run artifacts/stage9-ops-agent/NEW-C10-BATCH --mode rate --scenario retry-after --retry-after 8 --plain
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/09-ops-agent/tests/ma_c10_gateway.py control --run artifacts/stage9-ops-agent/NEW-C10-BATCH --mode reset --scenario network-failure
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/09-ops-agent/tests/ma_c10_gateway.py control --run artifacts/stage9-ops-agent/NEW-C10-BATCH --mode normal --scenario recovery --snapshot B
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/09-ops-agent/tests/ma_c10_gateway.py control --run artifacts/stage9-ops-agent/NEW-C10-BATCH --mode normal --scenario terminal --status succeeded
```

原生Chrome切换到其他标签再恢复，按页面ID/Cookie关联原生isTrusted事件与详情GET计数；不能只凭只读DOM镜像中的可见性值签收。结束前保存摘要、构建/源码SHA、原始请求与实际截图，只按本批次PID和启动时间关闭辅助服务；保留证据，不自动清理其他批次。

## 四、运行、失败及回退

健康接口仅说明依赖与配置，不证明模型可用。服务关闭/失联明确不可用，禁止恢复主智能体隐藏直查通道。取消等待只中止聊天HTTP等待；已持久受理的任务继续，可从本人任务查询恢复；取消任务停止后续步骤，正在进行的只读请求完成后保留证据。

进程重启：queued显式auth_context_lost，已执行任务worker_interrupted或timeout，保留部分证据；不会持久保存JWT后自动恢复。数据库保存失败停止后续步骤，旧epoch不可覆盖结果。会话删除先预留屏障，活动任务409；失败释放，成功保留墓碑和终态任务审计。

轮次保存后绑定任务，失败保留link_pending；打开本人会话时重试同一task与实际轮次。固定核验保留verification_id幂等及原轮次，两个模型均不调用；分页不完整显示partial，不推断无告警。Task卡不覆盖原回答，“解释此结果”是新轮次。

Dockerfile使用非root、单worker、无Docker socket；本轮未构建/部署服务器镜像。服务及数据库回退须保留ma_ops审计和会话数据；完整服务器回退属MA-M4，不能用开关替代。

## 三智能体计划与阶段9边界（2026-10-13）

MA-M1/M2技术签收、MA-M3本机隔离通过，当前写窗口关闭；2026-10-13当次MA-M4延期等待安全集成后评估；2026-10-14已联合启动，当前状态见阶段四交付页。现有ops_agent、安全签名模块、审批、Executor与broker是程序控制机制，当次不能称新增Security模型已实现；后续SEC独立模型的实际签收见阶段10及阶段四入口。

未来SEC在严格计划之后提供独立结论，程序仍验证当前身份、所有权、Manifest、资产/策略/参数、epoch/租约/取消/预算和审批一次消费。安全allow不打开写开关，不替代admin决定；后审不能覆写execution_uncertain或补写。

安全模型独立服务源码实际开发才建，ma-review/v1与security_reviews已在SEC-M2本机接入；生产运行仍须后续发布验收。当前命令、端口和测试范围保持，未来适配在独立SEC批次验收。

## SEC-M2本机集成

SEC-M2本机候选已通过runtime_reviews接入Worker/Executor/ApprovalStore，复用005、增量006；敏感读和R2最后领取/消费重新核对，审查默认关闭、已安装库不能降级旁路。门禁配套镜像deploy/security.Dockerfile包含共享协议，根上下文使用配套正向dockerignore。M1合成reviews控制器限制不变。

当前状态见[[notes/01-项目调研规划与开发建设记录/第二版-智能体升级/多智能体协作建设/05-安全审查模型/05-功能开发与阶段交付/02-强制门禁集成/03-SEC-M2测试与交付]]；本阶段统一运行命令见[阶段10源码README](../10-security-agent/README.md)。历史“尚未接入”段落指建设前状态，不能作当前源码判定。

## 阶段四当前交付与导航

- [[notes/01-项目调研规划与开发建设记录/第二版-智能体升级/多智能体协作建设/06-联合发布验收/02-服务器发布与恢复验收/03-MA-M4测试与交付]]
- [[notes/01-项目调研规划与开发建设记录/第二版-智能体升级/多智能体协作建设/06-联合发布验收/01-本地协作集成验收/03-SEC-M4测试与交付]] · [[notes/01-项目调研规划与开发建设记录/第二版-智能体升级/多智能体协作建设/06-联合发布验收/02-服务器发布与恢复验收/03-MA-M4测试与交付]]

## 阶段四服务器发布、回退与恢复

> 仅R730XD的/opt/powersystem及本期新ma-m4-*运行目录；先取得SEC-M4正式签收与对应运行摘要。candidate.tar.gz必须由正向白名单构成，运行日志、凭据和模型key私有保存；server写能力关闭，不复制本机写key。模型key只能直接传至既有业务主机私有目录，勿经公网入口主机中转。

#### 步骤1 · 准备、旧版备份与固定候选

```bash
python3 /opt/powersystem/runtime/<本期MA批次>/controller.py prepare --release /opt/powersystem/runtime/<本期MA批次>
bash /opt/powersystem/stages/04-frontend/deploy/backup.sh
python3 /opt/powersystem/runtime/<本期MA批次>/controller.py build --release /opt/powersystem/runtime/<本期MA批次>
python3 /opt/powersystem/runtime/<本期MA批次>/configure-ma.py --release /opt/powersystem/runtime/<本期MA批次>
python3 /opt/powersystem/runtime/<本期MA批次>/controller.py migrate --release /opt/powersystem/runtime/<本期MA批次>
python3 /opt/powersystem/runtime/<本期MA批次>/bootstrap-assets.py --release /opt/powersystem/runtime/<本期MA批次>
```

controller.py是本轮ma-release.py的稳定私有副本，实际回退后仍可恢复同候选。prepare从运行容器读取实际env来源及可寻址镜像，备份保存配套旧源码与collector登记。既有加密备份目标按密文SHA256实际核对，不能只检查文件存在。

#### 步骤2 · 激活、重新登记、真实页面与回退

```bash
python3 /opt/powersystem/runtime/<本期MA批次>/controller.py activate --release /opt/powersystem/runtime/<本期MA批次>
python3 /opt/powersystem/runtime/<本期MA批次>/controller.py refresh_assets --release /opt/powersystem/runtime/<本期MA批次>
python3 /opt/powersystem/runtime/<本期MA批次>/controller.py rollback --release /opt/powersystem/runtime/<本期MA批次>
python3 /opt/powersystem/runtime/<本期MA批次>/controller.py activate --release /opt/powersystem/runtime/<本期MA批次>
python3 /opt/powersystem/runtime/<本期MA批次>/controller.py refresh_assets --release /opt/powersystem/runtime/<本期MA批次>
```

初始任务库由主机发布维护程序引入已核验collector实际版本，imported_by=0表示程序维护，不伪造旧版本或用户JWT；每次容器身份变化后，控制器按保留库版本单调递增，沿用Store导入校验与事务记录程序来源；旧版本审查和审批不能复用。分别验证原版恢复及同一候选重新运行，数据库/审计保留。公网HTTPS/WSS、当前角色、跨用户404、旧历史、任务/巡检及内部接口隔离由实际页面登记；不制造真实群通知。

#### 步骤3 · 最终备份、隔离恢复与观察

```bash
bash /opt/powersystem/stages/04-frontend/deploy/backup.sh
bash /opt/powersystem/stages/04-frontend/deploy/restore-verify.sh <最终密文备份绝对路径>
python3 /opt/powersystem/stages/09-ops-agent/deploy/observe-ma.py --release /opt/powersystem/runtime/<本期MA批次> --seconds 1800
```

最终备份前停止接纳并排空本轮任务，补充备份钩子保存14张表、实际env引用/策略/审批/审查/资产版本。恢复钩子仅接受新建隔离项目、只读角色及Redis ACL，无模型和执行凭据、禁Worker/POST，不重放队列与批准。观察六个固定镜像及整个原业务项目状态，每30秒不少于1800秒；中断或候选变化必须新批次计时。备份/恢复与DB回归串行，生产故障注入放隔离环境。

## v3.0.0仓库发行

仓库大版本v3.0.0覆盖本阶段与Supervisor/Security集成；运行候选仍r6/v5及维护修订3，生产仅R0。源码使用固定标签获取，部署仍依本页唯一入口，不能由GitHub发行自动开放写窗口。记录见[[notes/04-系统部署发布备份与变更记录/部署与运维日志/05-v3.0.0-GitHub发布记录]]。

## 2026-10-16 · 严格取证回答可读性

[[notes/01-项目调研规划与开发建设记录/第二版-智能体升级/问答及调度业务主智能体/当前方案-Agent与RAG重建/04-增量优化与补充交付/01-回答可读性与任务结果展示/00-索引·回答可读性与任务结果展示]]维护问题、展示模板与边界；实现仅在阶段四前端，普通问答Markdown保持。程序事实来自实际步骤返回，模型结论分段、研判和建议编号，允许类审查默认折叠、异常项突出，历史回执与原文保留。实际安装及发布证据见[[notes/01-项目调研规划与开发建设记录/第二版-智能体升级/问答及调度业务主智能体/当前方案-Agent与RAG重建/04-增量优化与补充交付/01-回答可读性与任务结果展示/03-测试与交付]]，不为本次展示修改模型提示词或重复运行原业务测试。

## 相关笔记

- [多智能体建设索引](../../notes/01-项目调研规划与开发建设记录/第二版-智能体升级/多智能体协作建设/00-索引·多智能体协作建设.md)
- [任务协议与接口契约](../../notes/01-项目调研规划与开发建设记录/第二版-智能体升级/多智能体协作建设/03-程序控制层/02-任务协议与接口契约.md)
- [工程规范](../../notes/02-工程规范边界与验收治理/项目工程规范.md) · [运行产物说明](../../notes/02-工程规范边界与验收治理/运行产物说明.md)

## 2026-10-15 API故障恢复与现行收尾

17:33基础编排重建导致API版本回退及助手配置遗漏；本次按已签收r6镜像恢复API并加载阶段7/8/9完整配置，保留当前前端、通知与模拟数据。两公网入口的会话/任务列表及详情、核心只读业务、HTTPS/WSS和一次技术模型问答已复验。完整恢复、备份及观察证据见[[notes/04-系统部署发布备份与变更记录/部署与运维日志/06-API版本回退恢复记录]]；现行待办按[[notes/04-系统部署发布备份与变更记录/部署与运维日志/09-项目收尾核对]]核对，历史阶段未勾选项不覆盖后来签收。
