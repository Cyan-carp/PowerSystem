---
tags:
  - 源网智联
  - 开发阶段4
  - 部署
---

# 第 15 周服务器部署

> 2026-10-14当前：SEC-M4本机限定签收及r6追加准入通过；MA-C32～C35和最终文档门槛已完成，MA-M4及联合阶段四在声明范围正式技术签收。服务器r6/v5、恢复维护修订3；本机唯一批准动作保留，写窗口关闭，生产仅登记R0。

> **2026-10-11现行问答增量**：指定公网入口为`https://8.138.42.212/agent`。阶段8已接管通用问答，生产使用阶段4主编排、阶段7 Agent/搜索、阶段8生产编排及发布目录固定镜像文件；旧告警解读继续由阶段7运行。现行操作见[[内容/第二版-智能体升级/问答及调度业务主智能体/当前方案-Agent与RAG重建/技术选型与架构设计/04-服务器部署与回退]]，正式结果见[[内容/第二版-智能体升级/问答及调度业务主智能体/当前方案-Agent与RAG重建/开发阶段4-完整Agent与会话/05-RAG服务器发布测试与交付]]。以下初装步骤不能直接用于覆盖当前运行配置。

> 本目录编排 R730xd 上的阶段四演示环境。上级总索引见 [README.md](../README.md)，阶段笔记见 [04-第15周·部署上线](../../../内容/第一版-平台主体/开发阶段4-前端、上线与沉淀/04-第15周·部署上线.md)。数据库与模拟器使用新的持久卷，不迁移本机验收库。模型文件 `model.json`、`metadata.json` 来自本机被 Git 忽略的 `artifacts/stage3-AI预测/model/`，上线时另行传至服务器 `runtime/model/`。

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

旧公网演示地址为 `http://8.138.10.222:3667`。2026-09-29 第一版现场已为 `8.138.10.222:443` 安装可信 IP 证书：云端 [cloud-ip-https.conf](cloud-ip-https.conf) 在 Nginx 终止 TLS，向本机 FRP 端口转发；[cloud-public-firewall.service](cloud-public-firewall.service) 拦截外部直连 3667；[cloud-cert-renew.timer](cloud-cert-renew.timer) 定时续期。外部证书校验和 `/api/v1/ping` 成功，旧 HTTP 直连超时。域名不要求。更换云主机、IP 或续期方式后须重新核对证书、Nginx、IPv4/IPv6 防火墙和 WebSocket，不能只复制此配置就视为安全通过。现场明细见[阶段六测试与交付](../../../内容/第一版-平台主体/开发阶段6-公网安全与恢复能力/03-阶段六测试与交付.md)。

## 三、监控、备份与恢复

Prometheus 每 15 秒采集 API 和主机指标，规则包含 API 不可用及磁盘使用率超过 80%；已移除需要宿主机高权限挂载的 cAdvisor。Grafana 看板由本目录配置自动装载。阶段五的设备/AI 业务事件由 PostgreSQL 投递表交给内网适配器，失败会重试；平台监控事件仍由 Alertmanager 发送。2026-09-29 旧 API 暂停/恢复与新业务链路均已核对，见[阶段五测试与交付](../../../内容/第一版-平台主体/开发阶段5-飞书告警通知/03-阶段五测试与交付.md)。

两台服务器 SSH 安全基线按“盘点现况 → 安装并验证公钥 → 保持第二管理会话 → `sshd -t` 校验 → 关闭 root 密码登录 → 从新会话复测”的顺序执行。保留原会话直至新会话和应急入口验证成功；复核 FRP、备份专用 SFTP 和原有代理不受影响。本轮按此顺序完成，两端 root 仅允许公钥；未来如要改用非 root 管理员，应先验证其 sudo 和应急访问。现场结果记入 [P0 收尾记录](../../../内容/第一版-平台主体/开发阶段4-前端、上线与沉淀/06-P0收尾与M4验收记录.md)。

云服务器使用 SFTP 专用账号 `powersystem-backup` 保存密文，R730xd 的私钥路径为 `/etc/powersystem/backup-ed25519`。执行 `backup.sh` 前须显式设置 `POWERSYSTEM_BACKUP_TARGET` 或私有 `/etc/powersystem/backup-target`；脚本不含公开默认目标。备份包括 PostgreSQL、TDengine、网关与模拟器 SQLite 队列、模型、`.env` 与三个通知私有文件，整体以 GPG AES256 加密后传至接收端。备份口令须在 R730xd 之外另行保管，不包含在密文包内。现场已核对专用 SFTP 目录、更新前后两份密文的异地 SHA256、独立恢复数据与私有文件；`powersystem-backup.timer` 已启用。2026-09-29 项目所有者已反馈完成服务器外独立离线口令副本及 `MATCH` 核对，见 [阶段六测试与交付](../../../内容/第一版-平台主体/开发阶段6-公网安全与恢复能力/03-阶段六测试与交付.md)。

第二云接入时可另设 `/etc/powersystem/backup-secondary-target` 或 `POWERSYSTEM_BACKUP_SECONDARY_TARGET`，脚本将同一密文上传两处；任一上传失败均保留本地密文并返回失败。第二云专用 FRP 客户端、备份接收端和域名待备案配置见[独立云服务器部署](../../../部署/独立云服务器部署/README.md)，旧共享 FRP 不在此次变更范围。

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

详细操作和实际验收见[阶段二部署笔记](../../../内容/第二版-智能体升级/问答及调度业务主智能体/废案-旧智能体方案/开发阶段2-告警智能解读与大屏联动/04-服务器部署与故障隔离.md)。目前源码方案已实现，服务器切换状态以测试与交付笔记为准。

## 多智能体发布规划入口（2026-10-12历史）

后续阶段9部署/恢复设计见[[内容/第二版-智能体升级/多智能体协作建设/技术选型与架构设计/06-部署、观测、备份与回退]]。当前只是规划，未新增Compose服务、端口、数据库或发布动作；生产继续使用本README现行入口。新服务只在项目内网，沿用prepare/build/activate/rollback和实际备份隔离恢复，本次不提供可执行的新增生产命令。

## 五分区目录兼容（2026-10-13本机修复）

backup.sh读取资产/设备知识/手册来源清单.json，尚未迁移的服务器仅在新清单不存在时回退旧设备知识路径。实际私有PDF、模型、审计和发布目录继续使用服务器runtime及配置中的真实挂载源；本机资产/runtime仅为历史压缩包归档。

本轮只有本地路径选择、Shell语法和离线fixture检查，没有执行备份、SSH、Docker操作或服务器更新。详见[[部署/部署与运维/目录重构兼容修复-2026-10-13]]。

## 2026-10-11 BND-M2增量发布

BND-M2已在R730XD验收发布，公网入口8.138.42.212。本机及最终Linux Agent98项、前端19项、类型构建和Go全包通过；最终公网11类/19项、重启历史10项、实际公网浏览器9项、源码/镜像实际回退、最终加密备份隔离恢复及300秒固定镜像观察通过。五页阶段二笔记已同步，未提交或推送Git。正式证据见[[内容/第二版-智能体升级/问答及调度业务主智能体/智能体边界建设/开发阶段2-日常问题调用边界/03-阶段2测试与交付]]；指定入口https://8.138.42.212/agent，业务仍在R730XD。只切换API、前端和通用Agent，其他业务容器及持久卷保持原基线。更新前和最终本地加密包均完成摘要核对，最终包完成独立隔离恢复；本轮使用LOCAL_ONLY，不把此结果写成新云异地上传完成。

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

配置校验失败时停止，不以reload退出码判定恢复。核对API/前端容器ID、镜像与静态资产版本保持，使用既有可信HTTPS入口验证ping200，并在本人已登录页面检查WSS实时连接正常；模型请求502另按模型链故障取证。维护修订3补单/双/三参数恢复入口计数校验，隔离恢复十项结果及失败保留见[[内容/第二版-智能体升级/多智能体协作建设/03-联合发布验收/02-服务器发布与恢复验收/03-MA-M4测试与交付]]。

## 相关笔记

第三阶段发布使用[阶段七发布脚本与命令](../../07-agent/README.md)，统一备份同时保存受控知识索引、未命中问题持久目录与可选搜索密钥；恢复核对索引哈希与 JSONL 格式。实际镜像、回退和观察结果见[第三阶段交付](../../../内容/第二版-智能体升级/问答及调度业务主智能体/废案-旧智能体方案/开发阶段3-使用引导与值班问答/05-V2-M3测试与交付.md)。

- 阶段四总索引：[README.md](../README.md)
- 运维与使用：[平台运维与二次开发手册](../../../手册/平台运维与二次开发手册.md) · [平台使用手册](../../../手册/平台使用手册.md)
- 部署证据：[04-第15周·部署上线](../../../内容/第一版-平台主体/开发阶段4-前端、上线与沉淀/04-第15周·部署上线.md) · [05-运维与恢复](../../../内容/第一版-平台主体/开发阶段4-前端、上线与沉淀/05-运维与恢复.md)
- P0 收尾：[06-P0收尾与M4验收记录](../../../内容/第一版-平台主体/开发阶段4-前端、上线与沉淀/06-P0收尾与M4验收记录.md)
- M4 测试与交付：[07-M4测试与交付](../../../内容/第一版-平台主体/开发阶段4-前端、上线与沉淀/07-M4测试与交付.md)
- 飞书通知：[阶段五运行说明](../../05-feishu-notification/README.md) · [阶段五测试与交付](../../../内容/第一版-平台主体/开发阶段5-飞书告警通知/03-阶段五测试与交付.md)
- 安全恢复：[阶段六运行说明](../../06-security-recovery/README.md) · [阶段六测试与交付](../../../内容/第一版-平台主体/开发阶段6-公网安全与恢复能力/03-阶段六测试与交付.md)
