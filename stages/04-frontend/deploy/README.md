---
tags:
  - 源网智联
  - 开发阶段4
  - 部署
---

# 第 15 周服务器部署

> 2026-10-14当前：SEC-M4本机限定签收及r6追加准入通过；MA-C32～C35和最终文档门槛已完成，MA-M4及联合阶段四在声明范围正式技术签收。服务器r6/v5、恢复维护修订3；本机唯一批准动作保留，写窗口关闭，生产仅登记R0。

> **2026-10-11现行问答增量**：指定公网入口为`https://8.138.42.212/agent`。阶段8已接管通用问答，生产使用阶段4主编排、阶段7 Agent/搜索、阶段8生产编排及发布目录固定镜像文件；旧告警解读继续由阶段7运行。现行操作见[[notes/01-项目调研规划与开发建设记录/第二版-智能体升级/问答及调度业务主智能体/当前方案-Agent与RAG重建/02-技术选型与架构设计/04-服务器部署与回退]]，正式结果见[[notes/01-项目调研规划与开发建设记录/第二版-智能体升级/问答及调度业务主智能体/当前方案-Agent与RAG重建/03-功能开发与阶段交付/04-完整Agent与会话/05-RAG服务器发布测试与交付]]。以下初装步骤不能直接用于覆盖当前运行配置。

> 本目录编排 R730xd 上的阶段四演示环境。上级总索引见 [README.md](../README.md)，阶段笔记见 [04-第15周·部署上线](../../../notes/01-项目调研规划与开发建设记录/第一版-平台主体/05-功能开发与阶段交付/04-前端、上线与沉淀/04-第15周·部署上线.md)。数据库与模拟器使用新的持久卷，不迁移本机验收库。模型文件 `model.json`、`metadata.json` 来自本机被 Git 忽略的 `artifacts/stage3-AI预测/model/`，上线时另行传至服务器 `runtime/model/`。

## 一、目录与配置

服务器项目目录为 `/opt/powersystem`，私有配置由 `init-server.sh` 首次生成到该目录 `.env`。脚本不会覆盖现有配置。数据库、JWT、Grafana 密码只存在于服务器私有文件中；备份口令另存 `/etc/powersystem/backup-passphrase`，业务通知令牌另存 `/etc/powersystem/business-notify-token`。飞书群机器人地址与签名密钥由运维人员写入 `/etc/powersystem/feishu-webhook-url`、`/etc/powersystem/feishu-sign-secret`，均设 `chmod 600`。三个通知文件不写入 `.env`、命令历史、仓库或笔记；**首次 `compose config` 和 `up` 前须准备好文件**。Compose 把令牌挂给 API 与适配器，把机器人配置只挂给适配器。第一版阶段五同时包含业务事件和平台监控通知，见[阶段五运行说明](../../05-feishu-notification/README.md)。

从服务器项目目录执行：

```bash
bash stages/04-frontend/deploy/init-server.sh
docker compose --env-file .env -f stages/04-frontend/deploy/compose.yaml config --quiet
docker compose --env-file .env -f stages/04-frontend/deploy/compose.yaml up -d --build
docker compose --env-file .env -f stages/04-frontend/deploy/compose.yaml ps
curl -fsS http://127.0.0.1:18080/api/v1/ping
```

更新阶段四 P0 后，先在服务器备份当前数据与配置，再验证 `003_energy.sql` 迁移、Compose 配置和镜像构建。管理员用 `bash stages/04-frontend/deploy/bootstrap-admin.sh <新账号名>` 在服务器本地创建；脚本交互读取并确认口令，口令只经标准输入交给 API 容器。不要把已有演示 `operator` 提升为管理员。新电量汇总由 API 后台依据已处理 inbox 逐日回填；用原始功率样本核对汇总后再记录验收。

前端仅绑定 R730xd 的 `127.0.0.1:18080`。本项目 FRP 客户端没有对外映射 PostgreSQL、TDengine、Redis、EMQX、AI、Prometheus 与 Grafana；共享云主机其它 FRP 客户端监听端口另见 P0 收尾记录。API 的 `/metrics` 只供 Compose 内部 Prometheus 抓取。Grafana 可通过 SSH 本地转发 `13000` 访问。

## 二、FRP 入口

只在 R730xd 现有 `/opt/frp/frpc.toml` 追加下面一条代理；保留其他代理与云端 `frps` 配置。更新前备份原配置，使用现有版本的 `frpc verify -c` 校验，重启 `frpc` 后核对旧代理仍可用。

```toml
[[proxies]]
name = "powersystem-stage4-web"
type = "tcp"
localIP = "127.0.0.1"
localPort = 18080
remotePort = 3667
```

旧公网演示地址为 `http://8.138.10.222:3667`。2026-09-29 第一版现场已为 `8.138.10.222:443` 安装可信 IP 证书：云端 [cloud-ip-https.conf](cloud-ip-https.conf) 在 Nginx 终止 TLS，向本机 FRP 端口转发；[cloud-public-firewall.service](cloud-public-firewall.service) 拦截外部直连 3667；[cloud-cert-renew.timer](cloud-cert-renew.timer) 定时续期。外部证书校验和 `/api/v1/ping` 成功，旧 HTTP 直连超时。域名不要求。更换云主机、IP 或续期方式后须重新核对证书、Nginx、IPv4/IPv6 防火墙和 WebSocket，不能只复制此配置就视为安全通过。现场明细见[阶段六测试与交付](../../../notes/01-项目调研规划与开发建设记录/第一版-平台主体/05-功能开发与阶段交付/06-公网安全与恢复能力/03-阶段六测试与交付.md)。

## 三、监控、备份与恢复

Prometheus 每 15 秒采集 API 和主机指标，规则包含 API 不可用及磁盘使用率超过 80%；已移除需要宿主机高权限挂载的 cAdvisor。Grafana 看板由本目录配置自动装载。阶段五的设备/AI 业务事件由 PostgreSQL 投递表交给内网适配器，失败会重试；平台监控事件仍由 Alertmanager 发送。2026-09-29 旧 API 暂停/恢复与新业务链路均已核对，见[阶段五测试与交付](../../../notes/01-项目调研规划与开发建设记录/第一版-平台主体/05-功能开发与阶段交付/05-飞书告警通知/03-阶段五测试与交付.md)。

两台服务器 SSH 安全基线按“盘点现况 → 安装并验证公钥 → 保持第二管理会话 → `sshd -t` 校验 → 关闭 root 密码登录 → 从新会话复测”的顺序执行。保留原会话直至新会话和应急入口验证成功；复核 FRP、备份专用 SFTP 和原有代理不受影响。本轮按此顺序完成，两端 root 仅允许公钥；未来如要改用非 root 管理员，应先验证其 sudo 和应急访问。现场结果记入 [P0 收尾记录](../../../notes/01-项目调研规划与开发建设记录/第一版-平台主体/05-功能开发与阶段交付/04-前端、上线与沉淀/06-P0收尾与M4验收记录.md)。

云服务器使用 SFTP 专用账号 `powersystem-backup` 保存密文，R730xd 的私钥路径为 `/etc/powersystem/backup-ed25519`。执行 `backup.sh` 前须显式设置 `POWERSYSTEM_BACKUP_TARGET` 或私有 `/etc/powersystem/backup-target`；脚本不含公开默认目标。备份包括 PostgreSQL、TDengine、网关与模拟器 SQLite 队列、模型、`.env` 与三个通知私有文件，整体以 GPG AES256 加密后传至接收端。备份口令须在 R730xd 之外另行保管，不包含在密文包内。现场已核对专用 SFTP 目录、更新前后两份密文的异地 SHA256、独立恢复数据与私有文件；`powersystem-backup.timer` 已启用。2026-09-29 项目所有者已反馈完成服务器外独立离线口令副本及 `MATCH` 核对，见 [阶段六测试与交付](../../../notes/01-项目调研规划与开发建设记录/第一版-平台主体/05-功能开发与阶段交付/06-公网安全与恢复能力/03-阶段六测试与交付.md)。

第二云接入时可另设 `/etc/powersystem/backup-secondary-target` 或 `POWERSYSTEM_BACKUP_SECONDARY_TARGET`，脚本将同一密文上传两处；任一上传失败均保留本地密文并返回失败。第二云专用 FRP 客户端、备份接收端和域名待备案配置见[独立云服务器部署](../../../notes/04-系统部署发布备份与变更记录/独立云服务器部署/00-索引·独立云服务器部署.md)，旧共享 FRP 不在此次变更范围。

恢复验证只启动带独立卷的 `powersystem-restore-*` Compose 项目，不覆盖生产数据库：

```bash
bash stages/04-frontend/deploy/backup.sh
bash stages/04-frontend/deploy/restore-verify.sh /var/backups/powersystem/powersystem-<UTC时间>.tar.gz.gpg
```

恢复脚本输出隔离项目名、数据库行数、队列文件数及私有文件校验结果。新版本现场结果记录到第一版 M4 复验页。手动清理隔离演练资源前先确认其项目名，不能对生产 Compose 执行 `down -v`。

## 四、发布检查

每次更新先在本地运行前端、Go、Python 测试，备份当前数据，记录镜像版本；在服务器执行 Compose 配置校验和构建。上线后检查管理员/操作员权限、总览电量与健康分、设备实时值、告警确认、预测、WebSocket、Grafana、飞书通知和 `https://8.138.10.222/` 入口，再观察运行日志。回退时恢复上一版本源码和镜像，保持数据卷与备份不动；如新版本迁移了数据库，回退前核对旧版本是否能读取新增表。

## 五、V2-M2可选解读链路

使用 `stages/07-agent/deploy/agent.compose.yaml` 叠加基座并启用 `agent` profile。先备份，再配置独立服务/监控文件和审计目录，执行 `005_agent_interpretations.sql` 兼容迁移。Go 不依赖 Agent 健康而启动，Agent 不新增公网端口，Nginx 显式拒绝 `/internal/`。

独立监控令牌由 Agent 与 Alertmanager 读取；文件属组 `10001`、权限 `440`，Alertmanager 仅增加该附加组。模型及服务令牌权限 `400`。父目录保持仅管理员可访问，不把令牌内容放入 Compose 或命令输出。

新版备份在发现 Agent 服务凭据后增加三个私有文件、审计及完整 Redis RDB；PostgreSQL 全库备份自然包含新增任务、结果与已读表。恢复验证使用独立 Redis 卷，旧备份不要求存在新文件。回退关闭新增链路并恢复原镜像及 Alertmanager 配置，保留新增表和业务卷；禁止生产 `down -v`。

详细操作和实际验收见[阶段二部署笔记](../../../notes/01-项目调研规划与开发建设记录/第二版-智能体升级/问答及调度业务主智能体/废案-旧智能体方案/02-功能开发与阶段交付/02-告警智能解读与大屏联动/04-服务器部署与故障隔离.md)。目前源码方案已实现，服务器切换状态以测试与交付笔记为准。

## 多智能体发布规划入口（2026-10-12历史）

后续阶段9部署/恢复设计见[[notes/01-项目调研规划与开发建设记录/第二版-智能体升级/多智能体协作建设/02-技术选型与架构设计/06-部署、观测、备份与回退]]。当前只是规划，未新增Compose服务、端口、数据库或发布动作；生产继续使用本README现行入口。新服务只在项目内网，沿用prepare/build/activate/rollback和实际备份隔离恢复，本次不提供可执行的新增生产命令。

## 五分区目录兼容（2026-10-13本机修复）

backup.sh读取资产/设备知识/手册来源清单.json，尚未迁移的服务器仅在新清单不存在时回退旧设备知识路径。实际私有PDF、模型、审计和发布目录继续使用服务器runtime及配置中的真实挂载源；本机资产/runtime仅为历史压缩包归档。

本轮只有本地路径选择、Shell语法和离线fixture检查，没有执行备份、SSH、Docker操作或服务器更新。详见[[notes/04-系统部署发布备份与变更记录/部署与运维日志/02-目录重构兼容修复]]。

## 2026-10-11 BND-M2增量发布

BND-M2已在R730XD验收发布，公网入口8.138.42.212。本机及最终Linux Agent98项、前端19项、类型构建和Go全包通过；最终公网11类/19项、重启历史10项、实际公网浏览器9项、源码/镜像实际回退、最终加密备份隔离恢复及300秒固定镜像观察通过。五页阶段二笔记已同步，未提交或推送Git。正式证据见[[notes/01-项目调研规划与开发建设记录/第二版-智能体升级/问答及调度业务主智能体/智能体边界建设/04-功能开发与阶段交付/02-日常问题调用边界/03-阶段2测试与交付]]；指定入口https://8.138.42.212/agent，业务仍在R730XD。只切换API、前端和通用Agent，其他业务容器及持久卷保持原基线。更新前和最终本地加密包均完成摘要核对，最终包完成独立隔离恢复；本轮使用LOCAL_ONLY，不把此结果写成新云异地上传完成。

备份Agent配置时沿当前compose环境中实际RAG_ENV_FILE读取，禁止假设与compose.env同目录。增量发布及验收命令统一见[[stages/08-agent-rag/README]]。

### MA-M4隔离恢复兼容与失败重跑

业务库dump可包含诊断账号的ACL；隔离验证用--no-owner/--no-privileges导入数据和结构，不复制生产账号权限。恢复角色/执行凭据仍由专用恢复档限制；这不用于直接覆盖生产数据库。失败证据和卷保留，第二参数rN选择新目录：

```bash
bash /opt/powersystem/stages/04-frontend/deploy/restore-verify.sh /var/backups/powersystem/<已核验密文>.tar.gz.gpg r2
```

任务恢复另由阶段9restore-ma.py验证14张表、报告与queued/approved/outbox不执行，不重放生产批准记录。旧阶段7恢复样例仅在隔离环境验证其既有监控流，实际通知关闭。

## 备份恢复后Nginx上游核对

> MA-M4实际备份停止/恢复API后，Docker IP变化而前端Nginx仍缓存旧上游。六服务健康不能替代公共入口及WSS实测；先核对实际API地址和前端错误，再校验并重载配置。以下只重载现有Nginx，不更换镜像或重建容器；须在固定镜像观察开始前处理。

#### 步骤1 · 核对当前API容器地址

```bash
docker inspect powersystem-stage4-api-1 --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'
docker exec powersystem-stage4-frontend-1 getent hosts api
```

#### 步骤2 · 校验配置并重载既有前端

```bash
docker exec powersystem-stage4-frontend-1 nginx -t
docker exec powersystem-stage4-frontend-1 nginx -s reload
```

配置校验失败时停止，不以reload退出码判定恢复。核对API/前端容器ID、镜像与静态资产版本保持，使用既有可信HTTPS入口验证ping200，并在本人已登录页面检查WSS实时连接正常；模型请求502另按模型链故障取证。维护修订3补单/双/三参数恢复入口计数校验，隔离恢复十项结果及失败保留见[[notes/01-项目调研规划与开发建设记录/第二版-智能体升级/多智能体协作建设/06-联合发布验收/02-服务器发布与恢复验收/03-MA-M4测试与交付]]。

## 2026-10-16 · 回答可读性增量发布

[[notes/01-项目调研规划与开发建设记录/第二版-智能体升级/问答及调度业务主智能体/当前方案-Agent与RAG重建/04-增量优化与补充交付/01-回答可读性与任务结果展示/03-测试与交付]]记录本批实际结果。该批生产指针assistant-readability-20261016-r1，通过本页唯一入口及双HTTPS/WSS门禁；直接上一版本interpretation-layout-20261016-r1保留，仅frontend重建。既有Executor同ID、同镜像登记重载，API及其他镜像保持。免重复测试不跳过必要构建和发布门禁，实际rollback未触发。

## 相关笔记

第三阶段发布使用[阶段七发布脚本与命令](../../07-agent/README.md)，统一备份同时保存受控知识索引、未命中问题持久目录与可选搜索密钥；恢复核对索引哈希与 JSONL 格式。实际镜像、回退和观察结果见[第三阶段交付](../../../notes/01-项目调研规划与开发建设记录/第二版-智能体升级/问答及调度业务主智能体/废案-旧智能体方案/02-功能开发与阶段交付/03-使用引导与值班问答/05-V2-M3测试与交付.md)。

- 阶段四总索引：[README.md](../README.md)
- 运维与使用：[平台运维与二次开发手册](../../../notes/03-平台使用运维与二次开发手册/平台运维与二次开发手册.md) · [平台使用手册](../../../notes/03-平台使用运维与二次开发手册/平台使用手册.md)
- 部署证据：[04-第15周·部署上线](../../../notes/01-项目调研规划与开发建设记录/第一版-平台主体/05-功能开发与阶段交付/04-前端、上线与沉淀/04-第15周·部署上线.md) · [05-运维与恢复](../../../notes/01-项目调研规划与开发建设记录/第一版-平台主体/05-功能开发与阶段交付/04-前端、上线与沉淀/05-运维与恢复.md)
- P0 收尾：[06-P0收尾与M4验收记录](../../../notes/01-项目调研规划与开发建设记录/第一版-平台主体/05-功能开发与阶段交付/04-前端、上线与沉淀/06-P0收尾与M4验收记录.md)
- M4 测试与交付：[07-M4测试与交付](../../../notes/01-项目调研规划与开发建设记录/第一版-平台主体/05-功能开发与阶段交付/04-前端、上线与沉淀/07-M4测试与交付.md)
- 飞书通知：[阶段五运行说明](../../05-feishu-notification/README.md) · [阶段五测试与交付](../../../notes/01-项目调研规划与开发建设记录/第一版-平台主体/05-功能开发与阶段交付/05-飞书告警通知/03-阶段五测试与交付.md)
- 安全恢复：[阶段六运行说明](../../06-security-recovery/README.md) · [阶段六测试与交付](../../../notes/01-项目调研规划与开发建设记录/第一版-平台主体/05-功能开发与阶段交付/06-公网安全与恢复能力/03-阶段六测试与交付.md)

## 2026-10-15 前端 UI 增量发布

深空值班台UI及四项校对修正已在指定HTTPS入口发布。前端固定镜像、真实旧版回退/同候选恢复、HTTPS/WSS、备份隔离恢复及300.7秒观察通过；其他18个原业务服务保持基线，已有Executor仅为资产登记读取受控重启、镜像与容器ID未变。后台r6/v5、生产R0与关闭写窗口不变。原阶段签收和模型质量边界保留，不据本次UI发布重新声明全版本验收。详见[[notes/04-系统部署发布备份与变更记录/部署与运维日志/07-前端UI深空值班台上线记录]]。

本批只使用当前完整编排最后叠加前端镜像固定文件，不用初装命令覆盖现有运行配置。已留存的本批操作如下；仅适用于该批次，回退前确认任务已排空，并在操作后同步实际状态文档：

```bash
python3 /opt/powersystem/runtime/ui-20261015-r1/controller-r3.py baseline
python3 /opt/powersystem/runtime/ui-20261015-r1/controller-r3.py accept-rollback
python3 /opt/powersystem/runtime/ui-20261015-r1/controller-r3.py candidate
python3 /opt/powersystem/runtime/ui-20261015-r1/controller-r3.py accept-final
```

脚本只重建前端并维护已有前端资产身份；既有资产加载函数会受控重启同一Executor容器。文档与备份证据独立保留，不删除业务卷，不变更其他智能体镜像。后续发布应先从运行容器标签获取实际编排路径，不能遗漏本批前端固定镜像叠加文件。

## 2026-10-15 API恢复后的生产维护入口

> 以下是API恢复当时的历史操作定位。当前应用发布和回退统一使用本页下一节的`production-stack.py`；不得直接重放历史切换命令覆盖现网。

已部署环境不得使用本页初装的单份基础compose重建API。生产必须包含阶段4、阶段7 Agent/search、阶段8和阶段9叠加，以及当前各服务固定镜像；实际环境文件从健康agent-rag标签读取。先核对[[notes/04-系统部署发布备份与变更记录/部署与运维日志/06-API版本回退恢复记录]]。

本次已执行的完整参数快照为`/opt/powersystem/runtime/api-recovery-20261015-r1/compose-args.json`，其中末层镜像锁定保留r7前端/通知、r8模拟器及r6智能体。只读检查编排服务：

```bash
python3 - <<'PY'
import json, subprocess
from pathlib import Path
args = json.loads(Path('/opt/powersystem/runtime/api-recovery-20261015-r1/compose-args.json').read_text())
subprocess.run(args + ['config', '--services'], check=True)
PY
```

该快照只适用本批，不能直接覆盖未来发布。API维护前做本地加密备份、固定镜像/配置和排空核对；限定`up -d --no-deps --no-build --pull never --force-recreate --wait api`后还需现有Nginx语法检查/reload、已有API资产身份刷新与同ID Executor登记重载。不得清卷或把故障旧镜像当正常回退目标。

发布后的回归入口已增加历史会话、运维任务及非空列表详情，不会输出账号密码/JWT/ticket：

```bash
python3 /opt/powersystem/runtime/api-recovery-20261015-r1/verify-auth-wss.py /etc/powersystem/demo-password
```

脚本源码为`deploy/edge-server/独立云服务器部署/scripts/check-auth-wss.py`，执行前同步已审查的同版脚本。该检查不调用模型；真实模型连接需独立验收并明确范围。

## 生产发布唯一入口（2026-10-15）

设计原理、逐条要求与审查边界见[[notes/02-工程规范边界与验收治理/生产发布防回退与自动验收门禁规范]]；本页只维护具体操作命令和参数。

长期约束统一见[[notes/02-工程规范边界与验收治理/项目工程规范/02-发布仓库与界面验收规范#生产发布防回退与自动验收门禁（2026-10-15，强制）]]。入口为`/opt/powersystem/stages/04-frontend/deploy/production-stack.py`，状态目录为`/opt/powersystem/runtime/production-stack/`，仅在指定R730XD以root运行。

### 当前基线与只读查看

2026-10-18白天循环与故障注入解耦已通过统一入口发布；当前指针 `daylight-fault-decouple-20261018-r1`，上一版 `interpretation-role-20261018-r1` 保留，未触发rollback。仅指定 simulator，私有完整编排参数追加仅含模拟器命令的覆盖层，显式 `--fault-mode off` 并保留60分钟白天循环；不改已验收冻结文件和其他服务配置。原卷、账本、历史及模型阈值保持，必要构建、10项隔离检查、75个合成窗口、更新前双云密文备份、正式门禁及发布后一轮正常预测通过。事实统一见[[notes/01-项目调研规划与开发建设记录/补充业务/02-白天循环模拟模式/01-需求与实现·02-白天循环模拟模式]]，不另建笔记。当前指针及待恢复事务由下列命令读取：

```bash
python3 /opt/powersystem/stages/04-frontend/deploy/production-stack.py status
```

### 后续发布

先独立构建并审查新镜像。`prepare`只解析配置、读取Docker元数据和冻结候选；可传递已构建镜像标签，工具会解析为实际镜像ID。以下新运行编号与镜像标签须替换，不能原样当成现成版本：

```bash
python3 /opt/powersystem/stages/04-frontend/deploy/production-stack.py prepare \
  --id release-YYYYMMDD-rN --image api=reviewed-api:release-YYYYMMDD-rN
```

同批其他镜像使用重复`--image frontend=...`等参数；仅配置变更使用`--service api`等显式指定应用。需要新的集成环境文件时，传入受审完整Compose参数JSON的`--from-args`，仍须包含原顺序的五份基础编排。已接受版本引用的私有文件不可原位修改，应使用新路径准备候选；数据库迁移、公开端口、持久卷变化不在此入口范围。

候选准备完成后新建并核对更新前加密备份再发布，`--backup`必须是业务机既有备份目录内、候选准备后且24小时内生成、附有匹配SHA256的密文。发布会自动执行门禁，成功才移动当前指针：

```bash
python3 /opt/powersystem/stages/04-frontend/deploy/production-stack.py publish \
  --id release-YYYYMMDD-rN \
  --backup /var/backups/powersystem/powersystem-YYYYMMDDTHHMMSSZ.tar.gz.gpg
```

该入口只重建指定应用，使用冻结JSON及全部固定镜像；自动检查Nginx并更新已登记容器身份，通过同ID Executor重载刷新登记。真实操作员凭据仅从`/etc/powersystem/demo-password`读取，不输出密码、JWT或ticket。无`--skip-test`、`--force`或跳过门禁选项。

### 回退及中断恢复

发布门禁失败会自动回退并重新验收。进程中断或自动回退失败时，`pending.json`保留，后续发布被拒绝；不要删除该标记。下列命令优先完成未结束事务；没有未结束事务时回到当前版本的直接上一份已验收版本：

```bash
python3 /opt/powersystem/stages/04-frontend/deploy/production-stack.py rollback
```

回退保留所有业务数据和失败证据，不降级数据库。旧私有引用缺失或被改写时停止，由维护者先恢复相应材料，不用新凭据或新配置假冒旧版。未来门禁覆盖HTTPS/WSS与配置一致性，不主动调用模型；模型、容量、固定版本观察等专项按变更范围另行执行。

> 发布防回退的业务背景与流程见 [[notes/01-项目调研规划与开发建设记录/补充业务/05-生产发布防回退/01-设计与发布流程]]。具体运行命令继续在源码 README 维护，原阶段签收范围不变。
