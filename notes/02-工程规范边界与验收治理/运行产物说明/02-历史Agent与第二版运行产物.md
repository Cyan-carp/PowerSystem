---
tags:
  - 源网智联
  - 运行产物
  - 开发阶段3
  - 开发阶段4
  - 开发阶段5
status: 现行
scope: 跨版本
updated: 2026-10-30
---

# 运行产物说明 · artifacts 文件索引 · 历史Agent与第二版运行产物

> 本页由 [[02-工程规范边界与验收治理/运行产物说明/00-索引·运行产物说明]] 分卷维护，原有事实、编号和验收边界保持不变。

## 九、第二版阶段一 · Agent 基础服务产物

`artifacts/stage7-智能体/` 是第二版智能体的本机运行产物根目录，对应源码 `stages/07-agent/`。

> `artifacts/stage7/` 是早期目录名残留，仅含 `bin/api.exe`、`bin/gateway.exe`。**2026-10-30 实测：与 `stage7-智能体/bin/` 同名但不同字节**——stage7 为 2026-10-05 构建（24,128,512 / 24,562,176 字节），`stage7-智能体/bin/` 为 2026-10-10 重新构建（24,164,352 / 24,563,712 字节），两组 SHA256 均不同。即旧版构建产物残留，**不是重复文件**；此前「同字节、完全重复」的描述已按实测更正。可作冗余清理，代价是失去 10-05 那版二进制实体；源码仍在 `stages/07-agent/`，可按需重建。

**目录与稳定文件**

- `pydeps/`：本机锁定依赖，可重建，不是业务数据。
- `bin/api.exe`、`bin/gateway.exe`：第二版阶段一本机编译产物。
- `runtime/`：本阶段 API／网关进程记录和日志（`agent.log`、`api.log`、`gateway.log`、`fixture.log`、`frontend.log` 及对应 `.stderr.log`）；通过阶段七停止脚本结束，不删除业务数据卷。
- `private/`：模型密钥、服务令牌与测试 JWT，仅私有保留；另含 provider／搜索能力探测与诊断脚本及结果（`probe-*.py`、`diagnose-*.py` 及对应 `*.json`），仅私有保留。

> `private/` 绝不入 Git 或公开审查包。

- `local-fixture/`：首轮 29 项联调、后端 38 项回归与模拟器审计；不代表真实 LLM。
- `real-model/`：真实模型诊断与审计目录，顶层含模型列表探测、回答超时响应与审计摘录；其下 `audit/` 保存完整审计。

**关键批次**

- 最终批次 `stage7_20260930_174614_13d3/`：21 项离线、32 项本地联调与两份 38 项回归；不计真实 LLM 验收。
- 真实模型失败批次 `stage7_20260930_191610_5bef/`：模型列表与诊断、21 项离线、预检超时、真实设备工具证据、回答超时响应与审计摘录；完整审计在 `real-model/audit/`。

> 失败批次不计签收通过。

- 新密钥诊断 `newkey_20260930_193054/`：保存普通文本／工具耗时、回答校验诊断和完整审计；其下 `audit/` 为完整审计目录。
- 首次校验失败批次 `stage7_20260930_193156_7d74/`：保留失败证据，不覆盖。
- 新密钥最终批次 `stage7_20260930_193510_00e1/`：22 项离线、25 项真实模型联调、正常及空数据回答、审计摘录、模型列表与 `review.json`，判定本机合成范围技术验收通过。
- `stage7_20260930_173842_c74b/`：更早的离线与预检批次。
- `stage7_*/`：按运行编号生成的离线、预检、联调日志与响应。

> `run.json` 不自动签收。运行与阶段笔记：[[06-代码工作区导航与链接映射/stages-07-agent-README|README]] · [[01-项目调研规划与开发建设记录/02-第二版-智能体升级/问答及调度业务主智能体/废案-旧智能体方案/02-功能开发与阶段交付/01-基础服务与只读工具链/05-V2-M1测试与交付]]。

## 十、第二版阶段二 · 告警解读与监控产物

`artifacts/stage7-智能体/v2m2/<运行编号>/` 保存协议用例、真实模型结果、延迟、失败诊断及独立复核。

**协议与真实模型批次**

- `fixture-20261001-r2`：93 项协议用例，仅证明协议能力。
- `real-20261001-final-guard30`：真实模型最终批次，含 30 秒护栏测试。
- `m1-final-regression-r2`：M1 回归批次。
- `server-20261001`：服务器验收批次，含 `observation-final.json` 与 `finish-proof.json`，记录 30 分钟观察、资产恢复和隔离容器停止；公网证据图为 `public-ai-final.jpg`。

> `fixture-20261001-r2` 的 93 项仅证明协议能力；真实模型批次单独标明成功数和校验拒绝数，不能把安全降级计为成功解读。

**私有凭据与服务器持久化**

- `private/v2m2-*.env`、模型密钥、独立监控令牌及测试 JWT 不进入源码包。
- 服务器新增私有文件、审计、PostgreSQL 解读表及 Redis 未消费监控流纳入加密备份；Redis 使用 RDB 备份整库，恢复时必须使用隔离卷。

> 失败批次不清理，运行结论以 [[01-项目调研规划与开发建设记录/02-第二版-智能体升级/问答及调度业务主智能体/废案-旧智能体方案/02-功能开发与阶段交付/02-告警智能解读与大屏联动/05-V2-M2测试与交付]] 为准。这些运行证据保留在忽略目录，不写入发布源码或私有配置说明正文。

## 十一、第二版阶段三 · 知识问答与博查联网产物

`artifacts/stage7-智能体/v2m3/` 是第二版阶段三的本机运行产物根目录，包含实施、博查联网、入口安全与回答文本约束四批次的证据。

### 11.1 实施批次（2026-10-02）

`implementation-20261002/` 保存工作区清单、离线测试、发布包与清单；全部忽略，不入公开仓库。

**子批次**

- `local-real/`：本机真实模型文档与缺口四例。
- `server-acceptance-r5/`：服务器 54/54 检查批次。

**关键稳定文件**

- `agent-tests.log`、`agent-tests-r3.log`、`agent-tests-r4.log`、`agent-tests-r5.log`、`agent-tests-final.log`：Agent 回归各轮日志。
- `backup-final.log`、`restore-final.log`：发布前备份与隔离恢复日志。
- `baseline-status.txt`、`final-worktree-status.txt`：工作区基线与最终状态。
- `boundary-final.json`：跨用户复用会话与路径穿越边界检查结果。
- `browser-miss.png`、`browser-panels.png`、`browser-user.json`：浏览器未命中、面板与临时用户证据。
- `check-delivery.py`、`create-browser-user.py`、`diagnostic.py`、`local-real.py`、`server-boundary.py`：交付检查与诊断脚本。
- `delivery-check.json`：交付检查结果。
- `fault-rollback-r3.log`、`fault-rollback.sh`：故障隔离与回退日志/脚本。
- `finish-proof.json`：完成标志与最终镜像取证。
- `frontend-build-final.log`、`frontend-tests-final.log`、`frontend-tests.log`、`go-tests-final.log`、`knowledge-tests-final.log`：前端、Go 与知识测试日志。
- `local-real.log`：本机真实模型批次日志。
- `observation-r5.json`：1800.53 秒、60 次采样、零失败的最终观察。
- `release-final-manifest.json`、`release-final.tar.gz`、`release-manifest.json`、`release.tar.gz`：发布清单与归档。
- `retrieval-report-final.json`、`retrieval-report.json`：固定检索集报告。
- `source-match.json`：源码逐文件比对结果。

### 11.2 知识缺口持久记录

- `knowledge-misses/misses-20261001.jsonl`、`misses-20261003.jsonl`：本机脱敏缺口 JSONL；日志默认保留九十天。

> 服务器 `runtime/agent-knowledge-misses/`：UID 10001 私有持久挂载；包含于加密备份，不作为公开知识资产。`stages/07-agent/knowledge/index.json`：受控文档构建资产，可入 Git；更改来源后必须重建并检验。

### 11.3 博查增量批次（2026-10-03）

`bocha-20261003/` 保存工作区及防线基线、协议与回归日志、本机真实成功／失败批次、来源审查、取消证明和页面截图。

**子批次**

本机真实联网三轮：

- `local-real-r1/`：r1，第三题被校验拒绝后中断
- `local-real-r2/`：r2，4/5
- `local-real-r3-diagnosis/`：r3，5/5

服务器真实联网三轮：

- `server-real-r1/`：4/5
- `server-real-r2/`：5/5
- `server-real-final/`：final，4/5（风机过热题被原校验拒绝）

**关键稳定文件**

- `agent-tests.log`、`agent-tests-cancel-fix.log`、`agent-tests-final.log`：Agent 回归与取消修复日志。
- `baseline-status.txt`、`final-status.txt`：基线与最终状态。
- `browser-user.json`、`browser-web-evidence.png`：浏览器临时用户与网页来源证据截图。
- `cancel-proof.json`：真实取消留档证明。
- `check-delivery.py`、`delivery-check.json`：交付检查脚本与结果。
- `finish-proof.json`：完成标志。
- `frontend-build.log`、`frontend-tests.log`、`go-tests.log`：前端与 Go 测试日志。
- `key-probe.json`：博查密钥探测结果。
- `local-env-baseline`：本机环境基线目录。
- `main-baseline.py`、`search-baseline.py`：主请求与搜索请求的防线基线快照脚本。
- `observation-final.json`：完整 1800.52 秒、60 次采样、全部通过的最终观察。
- `qa-cleanup.json`：问答清理结果。
- `release-final-manifest.json`、`release-final.tar.gz`、`release-manifest.json`、`release.tar.gz`：发布清单与归档。
- `restore-final.log`、`rollback-restore-final.log`：隔离恢复与回退恢复日志。
- `safety-baseline.json`、`safety-comparison.json`：防线基线与逐字节比对结果。
- `source-review.json`：来源支撑审查结果。

> 服务器对应 `runtime/bocha-20261003T083522Z/`，保存镜像标签、部署、回退恢复、加密备份、隔离恢复与独立完整观察。**旧观察不累计时长**。`artifacts/stage7-智能体/private/agent-search-key` 与服务器 `/etc/powersystem/agent-search-key` 为私有搜索凭据。验收临时操作员凭据也仅放忽略目录，交付后禁用并退出。发布清单只打包明确列出的源码、配置模板、受控索引与笔记，不能包含 JSONL、用户原问题或真实凭据。

### 11.4 独立审查批次（2026-10-03）

`review-20261003/` 保存独立审查脚本与结论。

- `probe_local.py`、`probe_remote.py`：本机与服务器独立审查脚本。
- `server-review.log`：服务器审查日志。
- `第三阶段实现与笔记评估.md`：独立审查对第三阶段实现与笔记的评估文档。

### 11.5 入口安全补强批次（2026-10-03）

`route-fix-20261003/` 保存窄范围受审包、逐文件 SHA256、原校验函数比对、发布与回退脚本。

**关键稳定文件**

- `archive-sha256.txt`：归档逐项哈希。
- `build-notes.py`、`build-package.py`、`check-notes.py`：笔记构建、打包与校验脚本。
- `deploy-route-fix.sh`：部署脚本。
- `final-notes-manifest.json`、`final-notes.tar.gz`：12 篇修订文档清单与归档。
- `manifest.json`：发布清单。
- `orchestrator-before.py`：补丁前 `orchestrator.py` 副本。
- `route-fix-release.tar.gz`：补丁发布归档。
- `sync-notes.sh`：笔记同步脚本。
- `verify-rollback.sh`：回退验证脚本。

> 服务器 `runtime/route-fix-20261003T111105Z/` 保存前后 Agent 镜像、被覆盖源码、严格七题与五题联网的独立结果、实际源码及镜像回退恢复记录。新五题只有 2/5 协议通过；其余拒绝结果和人工来源审查模板独立保留。**前次博查发布的三十分钟观察与隔离恢复属于旧镜像，不能标作本次结果**。

### 11.6 回答文本约束最终批次（2026-10-03）

服务器运行目录 `runtime/route-fix-20261003T124153Z/`，本机沿用 `route-fix-20261003/` 的受审包与脚本，保存严格七题本批 65/65、联网五题 1/5、四题安全拒绝及再次实际回退恢复的记录。

> **2026-10-03 历史批次**：`route-fix-20261003T111105Z/` 目录继续保留其原 67/67、2/5 批次。两者均不继承更早博查镜像的三十分钟观察和隔离恢复结论。当时后续镜像的五题真实联网仅 1/5 协议通过，且该题尚待逐条人工来源审查，正式签收为 0/5；该镜像的三十分钟观察和隔离恢复也尚未完成，因此当时 V2-M3 未签收。当前结论见第十四节。

## 十二、查找与复核办法

**按阶段定位**

先按阶段定位，再按**批次目录名**找 `run.json`/`environment.json` 和 `summary.md`；异常时对照 `.stderr.log`、`events.jsonl`、`reconciliation.json`。

**按阶段四公网部署复核**

要验证阶段四公网部署，先看第十五周笔记的时间和结论，再看 `stage4-前端上线/` 截图/脚本。源码变更以 `stages/` 为准，不要修改 `artifacts/` 的编译副本当作正式开发。

**本机快速列举**

- 列举有稳定名称的阶段四文件：`Get-ChildItem artifacts/stage4-前端上线 -File`
- 列举某次阶段一/二批次时只针对那个运行目录，避免一次递归打印数万条缓存路径。

> 清理前先核对是否还有未转存的模型、SQLite 队列或验收证据。失败批次不得覆盖或合并成通过批次。

## 十三、阶段三人工建议口径历史候选产物（2026-10-04）

本机忽略目录 `artifacts/stage7-智能体/v2m3/manual-advice-20261003/` 保存跨日实施证据：

- `workspace-baseline.tar.gz`、`git-status-baseline.txt`、`file-review-baseline.json`：原 338 文件与已有改动快照，不覆盖原暂存区。
- `repository-review.json`：全库文字、章节与阶段／安全边界审阅索引。
- `validate-answer-baseline.txt`、`safety-baseline-check.json`：原引用／数字函数及工具、搜索、主请求入口比对。
- `agent-final.log`、`validation-results.json`：本机回归与构建结果。
- `manifest.json`、`manual-advice-release.tar.gz`、`archive-sha256.txt`：明确增量清单、源码包及两端核对摘要。该包不含凭据或用户问题。

该日服务器候选目录为 `runtime/manual-advice-20261004T143910Z/`，保留原七题／五题批次、逐条来源暂缓审核、缺口验证、事件快照、实际回退、最终备份及隔离恢复证据。

- 只读观察 `observation-final.json`：1800.52 秒、60 次采样全部通过，三项业务服务健康且重启为零。
- `finish-proof.json`：按完整镜像 ID 核对全部采样。
- 当时实际浏览器仍待已有账号登录，未计为通过。
- 该日候选和更早失败批次独立保留；当前正式候选见第十四节。

新增本机文件：

- `active-run.json`、`safety-baseline-check-final.json`、`agent-r7-final.log`
- `approval-block.json`、`browser-approval-block.json` 和实际登录页截图

随机未激活的测试凭据只在私有运行材料中，不进入源码包或 Git。明确备份授权后已执行更新前和最终备份，自动拒绝历史保留。正式门槛与固定镜像 ID 统一见阶段交付页第十节。

## 十四、当天简短 JSON 候选与页面收尾（2026-10-05）

本机 `artifacts/stage7-智能体/v2m3/manual-advice-20261003/r8/` 保存当天单文件增量包、哈希、源码清单及服务器证据副本；`agent-r8.log` 为 71/71。`browser-final/` 保存真实指南、章节阅读、两处提醒、逐条来源、取消和新会话截图／DOM。

`web-ui-r1` 的 3/5 和中止的 `web-ui-r2` 属昨日镜像，失败不混入通过结果；`web-ui-r3` 原五题完整 5/5 属当天固定候选。`ui-checks.json` 标记批次、请求编号、候选和实际核查结果。读取外站内部错误页被浏览器策略阻止，未绕过，实际来源核查留待专业人员。

当天服务器目录 `runtime/manual-advice-20261005T004409Z/`，收尾证据位于 `browser-closeout-20261005/`：UI 对应持久缺口与审计、逐条来源哈希、最终加密备份、隔离恢复及本镜像观察。私有审计、恢复日志和未激活测试材料仍只放忽略运行目录，不进发布包或 Git。

当天最终证据已封存：`r8/finish-proof.json` 为签收、观察摘要和固定镜像依据，`r8/server/browser-closeout-20261005/` 保存最终备份、隔离恢复、真实页面缺口及完成后的观察。`final-notes-proof.json` 记录十六份公开笔记同步后代码及镜像不变。主 `active-run.json`／`validation-results.json` 指向当天 R8；昨日 R7 汇总另存历史副本。失败批次与专业来源待复核状态保留，不将缺口留档等同于补库。

## 十五、第二版第四阶段实施（2026-10-06）

> V2-M4 在合成演示与公开资料辅助建议范围正式技术签收，发布版本 v2.0.0。当前方案、测试与问题见 [[01-项目调研规划与开发建设记录/02-第二版-智能体升级/问答及调度业务主智能体/废案-旧智能体方案/02-功能开发与阶段交付/04-测试、安全复核与交付/00-索引·测试、安全复核与交付]]。

本轮证据进入忽略的 `artifacts/stage7-智能体/v2m4/` 和服务器 `runtime/v2m4-<运行编号>/`。

**固定批次**

- 本机固定批次 `v2m4_20261006_012208`，服务器 `runtime/v2m4-20261005T180928Z`。

**交付账本**

- `final-delivery/delivery-ledger.json`：最终证据账本，以 SHA256 绑定。
- `final-delivery/delivery-summary.json`：十五项全部 `passed=true`。
- 旧 `delivery-summary.json` 因当时缺少 browser 门槛仍保留 `passed=false`，不覆盖历史。

**本机回归批次**

- `local-suite-r3/`：完整基础回归。
- `agent-final.log`：最终 79 例。
- `main-chain-r8/`：完整 1140 项；`main-chain-r1～r7` 失败／中断保留。
- `real-interpretations-r1/`：30 例。
- `source-sampling.json`、`performance-r1.json`：技术抽查与计时。
- `source-access-r1/r2.json`、`knowledge-gap-summary.json`：只作可达性与缺口归档，不作专业审核。

**服务器证据**

- `server-release-evidence/`：严格七题、原五题联网、三类事件和实际回退。
- `observation-final.json`：最终 60 次观察。
- `final-restore.log` 及两端 `*.sha256`：加密备份恢复。
- `final-source-proof.json`：最终公开源码文件摘要及不变运行资产判定。
- `server-security-inventory.json`、`server-security-inventory.txt`：服务器安全盘点。
- `r730-external-check.txt`、`public-external/`：R730xd 与公网外部检查。
- `run-identity.json`：本轮运行身份与证据绑定。

> 私有配置、JWT、队列、审计和恢复数据不进入 Git 或 Release。

**页面与发布验证**

- `browser-page-proof.png`、`browser-r1/r2/r3.json`：真实页面证据（r1～r3 演进）。
- `browser-audit-final.json`：本轮真实页面、回答／取消审计及来源哈希。
- `notification-group-confirmation.json`：所有者群内可见确认。
- `candidate-audit-r1/r2/r3/final/closeout/release.json`：候选逐项审计，最终版本为 `candidate-audit-release.json`。
- `package-r1/`、`package-final/`、`package-release/`：发布候选打包批次（candidate.json + candidate.tar.gz）。
- `release-assets/powersystem-v2.0.0-reviewed.tar.gz`、`powersystem-v2.0.0-manifest.json`、`SHA256SUMS`：v2.0.0 发布资产（受审源码包 + 清单 + 校验和）。
- `published-release-verification.json`、`release-server-verification.json`、`release-notes.md`、`tag-message.txt`：发布后验证（GitHub Release / 服务器 / 标签）。

**收尾证据**

- `closeout-restore.log`、`closeout-backup-*.sha256`：最终收尾备份恢复。
- `observation-closeout.json`、`knowledge-gap-closeout-summary.json`、`final-source-proof-release.json`：最终收尾观察、缺口与源码证明。

> 不以 M3 证据替代本轮。

## 十六、远程知识缺口查看器的本机产物（2026-10-06）

- `artifacts/stage7-智能体/private/查看远程知识缺口.cmd`：作者本机双击入口，包含本机连接参数；由 Git 忽略，不随仓库或分享包提供。运行时调用仓库中的通用 `stages/07-agent/scripts/view_remote_knowledge_misses.py`，私钥只从本机现有路径读取，不复制到入口或报告。
- `artifacts/stage7-智能体/private/reports/knowledge-misses.html`：源码版入口生成的本机私有报告。每次成功读取后覆盖；连接或解析失败时先移除旧报告，不将旧数据标成新结果。报告可搜索、按审核状态筛选，去重问题可展开逐条记录。报告可能含脱敏未识别的自由文本，不能进入 Git 或公开知识库。
- `artifacts/stage7-智能体/distribution/PowerSystem-Knowledge-Misses.exe`：可分发的 Windows 64 位单文件程序；`使用说明.txt` 与 `PowerSystem-Knowledge-Misses-share.zip` 是给收件人的说明和仅含程序、说明的分享包。程序不内置服务器地址、私钥、口令或知识缺口数据；收件人须自行具备 SSH 授权与主机密钥记录。`distribution/build/`、`.spec` 为可再生成的 PyInstaller 构建产物，不需分享。
- 分发版在收件人的 `%LOCALAPPDATA%/PowerSystem/knowledge-misses/knowledge-misses.html` 生成并覆盖私有报告，不写入仓库。服务器记录仍由运行中 Agent 的 `/knowledge-misses` 实际 bind source 读取；默认宿主目录是 `/opt/powersystem/runtime/agent-knowledge-misses/`，不因查看器而复制、清理或修改服务器数据。

上述本机入口、报告和分发包均位于 Git 忽略的 `artifacts/`；分享前仅取 `PowerSystem-Knowledge-Misses-share.zip`，不要将整个 `private/` 或 `distribution/` 目录打包。使用方式与权限边界见 [[06-代码工作区导航与链接映射/stages-07-agent-README|README]]、[[02-工程规范边界与验收治理/项目工程规范/07-能力数据与服务器边界]]。

## 十七、自然语言问答本地候选（2026-10-09）

`artifacts/stage7-智能体/natural-20261009/` 保存本轮隔离配置、Redis/TDengine 配置、模拟器队列、审计和知识缺口；这些材料可能含秘密或自由文本，全部由 Git 忽略。

- `test-r1～r4/`：Agent 与后端演进批次；失败报告保留，最终后端/前端套件用 `local-final-r2/`，最终 Agent 单独日志为 `agent-final-r7.log`。
- `real-r1/`、`real-r2/`、`real-r3/`：12 题初测、25 题完整初测和 9 题修复复测；不得将原失败批次标为全通过。
- `real-final/`、`real-final-r2～r6/` 和最终 `real-delivery/`：逐轮修复后的完整真实模型题集，逐题保存答案、断言及实际工具审计。
- `fault-r1.json`、`fault-delivery.json`：独立主模型不可达 Agent 的实际业务直答与开放式故障测试。
- `model-probe.log`、`credential-scan-*.json`：真实能力探针及只输出位置/计数的秘密扫描。
- `runtime/audit/`、`runtime/misses/`、`browser/`：本轮审计、知识缺口及实际页面证据。
- `baseline/`、`source-manifest.json`、`delivery-summary.json`：本机源码基线、候选指纹及结果汇总，不代表服务器镜像回退。

测试 PostgreSQL 使用专用 `powersystem_natural_20261009` 数据库，Redis/TDengine 使用本轮命名的独立容器及回环端口 16380/16041；旧库与历史卷不清理。本轮 Web 为回环 5173，未新增公网端口。
