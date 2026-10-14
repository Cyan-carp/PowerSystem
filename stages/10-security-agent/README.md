---

## SEC-M3本机项目只读巡检（2026-10-14）

以下M3历史本机隔离技术签收见[SEC-M3正式交付](../../内容/第二版-智能体升级/多智能体协作建设/02-安全审查模型/开发阶段3-项目只读巡检/03-SEC-M3测试与交付.md)。该M3批次零写，M3没有open阶段；旧SEC-M2历史命令范围保持独立，不得用它打开本轮巡检写窗口。许可sec-m3-local-inspection/v1，策略sec-m3-policy/v3，模型0731/30秒，旧模型质量失败保留。

### 步骤1 · 建立新批次与隔离实例

从仓库根目录执行；run必须新建，不覆盖已存在private。15434/16379/18105/18097/8110～8113/15179全部回环预检，冲突停止。仅本批Compose对象，原服务/卷保持；模型凭据只继承私有引用，不打印。

```powershell
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/prepare-m3.py --run artifacts/stage10-security-agent/sec-m3-<日期>-<批次> --phase config
docker compose -f artifacts/stage10-security-agent/<本批次>/private/compose.json up -d postgres redis drill
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/prepare-m3.py --run artifacts/stage10-security-agent/<本批次> --phase databases
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/prepare-m3.py --run artifacts/stage10-security-agent/<本批次> --phase drill
docker compose -f artifacts/stage10-security-agent/<本批次>/private/compose.json up -d broker
stages/10-security-agent/scripts/start-m3.ps1 -ConfigFile artifacts/stage10-security-agent/<本批次>/private/local.env -Initialize -Frontend
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/accept-m3.py --run artifacts/stage10-security-agent/<本批次> --phase import --output artifacts/stage10-security-agent/<本批次>/import-<尝试>
```

### 步骤2 · 软件与真实分层验收

验收sec_m3_acceptance_test/rag_acceptance与可清理sec_m3_regression_test/rag_regression分离；旧SEC/MA回归只使用本轮新PG内独立库。每次attempt/output新建，失败不覆盖。数据库回归完成后再做依赖恢复，不并行重启同一PG。

```powershell
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/run-regression.py --run artifacts/stage10-security-agent/<本批次> --layer sec --attempt <尝试>
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/run-regression.py --run artifacts/stage10-security-agent/<本批次> --layer ops --attempt <尝试>
& artifacts/stage8-agent-rag/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/run-regression.py --run artifacts/stage10-security-agent/<本批次> --layer rag --attempt <尝试>
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/accept-m3.py --run artifacts/stage10-security-agent/<本批次> --phase read --output artifacts/stage10-security-agent/<本批次>/read-<尝试>
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/accept-m3.py --run artifacts/stage10-security-agent/<本批次> --phase target --output artifacts/stage10-security-agent/<本批次>/target-<尝试>
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/quality-m3.py --run artifacts/stage10-security-agent/<本批次> --output artifacts/stage10-security-agent/<本批次>/quality-<尝试>
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/accept-m3.py --run artifacts/stage10-security-agent/<本批次> --phase explain --task <本人task-id> --output artifacts/stage10-security-agent/<本批次>/explain-<尝试>
```

真实质量脚本为条件fixture，零grant/工具，不代偿实际取证。本机仅登记容器inspect，其余能力未检查；完整报告仅本人页面，解释只发送新审查摘要。前端/Go测试命令见对应源码README，business-test.env仅本轮专库，不输出私有值。

### 步骤3 · 页面、恢复与关闭

fixture-cancel只建立明确标注的合成等待任务，由本轮本人实际页面取消，零模型/远端读取/写；不能拿它证明真实主机故障。实际应用/PG重启先保存基线，回归全部结束后才停止本批进程，禁止down -v。

```powershell
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/verify-m3.py --run artifacts/stage10-security-agent/<本批次> --phase fixture-cancel --output artifacts/stage10-security-agent/<本批次>/cancel-<尝试>
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/verify-m3.py --run artifacts/stage10-security-agent/<本批次> --phase before-recovery --task <本人task-id> --output artifacts/stage10-security-agent/<本批次>/before-<尝试>
stages/10-security-agent/scripts/start-m3.ps1 -ConfigFile artifacts/stage10-security-agent/<本批次>/private/local.env -Stop
docker compose -f artifacts/stage10-security-agent/<本批次>/private/compose.json restart postgres
stages/10-security-agent/scripts/start-m3.ps1 -ConfigFile artifacts/stage10-security-agent/<本批次>/private/local.env -Frontend
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/verify-m3.py --run artifacts/stage10-security-agent/<本批次> --phase after-recovery --task <本人task-id> --baseline artifacts/stage10-security-agent/<本批次>/before-<尝试>/baseline.json --output artifacts/stage10-security-agent/<本批次>/after-<尝试>
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/verify-m3.py --run artifacts/stage10-security-agent/<本批次> --phase closed --task <本人task-id> --output artifacts/stage10-security-agent/<本批次>/closed-<尝试>
```

进程/库/端口当前状态以实际检查为准；健康不替代模型、实际读取、页面和存证。SEC-M3通过只判SEC-M4准入，服务器及知识不自动发布。

tags: [源网智联, 第二版, 安全审查, 源码运行]
---

# 阶段10 · 独立安全审查与强制门禁

> 2026-10-14当前：SEC-M4本机限定签收及r6追加准入通过；MA-C32～C35和最终文档门槛已完成，MA-M4及联合阶段四在声明范围正式技术签收。服务器r6/v5、恢复维护修订3；本机唯一批准动作保留，写窗口关闭，生产仅登记R0。

> 当前：SEC-M1常规配置签收；SEC-M2本机限定技术验收签收，窗口已关闭并实测阻断，SEC-M3未启动。最新结果、问题和下一阶段判定见[SEC-M2测试与交付](../../内容/第二版-智能体升级/多智能体协作建设/02-安全审查模型/开发阶段2-强制门禁集成/03-SEC-M2测试与交付.md)。本页维护运行命令；旧SEC-M1批次及其命令保留为历史。

## SEC-M2当前运行入口

程序门禁归阶段9runtime_reviews与006迁移，独立服务/ma-review契约/版本化策略归阶段10。OPS_SECURITY_ENABLED默认false；安装006的任务库不能通过关闭服务跳过门禁。运行模型固定deepseek-v4-flash-0731，30秒/TTL≤300/并发1与2/零自动重试；当前策略sec-m2-policy/v5按kind仅给出适用规则，许可sec-m2-local-drill/v1。M1控制器、sec-m1-policy/v6及合成许可不改变。

### 步骤1 · 准备新本机批次

从根目录运行。prepare config只建立新私有目录，预检九个端口并生成独立凭据；显式继承已验收的模型私有文件引用，修改引用须重新验收。不要打印配置、认证头或密钥；不要覆盖旧批次。脚本创建新PG17.6、Redis7、无状态drill与专用broker编排，不替换原服务。drill阶段仅读取本批对象并登记别名、镜像与策略摘要。

```powershell
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/prepare-m2.py --run artifacts/stage10-security-agent/sec-m2-<日期>-<批次> --phase config
docker compose -f artifacts/stage10-security-agent/<本批次>/private/compose.json up -d postgres redis drill
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/prepare-m2.py --run artifacts/stage10-security-agent/<本批次> --phase databases
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/prepare-m2.py --run artifacts/stage10-security-agent/<本批次> --phase drill
docker compose -f artifacts/stage10-security-agent/<本批次>/private/compose.json up -d broker
stages/10-security-agent/scripts/start-m2.ps1 -ConfigFile artifacts/stage10-security-agent/<本批次>/private/local.env -Initialize -Frontend
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/accept-m2.py --run artifacts/stage10-security-agent/<本批次> --phase import --output artifacts/stage10-security-agent/<本批次>/import-<独立尝试>
```

PG15433/Redis16378，Go代理18104、Supervisor18096、Ops8101、adapter8102、broker8103、Security8100、前端15178均限定回环。进程启动校验PID/启动时间及端口归属、隐藏窗口、按组件收窄环境变量。Go代理不启动MQTT、预测或通知。验收库sec_m2_acceptance_test、sec_m2_rag_acceptance_test与sec_m2_regression_test、sec_m2_rag_regression_test分离；旧MA及SEC回归在新实例内独立库，避免TRUNCATE历史。

### 步骤2 · 程序回归与真实只读/模型验收

每次attempt/output新建；pytest日志与XML独立保存。SEC回归包括M1合成服务/专库与M2真实PG事务，provider/collector标明fixture。真实quality条件fixture没有执行grant，不能称为真实工具或运行身份授权证明。read/explain是实际本机链路；串行运行同用户真实样例，超限明确停止，不自动模型重试。

```powershell
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/run-regression.py --run artifacts/stage10-security-agent/<本批次> --layer sec --attempt <独立尝试>
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/run-regression.py --run artifacts/stage10-security-agent/<本批次> --layer ops --attempt <独立尝试>
& artifacts/stage8-agent-rag/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/run-regression.py --run artifacts/stage10-security-agent/<本批次> --layer rag --attempt <独立尝试>
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/accept-m2.py --run artifacts/stage10-security-agent/<本批次> --phase read --output artifacts/stage10-security-agent/<本批次>/read-<独立尝试>
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/quality-m2.py --run artifacts/stage10-security-agent/<本批次> --output artifacts/stage10-security-agent/<本批次>/quality-<独立尝试>
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/accept-m2.py --run artifacts/stage10-security-agent/<本批次> --phase explain --task <本人task-id> --output artifacts/stage10-security-agent/<本批次>/explain-<独立尝试>
```

Go全包和前端test/typecheck/build命令见对应源码README；本批business-test.env只指向ma_m2_business_test和Redis8专库，运行前在本机环境加载，不输出私有值。所有模型材料仅许可内归一化请求、别名/版本/摘要、状态、审批核对与引用；Security服务自身按固定许可校验，不递归自审。

### 步骤3 · 本人审批的真实演练

只有同候选预检证据齐备、真实模型质量阻塞清零才能保存preflight-ready.json并开启本批最长1800秒窗口，旧窗口不能复用。仅此新建无状态容器，短暂健康中断、10秒停止宽限；无业务数据/持久卷，不能承诺重启回滚。脚本write只生成卡片，**每次真实重启由本人admin在卡片明确批准**；批准后程序直接执行保存计划，无模型重新规划。allow不打开窗口。

```powershell
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/prepare-m2.py --run artifacts/stage10-security-agent/<本批次> --phase open
docker compose -f artifacts/stage10-security-agent/<本批次>/private/compose.json up -d --force-recreate broker
stages/10-security-agent/scripts/start-m2.ps1 -ConfigFile artifacts/stage10-security-agent/<本批次>/private/local.env -Frontend
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/accept-m2.py --run artifacts/stage10-security-agent/<本批次> --phase write --output artifacts/stage10-security-agent/<本批次>/write-<独立尝试>
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/accept-m2.py --run artifacts/stage10-security-agent/<本批次> --phase report --task <本人task-id> --output artifacts/stage10-security-agent/<本批次>/result-<独立尝试>
```

### 步骤4 · 立即关闭、实测阻断与实际恢复

完成动作即关闭，不等待窗口到期。closed只在私有文件及程序写开关均关闭后，使用结构合法测试grant探测broker关闭门禁，核对drill未变化和跨用户404；不产生新审批。随后停止仅本批进程、重启仅本批PG并启动；受控pending恢复fixture不授权续跑，核对历史、动作及模型计数不变。禁止down -v及原库清理。

```powershell
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/prepare-m2.py --run artifacts/stage10-security-agent/<本批次> --phase close
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/verify-m2.py --run artifacts/stage10-security-agent/<本批次> --phase closed --output artifacts/stage10-security-agent/<本批次>/closed-<独立尝试>
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/verify-m2.py --run artifacts/stage10-security-agent/<本批次> --phase prepare-recovery --output artifacts/stage10-security-agent/<本批次>/recovery-<独立尝试>
stages/10-security-agent/scripts/start-m2.ps1 -ConfigFile artifacts/stage10-security-agent/<本批次>/private/local.env -Stop
docker compose -f artifacts/stage10-security-agent/<本批次>/private/compose.json restart postgres
stages/10-security-agent/scripts/start-m2.ps1 -ConfigFile artifacts/stage10-security-agent/<本批次>/private/local.env -Frontend
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/verify-m2.py --run artifacts/stage10-security-agent/<本批次> --phase verify-recovery --output artifacts/stage10-security-agent/<本批次>/recovery-<独立尝试>
```

历史批次证据只保留，不替代新候选签收。源码镜像本地构建：Security上下文stages/10-security-agent正向白名单；Ops门禁配套Dockerfile从根上下文显式复制共享协议，配套dockerignore排除其他文件。没有在本阶段发布这些镜像。笔记入口：[阶段索引](../../内容/第二版-智能体升级/多智能体协作建设/02-安全审查模型/开发阶段2-强制门禁集成/00-索引·强制门禁集成.md)、[实现](../../内容/第二版-智能体升级/多智能体协作建设/02-安全审查模型/开发阶段2-强制门禁集成/02-实现记录.md)、[问题](../../内容/第二版-智能体升级/多智能体协作建设/02-安全审查模型/开发阶段2-强制门禁集成/04-问题记录与解决方案.md)。

## SEC-M1历史运行入口

> 以下保留SEC-M1原签收与命令范围；“尚未接入Worker”等均指当时，当前运行门禁见上节及SEC-M2正式页。M1合成专库/许可限制继续有效。

## 一、职责与隔离

Security只接收程序生成的安全包并输出三类结论。服务不连接数据库、不持SSH/Docker/业务凭据、不注册执行工具。阶段9新增程序控制模块负责ma_ops审查记录与事件；迁移005仅由专用验收入口显式加载，现有Store/Worker/ResultEnvelope未接入。

服务、请求及结果签名使用三个独立私有文件；API凭据使用第四个文件。认证头、签名、JWT和连接配置不进入模型、数据库或公开文档。当前模型经所有者指令换回deepseek-v4-flash-0731，使用本次新独立私有API文件，30秒、例外false，无fallback。原型号404、0731清单/质量/502故障及旧配置保留；不会自动换回。更换后的API/型号须重新验收。

协议字段、预算、失败与投影的单一说明见[[内容/第二版-智能体升级/多智能体协作建设/00-程序控制层/06-审查协议、外发与审计]]；规则由安全模型专题维护。当前许可仅sec-m1-synthetic/v3、策略sec-m1-policy/v6，旧日志/业务原文/生产任务不可送入本阶段控制器。普通GET、BND和写窗口保持原状态。

## 二、准备私有批次

#### 步骤1 · 创建独立配置

> 从仓库根目录运行；批次必须新建，不能重复覆盖local.env。配置准备不启动Docker、不清理历史。API凭据由所有者保存为本批次private/sec-llm-key；不能放在命令参数或公开.env.example中。

```powershell
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/prepare-local.py --run artifacts/stage10-security-agent/sec-m1-<日期>-<批次>
```

优先使用已可用的阶段9虚拟环境；新环境安装requirements-test.txt，并准备程序验收需要的阶段9/阶段8既有依赖。requirements.txt锁定服务及依赖版本；Docker仅安装服务依赖，以UID10004运行。构建上下文只放源码，私有文件不复制进镜像。

#### 步骤2 · 启动独立数据库依赖

> 先确认Docker Engine可达、15432/16376回环端口未被占用。仅启动本批次Compose项目；禁止重置Docker、清空旧库或执行down -v。本轮Docker故障已解阻，独立PG17.6/Redis7实例已运行；问题及历史见独立问题页。

```powershell
docker compose -f artifacts/stage10-security-agent/<本批次>/private/compose.json up -d
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/setup-databases.py --run artifacts/stage10-security-agent/<本批次>
```

数据库sec_m1_acceptance_test与sec_m1_regression_test分别用于长期验收与清理测试；旧MA回归使用新实例内的ma_m3_ops_regression_test，不加载005，避免旧TRUNCATE碰到新增外键。程序持有连接，模型服务不读取databases.json。

## 三、服务及独立模型探测

#### 步骤1 · 启动或停止本批次服务

> 仅监听127.0.0.1:8100，启动前检查占用。停止只针对process.json中PID与启动时间匹配的辅助进程，并核验健康地址不可达。窗口隐藏，日志独立命名。

```powershell
stages/10-security-agent/scripts/start-local.ps1 -ConfigFile artifacts/stage10-security-agent/<本批次>/private/local.env
stages/10-security-agent/scripts/start-local.ps1 -ConfigFile artifacts/stage10-security-agent/<本批次>/private/local.env -Stop
```

模板SEC_ENABLED=false；专用合成验收配置显式true。health只表示进程/配置，不证明模型可用或阶段签收。

#### 步骤2 · 单样例型号可用性探测

> 仅在明确授权网关及合成字段范围后运行。probe是独立服务/真实模型层，不验证数据库、不代表SEC-M1完整验收。404或其他故障不自动换型号、不重试。

```powershell
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/probe-model.py --run artifacts/stage10-security-agent/<本批次> --output artifacts/stage10-security-agent/<本批次>/probe-<独立尝试>
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/list-models.py --key-file artifacts/stage10-security-agent/<本批次>/private/sec-llm-key --output artifacts/stage10-security-agent/<本批次>/model-inventory-<独立尝试>.json
```

## 四、程序测试与真实闭环验收

#### 步骤1 · 运行独立测试

> 数据库配置仅指向sec_m1_regression_test；不设置配置时16项明确skip，不能算通过。pytest临时目录在本批次下独立命名；Windows当前用户权限与受限沙箱差异须分别登记。

```powershell
$env:PYTHONPATH="$PWD/stages/10-security-agent;$PWD/stages/09-ops-agent;$PWD/stages/08-agent-rag"
$env:SEC_TEST_CONFIG_FILE="$PWD/artifacts/stage10-security-agent/<本批次>/private/databases.json"
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 -m pytest stages/10-security-agent/tests -q -p no:cacheprovider --basetemp artifacts/stage10-security-agent/<本批次>/tmp-<独立尝试>
```

#### 步骤2 · 执行真实程序存证闭环

> 先通过数据库测试与指定型号探测。验收创建专库内合成任务/步骤/资产，不执行真实读取或重启。输入不含生产材料；每条样例至多一次模型调用，重放读取原记录。错误批次保留，输出目录不得已存在。

```powershell
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/accept-local.py --run artifacts/stage10-security-agent/<本批次> --output artifacts/stage10-security-agent/<本批次>/real-<独立尝试>
```

十条合成样例覆盖六类审查；记录预期、实际、引用、错误、耗时和token，不保存内部推理或网关原响应。程序状态/绑定/有效期和审计由PostgreSQL事务保存。持久化失败不返回有效结果；startup恢复pending为unavailable，不重放模型。

程序模块只供本阶段验收；recover须在确认没有活跃本批次控制器后由单一启动者调用。service进程内防重只是短期保护，跨重启真相由ma_security_reviews维护，不把服务缓存当历史。

## 五、本轮证据与后续

证据：artifacts/stage10-security-agent/sec-m1-20261013-r1/。offline-r1原失败、offline-r2/sec-r3/sec-final-r1/r2/r3、ops-r1、rag-r1/r2/r3及probe-r1保留；失败修复、跳过及Docker故障分别见正式页与问题页。原0731记录为历史；当前新密钥/0731常规30秒补查签收，v6下124程序/专库、真实程序存证10/10与恢复通过。

SEC-M2负责门禁和security_reviews报告集成；SEC-M3负责主动只读巡检。安全前三阶段签收后统一启动第四阶段，先SEC-M4本地集成，再MA-M4发布验收。本轮不发布服务器、不开写、不导入知识、不提交或推送。

## 六、0731服务/模型层十样例验收

#### 步骤1 · 显式运行服务层模式

> Docker未就绪时仍可在明确合成许可下执行此模式；它只创建虚构请求、验证真实服务签名/模型意见，不创建或模拟数据库。没有自动从数据库模式降级的分支。输出sample_suite_passed=true也不能替代database_verified/signed_off。

```powershell
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/accept-local.py --run artifacts/stage10-security-agent/<本批次> --output artifacts/stage10-security-agent/<本批次>/real-service-<独立尝试> --model deepseek-v4-flash-0731 --service-only
```

模型只返回四字段Opinion；程序附加protocol/review_id组成原六字段响应并签署摘要。JSON模式和Opinion Schema约束格式，严格校验仍拒绝额外模型关联字段；不补救非法模型结果。scope v3包含虚构程序核对事实及实际程序审查时间，不能等同真实身份或批准。

本轮probe-0731-r1可用，r6六类十样例10/10、危险漏放/误拒0；tests-model-0731-final-r1为92通过/16skip。旧r1～r5失败独立保留。数据库模式默认不变，需依赖就绪后显式执行；当前未签收。

## 七、独立回归与重启恢复

#### 步骤1 · 复核既有运维与BND

> setup-databases已生成私有fixture配置；运维使用新实例内独立旧表集，阶段8使用独立RAG库。所有调用使用测试provider或MockTransport，没有生产工具执行；不传私有值到命令参数。Windows使用本机当前用户权限及各阶段完整环境。

```powershell
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/run-regression.py --run artifacts/stage10-security-agent/<本批次> --layer ops --attempt <独立尝试>
& artifacts/stage8-agent-rag/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/run-regression.py --run artifacts/stage10-security-agent/<本批次> --layer rag --attempt <独立尝试>
```

#### 步骤2 · 核验独立存证重启

> 先确认本批次控制器已停止且无活跃任务。prepare只添加合成pending和保存已有历史；verify使用全新程序进程恢复pending并禁止任何模型调用。仅重启当前SEC项目的postgres，不删除卷。确认ready后才运行verify；失败换目录，保留原快照/错误，不改写历史或延长期限。

```powershell
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/verify-recovery.py --run artifacts/stage10-security-agent/<本批次> --output artifacts/stage10-security-agent/<本批次>/recovery-<独立尝试> --phase prepare
stages/10-security-agent/scripts/start-local.ps1 -ConfigFile artifacts/stage10-security-agent/<本批次>/private/local.env -Stop
docker compose -f artifacts/stage10-security-agent/<本批次>/private/compose.json restart postgres
docker compose -f artifacts/stage10-security-agent/<本批次>/private/compose.json exec -T postgres pg_isready -U sec_owner
# pg_isready显示accepting connections后，才执行下面的核验
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/verify-recovery.py --run artifacts/stage10-security-agent/<本批次> --output artifacts/stage10-security-agent/<本批次>/recovery-<独立尝试> --phase verify
```

当前124 SEC/108 MA/114 BND+2子测试通过，无skip。最终recovery-gpt-final-r1保留15条历史completed，本批10条另核验绑定/20条审计保持且当刻有效，pending恢复1条、网络调用0；旧失败与功能签收分账，30秒性能例外单列。

## 八、所有者批准的临时时限例外及回切（历史，当前已关闭）

#### 步骤1 · 明确启用本批例外

> 仅本轮gpt-5.6-sol专用合成程序存证获得所有者明确许可。私有local.env分别设置SEC_MODEL_TIMEOUT_SECONDS=120、SEC_ACCEPTANCE_TIMEOUT_EXCEPTION_ENABLED=true，API密钥只引用本批private文件；服务仍受父任务剩余预算限制。模板默认30秒/例外false，客户端须双重显式指定，service-only不允许该例外。

```powershell
stages/10-security-agent/scripts/start-local.ps1 -ConfigFile artifacts/stage10-security-agent/<本批次>/private/local.env
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/accept-local.py --run artifacts/stage10-security-agent/<本批次> --output artifacts/stage10-security-agent/<本批次>/real-gpt-exception-<独立尝试> --model gpt-5.6-sol --review-timeout 120 --acceptance-timeout-exception
```

GPT使用none推理及严格JSON Schema，仅返回四字段意见；其他型号不发送这些GPT参数。单条自动重试0，模型非法意见不修补；预算例外不改变身份、投影、签名、审批、并发、存证有效期或失败阻断。

#### 步骤2 · 更换API后恢复常规验收

> 先停止所属服务并保留当前私有配置/失败；在新的独立验收配置中改为明确的目标型号与私有API文件引用，将SEC_MODEL_TIMEOUT_SECONDS恢复30、SEC_ACCEPTANCE_TIMEOUT_EXCEPTION_ENABLED设false，再启动服务。不自动换型号、不覆写旧证据；常规验收不传例外参数。若换回0731，显式--model deepseek-v4-flash-0731；确认可用性后才进行完整批次。

```powershell
stages/10-security-agent/scripts/start-local.ps1 -ConfigFile artifacts/stage10-security-agent/<本批次>/private/local.env -Stop
# 修改新的私有配置引用并恢复30秒/关闭例外后再启动
stages/10-security-agent/scripts/start-local.ps1 -ConfigFile artifacts/stage10-security-agent/<本批次>/private/local.env
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/probe-model.py --run artifacts/stage10-security-agent/<本批次> --output artifacts/stage10-security-agent/<本批次>/probe-return-<独立尝试> --model <明确型号>
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/accept-local.py --run artifacts/stage10-security-agent/<本批次> --output artifacts/stage10-security-agent/<本批次>/real-return-<独立尝试> --model <明确型号>
```

同时按本页前节运行程序/专库、MA/BND、完整真实样例、预算/幂等/绑定/重启恢复及秘密/交付检查；不得把本次120秒功能签收当30秒性能或新API通过。旧成功意见仍最多300秒有效，换配置不延长原记录。

## 九、本次回切后的常规验收入口

#### 步骤1 · 按新配置独立验收

> 本批私有配置引用新独立API文件，固定0731、30秒、SEC_ACCEPTANCE_TIMEOUT_EXCEPTION_ENABLED=false；模板默认服务关闭。新输出目录不能已存在，单条不自动重试，旧GPT和失败批次保留。不传时限例外参数，DeepSeek不会收到GPT专用参数。运行前核验8100归属/端口与本批专庫ready。

```powershell
stages/10-security-agent/scripts/start-local.ps1 -ConfigFile artifacts/stage10-security-agent/<本批次>/private/local.env
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/probe-model.py --run artifacts/stage10-security-agent/<本批次> --output artifacts/stage10-security-agent/<本批次>/probe-return-<独立尝试> --model deepseek-v4-flash-0731
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/accept-local.py --run artifacts/stage10-security-agent/<本批次> --output artifacts/stage10-security-agent/<本批次>/real-return-<独立尝试> --model deepseek-v4-flash-0731
```

依本页前节执行程序/专库、MA/BND与prepare→专用PG重启→ready→verify的恢复核验；理由需另与合成事实核对，程序summary不能代替语义复核。最终real-return-v6-program-db-r1完整10/10、最长9.884秒，124/108/114+2子测试及恢复/绑定通过；原v5首批摘要误述保留quality-review，不作为当前签收批次。当前策略v6，默认30秒/例外关闭，旧有效期不延长，未启动后续或发布。

## 阶段四当前交付与导航

- [[内容/第二版-智能体升级/多智能体协作建设/03-联合发布验收/01-本地协作集成验收/03-SEC-M4测试与交付]]
- [[内容/第二版-智能体升级/多智能体协作建设/03-联合发布验收/01-本地协作集成验收/03-SEC-M4测试与交付]] · [[内容/第二版-智能体升级/多智能体协作建设/03-联合发布验收/02-服务器发布与恢复验收/03-MA-M4测试与交付]]

## 十、SEC-M4独立本机集成

> 在仓库根执行，沿用现有两套虚拟环境与已装依赖。每次失败使用新输出目录；先核对端口与拥有标签。旧M1～M3卷/配置保留。下列占位批次不能复用已消费窗口；真实approve仅由本人admin点击任务卡，脚本不批准。

#### 步骤1 · 新建批次并串行准备

```powershell
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/prepare-m4.py --run artifacts/stage10-security-agent/<新SEC-M4批次> --phase config
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/prepare-m4.py --run artifacts/stage10-security-agent/<新SEC-M4批次> --phase databases
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/prepare-m4.py --run artifacts/stage10-security-agent/<新SEC-M4批次> --phase drill
pwsh -NoProfile -File stages/10-security-agent/scripts/start-m4.ps1 -ConfigFile artifacts/stage10-security-agent/<新SEC-M4批次>/private/local.env -Initialize -Frontend
```

#### 步骤2 · 冻结与分层验收

```powershell
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/accept-m4.py --run artifacts/stage10-security-agent/<新SEC-M4批次> --output artifacts/stage10-security-agent/<新SEC-M4批次>/import-r1 --phase import
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/quality-m4.py --run artifacts/stage10-security-agent/<新SEC-M4批次> --output artifacts/stage10-security-agent/<新SEC-M4批次>/quality-r1
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/freeze-m4.py --run artifacts/stage10-security-agent/<新SEC-M4批次> --candidate <明确的新MA候选> --output-name runtime-manifest-<独立候选名>.json
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/accept-m4.py --run artifacts/stage10-security-agent/<新SEC-M4批次> --output artifacts/stage10-security-agent/<新SEC-M4批次>/diagnose-r1 --phase diagnose
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/accept-m4.py --run artifacts/stage10-security-agent/<新SEC-M4批次> --output artifacts/stage10-security-agent/<新SEC-M4批次>/inspection-r1 --phase target
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/accept-m4.py --run artifacts/stage10-security-agent/<新SEC-M4批次> --output artifacts/stage10-security-agent/<新SEC-M4批次>/explain-r1 --phase explain --task <本人任务ID>
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/restore-local-m4.py --run artifacts/stage10-security-agent/<新SEC-M4批次> --output artifacts/stage10-security-agent/<新SEC-M4批次>/restore-r1
```

数据库回归和恢复串行，完整SEC/MA/RAG/BND/阶段7/Go/前端测试及类型/构建按前文各层入口记录，不相加重叠选测。实际页面事实、诊断、巡检缺项、失败、刷新与取消单独截图。固定主动核验须实际接口并证明模型0调用。

#### 步骤3 · 本人批准、守护及关闭

> 只在全部演练前置检查通过并保存preflight-ready.json后开窗，最多1800秒；先启动守护并取得任务卡，最后由本人点击一次批准。本批已执行一次并关闭，不可重开。

```powershell
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/prepare-m4.py --run artifacts/stage10-security-agent/<新SEC-M4批次> --phase open
pwsh -NoProfile -File stages/10-security-agent/scripts/start-m4.ps1 -ConfigFile artifacts/stage10-security-agent/<新SEC-M4批次>/private/local.env -Frontend
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/accept-m4.py --run artifacts/stage10-security-agent/<新SEC-M4批次> --output artifacts/stage10-security-agent/<新SEC-M4批次>/pending-write-r1 --phase write
$m4Pending = Get-Content artifacts/stage10-security-agent/<新SEC-M4批次>/pending-write-r1/summary.json -Raw -Encoding utf8 | ConvertFrom-Json
@{task_id=$m4Pending.result.task_id} | ConvertTo-Json | Set-Content artifacts/stage10-security-agent/<新SEC-M4批次>/write-pending.json -Encoding utf8
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/watch-window-m4.py --run artifacts/stage10-security-agent/<新SEC-M4批次> --shell-executable <PowerShell7绝对路径>
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/verify-m4.py --run artifacts/stage10-security-agent/<新SEC-M4批次> --output artifacts/stage10-security-agent/<新SEC-M4批次>/closed-r1 --phase closed --task <本人任务ID>
& artifacts/stage9-ops-agent/venv/Scripts/python.exe -X utf8 stages/10-security-agent/scripts/prove-write-m4.py --run artifacts/stage10-security-agent/<新SEC-M4批次> --output artifacts/stage10-security-agent/<新SEC-M4批次>/one-shot-r1
```

如权威窗口已关闭但所属服务reload失败，仅使用watch-window-m4.py的--closed-reload及显式PowerShell7恢复关闭档；独立stdout/stderr与失败证据保留。verify-m4.py的before-recovery/after-recovery串行比较实际专库重启前后记录，禁止重新消费审批。模型或写动作不自动重试。

### M4当前运行档与历史范围

2026-10-14：本机许可/策略保持v2；服务器只读策略sec-m4-policy/v5、许可sec-m4-project-readonly/v5；旧v2/v3/v4服务器任务保留原绑定且不自动提升，配置/资产变化使旧审查或审批失效。公共ma/v1与ma-review/v1及四字段意见/六字段服务响应不变。r6核心174项905f62d2209ec6d9f0f6d56abb9c321f6a81ef90e0a4bdbb547bcdfed565a1e8；备份恢复脚本维护修订独立留档，不改运行核心/镜像。

上方M1/M2/M3许可、命令和“当次未签收”描述均为对应历史批次，不代表M4当前权限。实际本人批准前须确认守护已经轮询write-pending.json对应任务；守护单独终端保持运行，命令不替本人批准。本轮已消费一次且关闭，禁止复用。

## v3.0.0仓库发行

安全审查服务与运维模型一起纳入v3.0.0公开源码版本；原运行策略/许可及SEC-M4签收不重写。发行记录见[[部署/部署与运维/v3.0.0-GitHub发布记录]]，运行/复验仍按本页入口与已授权范围进行。

## 相关笔记

- [[内容/第二版-智能体升级/多智能体协作建设/02-安全审查模型/开发阶段1-独立审查闭环/00-索引·独立审查闭环]]
- [[内容/第二版-智能体升级/多智能体协作建设/02-安全审查模型/开发阶段1-独立审查闭环/03-SEC-M1测试与交付]]
- [[内容/第二版-智能体升级/多智能体协作建设/02-安全审查模型/开发阶段1-独立审查闭环/04-问题记录与解决方案]]
