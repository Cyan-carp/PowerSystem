---
tags:
  - 源网智联
  - 开发阶段4
  - 部署
---

# 第 15 周服务器部署

> 当前运行状态只见[唯一状态页](../../../notes/00-当前系统状态.md)。日常开发与普通兼容修复优先阅读下方“分级开发与普通兼容发布”；后文带日期的旧批次只作历史，初装命令不能覆盖现网。规则见[上传规范](../../../notes/02-工程规范边界与验收治理/项目工程规范/05-上传服务器规范.md)，本次实现与验证见[流程提效记录](../../../notes/04-系统部署发布备份与变更记录/部署与运维日志/05-发布工具链与文档治理/54-开发测试发布与备份流程提效.md)。

## 分级开发与普通兼容发布

### 可用率小时汇总的本机管理

本节命令仅适用于包含017迁移与可用率汇总代码的API，实际启用状态仍见唯一状态页。
`-availability-rollup`只管理派生统计，不运行HTTP服务、MQTT订阅、模型或告警处理；不与`-migrate-only`或`-create-admin`混用。
新迁移默认后台计算开启、报表读取关闭；完成固定窗口原始结果对照与性能短查后才启用读取。

```bash
api_id=$(docker ps -q --filter label=com.docker.compose.project=powersystem-stage4 --filter label=com.docker.compose.service=api)
docker exec "$api_id" /app/server -availability-rollup status
docker exec "$api_id" /app/server -availability-rollup step
docker exec "$api_id" /app/server -availability-rollup enable
docker exec "$api_id" /app/server -availability-rollup disable
```

`step`最多运行2分钟且与API后台任务共用数据库互斥锁；未获得锁即退出，不额外增加并行读取。
`disable`仅停止报表读取汇总，恢复原始统计路径，后台可继续预热。控制表`worker_enabled`由本人本机维护，未提供HTTP写入口。
JSON追加`statistics_updated_at`、`aggregation_state`（raw/warming/ready/degraded）、`aggregation_lag_ms`；CSV追加对应的`X-Statistics-Updated-At`、`X-Aggregation-State`、`X-Aggregation-Lag-Ms`响应头。
缺桶、脏桶、未完成预热或心跳超过60秒均回原始计算；原始读取失败仍返回503，不返回部分成功。

应用回退无需降017表结构。若回到不支持汇总失效标记的旧API，须先关闭汇总读取；以后重新启用新版前，令派生小时全部`generation=generation+1`并清空`swept_at`，完成重算/对照后再开启，不能直接沿用旧API运行期间未失效的小时。
这是派生数据重建，不修改或回灌原始遥测；新发布仍按本页统一入口和本批恢复材料执行。

### 重建 gateway 或 simulator 前必读（2026-10-30 实测）

现行候选 `compose.json` 里 `gateway` 与 `simulator` 两行来自 `v4-business-schedule-20261026-r1` 模板，是旧值；现网实际运行的值更晚：

| 服务 | 字段 | 候选编排 | 现网运行值 |
| --- | --- | --- | --- |
| gateway | `MQTT_CLIENT_PREFIX` | `powersystem-stage2-fresh-20261029-r1` | `powersystem-stage2-ackfix-20261030-r1` |
| gateway | `STAGE2_DATA_DIR` | `/data/fresh-20261029-r1` | `/data/ackfix-20261030-r1` |
| simulator | `command` | 无 `--window-mode` | 含 `--window-mode on` |

`ui-align-20261030-r1` 与 `alarm-fixed-demo-20261030-r1` 两批都只重建 frontend/API，这两个容器没被碰过，所以当前没有实际影响。风险在以后：对候选编排执行不带 `--no-deps` 的 `up -d`，或下一批以它为 `--from-args` 再重建这两个服务，就会按旧值启动——网关前缀退回上一代、数据目录指错、模拟器窗口调度退化。

重建前先取现网真值并显式覆盖：

```bash
docker inspect powersystem-stage4-gateway-1 --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -E 'MQTT_CLIENT_PREFIX|STAGE2_DATA_DIR'
docker inspect powersystem-stage4-simulator-1 --format '{{json .Config.Cmd}}'
```

另一处结构性缺口：手工激活的批次（`ui-align`、`alarm-fixed-demo`）`private/origin-args.json` 是单文件冻结编排形式，不满足控制器的「五份基础编排按序」检查，下一批 `prepare` 会报 `complete_ordered_stage4_7_8_9_stack_required`。遇到时用 `v4-business-schedule-20261026-r1` 的 16 文件链作 `--from-args` 即可，细节见 [[notes/04-系统部署发布备份与变更记录/部署与运维日志/05-发布工具链与文档治理/69-报表左对齐与直接发布]]。

`stages/04-frontend/deploy/fleet.compose.yaml` 还有一处：它写 `--fault-mode scheduled`，而现网模拟器实际跑 `--fault-mode off`（用上面的 inspect 可验）。`compose` 合并时后叠加的文件会整段替换 `command`，把它叠在完整编排末尾，模拟器就从「不造故障」切成「按计划造故障」，在生产上凭空生成设备告警。要造故障必须显式确认，不能顺手套用。

版本差也要留意：仓库 HEAD 比现行生产多两个文件——`internal/ingest/telemetry_admission.go` 的 ACK 排序修复（拒绝路径改走批内统一 ACK，避免回调侧抢跑早于 SQLite 提交的消息）与本节的 `fleet.compose.yaml`。它们提交于 2026-10-30T12:02Z，晚于 `alarm-fixed-demo` 的 11:30Z 发布，现网 gateway 镜像仍是 `35f1996c`（06:58 启动），**没有**这处修复。所以从 HEAD 重建 gateway 属于功能变更而不是等价重建，重建后要重新做一轮遥测与告警短查。

### 步骤1 · 按影响完成开发验证与一次构建

本地迭代只做受影响检查，不连接生产、不新建全备。按本页“完整源码快照与构建回执”使用source-build导出明确提交及已审增量，统一LF、验证完整来源并构建一次；构建完成的固定镜像直接进入prepare，不在publish重新build/pull。

普通兼容档为`standard-fast`：仅替换已支持应用镜像，不改配置/权限/schema/契约或持久化语义；full仍为CLI默认。原`frontend-fast`继续兼容。新档在服务器安装相同工具前不可使用；实际启用状态见唯一状态页。迁移、ACK/幂等语义、数据修复等采用full和受影响专项。

### 步骤2 · 核对生产与有效数据恢复点

```bash
python3 /opt/powersystem/stages/04-frontend/deploy/production-stack.py status
```

同时读取active、actual_images、images_match_accepted及transaction。pending存在时先沿原事务接续；本次流程改造不清r4的未验事务。标准档要求24小时内双端成功且与已验应用兼容的全量基线，可沿兼容快发布链继承；基线不合格先等/补定时全备，不把旧包冒称新备份。

若成功定时全备仅造成有精确回执的写者停复，使用其实际密文路径做短重核：

```bash
python3 /opt/powersystem/stages/04-frontend/deploy/production-stack.py reconcile-backup --backup /var/backups/powersystem/<本次成功全备>.tar.gz.gpg
```

该命令只核原版本、成功回执、容器身份/镜像/重启数、配置及健康，保留published_at，不重建服务，不声称新HTTPS测试。失败备份、未知漂移、全体重启及pending均不适用；原verify-backup完整门禁入口继续保留。

### 步骤3 · 保存与测试镜像绑定的兼容评估

在runtime本轮目录保存JSON，以下是格式示例，必须替换候选/父版、真实镜像ID、实际测试证据路径及摘要。测试证据由本轮真实执行产生，不能复制示例假装通过；生产配置核对包含影响本次行为的durable会话、并发和超时等。

```json
{
  "format": 1,
  "kind": "compatible-release/v1",
  "candidate": "<本次候选>",
  "parent": "<现查已验父版>",
  "tested_images": {"api": "sha256:<实际64位镜像摘要>"},
  "rpo_seconds": 86400,
  "rollback_compatible": true,
  "production_config_matched": true,
  "schema_changed": false,
  "data_repair": false,
  "persistent_semantics_changed": false,
  "permissions_changed": false,
  "storage_changed": false,
  "contracts_changed": false,
  "tests": [{
    "name": "<受影响入口定向检查>",
    "exit_code": 0,
    "evidence": "/opt/powersystem/runtime/<本轮>/targeted-test.log",
    "sha256": "<该证据实际SHA256>"
  }]
}
```

选择普通档须确认本次最多24小时数据恢复点可接受。控制器自动比较父/候选完整来源中的SQL、迁移、schema、契约、策略/权限与构建依赖保护项；存在变化就拒绝普通档。评估不是任意强制放行开关，未知风险应改full。

### 步骤4 · 冻结、生成构件小包并发布

```bash
python3 /opt/powersystem/stages/04-frontend/deploy/production-stack.py prepare --id <本次候选> --tier standard-fast --image api=<已审固定镜像> --assessment /opt/powersystem/runtime/<本轮>/compatible-assessment.json
python3 /opt/powersystem/stages/04-frontend/deploy/frontend-release-backup.py --id <本次候选>
python3 /opt/powersystem/stages/04-frontend/deploy/production-stack.py publish --id <本次候选> --backup /var/backups/powersystem/<上一步实际输出的powersystem-release密文>.tar.gz.gpg
```

小包沿用既有脚本入口，由候选档位自动选择：standard-fast保存变更应用镜像、完整源码/回执、冻结配置/评估/测试证据与匹配控制器依赖；frontend-fast保持旧格式。新包加密、内层摘要读取及双端上传通过后签发回执。普通发布不再重复导出数据库、队列或向量，数据依赖所引用全备；小包仍需双端成功，不把上传队列当已完成副本。

普通档保留全服务身份/健康/配置检查与排空，公网检查主域名登录、匿名401、核心只读业务、会话/任务及可用详情、ticket/WSS；full与旧frontend-fast保留双IP完整门禁。改反向代理、鉴权、端口或权限不能借普通档缩减检查。失败按已验父版恢复，schema不兼容则按原专项冻结前滚。

### 步骤5 · 按范围记录与恢复

日常修复一份变更记录；运行变化写唯一状态，接口/用法/资产变化才更新相应文档，里程碑才完整交接。数据库定时全备、周期恢复和长观察独立安排，受影响的恢复链变更保留专项验证。

普通小包的离线核对：

```bash
python3 /opt/powersystem/stages/04-frontend/deploy/frontend-release-backup.py --verify /var/backups/powersystem/<实际构件小包>.tar.gz.gpg
```

它只解密流式读取与核哈，不提取秘密、不加载镜像、不改指针。灾难恢复先按原脚本在隔离环境恢复全量基线，再核小包payload摘要，导入application-images.tar并逐个核对实际image ID；按内含source-builds恢复对应来源，按tools恢复匹配工具，核冻结文件及私有引用后重建完整编排。全量基线未覆盖的卷/基础设施镜像仍按原恢复计划补齐。首次生产使用标准档前完成该组合的Linux隔离短核对，不重跑未受影响业务全矩阵。

### 本地与CI定向验证

```bash
python3 -m unittest discover -s stages/04-frontend/deploy -p 'test_production_stack*.py'
python3 -m unittest discover -s stages/04-frontend/deploy -p 'test_compatible_release.py'
python3 -m unittest discover -s stages/04-frontend/deploy -p 'test_backup_upload_parallel.py'
bash -n stages/04-frontend/deploy/backup.sh
```

Windows可用Python3.12+的`-X utf8`运行同样测试。发布工具CI仅由对应路径触发；笔记检查由笔记/规范/README结构变化触发，不因每次业务源码编辑重跑全Vault。

告警官方v2与每日五次演示批次使用完整冻结编排叠加`interpretation-official.compose.yaml`，不能单独运行或覆盖原有问答/运维配置。专属密钥在`/etc/powersystem/interpret-deepseek-key`，只挂阶段7 Agent；新密钥纳入既有加密备份。受控分步驱动见阶段7`scripts/interpretation-remote.py`，仍经production-stack的prepare→新备份→publish；清理额外持有部署锁并冻结业务写入、留存可读密文快照，不重置序列或清Redis。实际状态见[发布与清理记录](../../../notes/04-系统部署发布备份与变更记录/部署与运维日志/03-智能体与模型调用链/30-官方独立告警解读与每日五次演示.md)。

> 2026-10-14当前：SEC-M4本机限定签收及r6追加准入通过；MA-C32～C35和最终文档门槛已完成，MA-M4及联合阶段四在声明范围正式技术签收。服务器r6/v5、恢复维护修订3；本机唯一批准动作保留，写窗口关闭，生产仅登记R0。

> **2026-10-11现行问答增量**：指定公网入口为`https://8.138.42.212/agent`。阶段8已接管通用问答，生产使用阶段4主编排、阶段7 Agent/搜索、阶段8生产编排及发布目录固定镜像文件；旧告警解读继续由阶段7运行。现行操作见[[notes/01-项目调研规划与开发建设记录/02-第二版-智能体升级/问答及调度业务主智能体/当前方案-Agent与RAG重建/02-技术选型与架构设计/04-服务器部署与回退]]，正式结果见[[notes/01-项目调研规划与开发建设记录/02-第二版-智能体升级/问答及调度业务主智能体/当前方案-Agent与RAG重建/03-功能开发与阶段交付/04-完整Agent与会话/05-RAG服务器发布测试与交付]]。以下初装步骤不能直接用于覆盖当前运行配置。

> 本目录编排 R730xd 上的阶段四演示环境。上级总索引见 [README.md](../README.md)，阶段笔记见 [04-第15周·部署上线](../../../notes/01-项目调研规划与开发建设记录/01-第一版-平台主体/05-功能开发与阶段交付/04-前端、上线与沉淀/04-第15周·部署上线.md)。数据库与模拟器使用新的持久卷，不迁移本机验收库。模型文件 `model.json`、`metadata.json` 来自本机被 Git 忽略的 `artifacts/stage3-AI预测/model/`，上线时另行传至服务器 `runtime/model/`。

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

旧公网演示地址为 `http://8.138.10.222:3667`。2026-09-29 第一版现场已为 `8.138.10.222:443` 安装可信 IP 证书：云端 [cloud-ip-https.conf](cloud-ip-https.conf) 在 Nginx 终止 TLS，向本机 FRP 端口转发；[cloud-public-firewall.service](cloud-public-firewall.service) 拦截外部直连 3667；[cloud-cert-renew.timer](cloud-cert-renew.timer) 定时续期。外部证书校验和 `/api/v1/ping` 成功，旧 HTTP 直连超时。域名不要求。更换云主机、IP 或续期方式后须重新核对证书、Nginx、IPv4/IPv6 防火墙和 WebSocket，不能只复制此配置就视为安全通过。现场明细见[阶段六测试与交付](../../../notes/01-项目调研规划与开发建设记录/01-第一版-平台主体/05-功能开发与阶段交付/06-公网安全与恢复能力/03-阶段六测试与交付.md)。

## 三、监控、备份与恢复

Prometheus 每 15 秒采集 API 和主机指标，规则包含 API 不可用及磁盘使用率超过 80%；已移除需要宿主机高权限挂载的 cAdvisor。Grafana 看板由本目录配置自动装载。阶段五的设备/AI 业务事件由 PostgreSQL 投递表交给内网适配器，失败会重试；平台监控事件仍由 Alertmanager 发送。2026-09-29 旧 API 暂停/恢复与新业务链路均已核对，见[阶段五测试与交付](../../../notes/01-项目调研规划与开发建设记录/01-第一版-平台主体/05-功能开发与阶段交付/05-飞书告警通知/03-阶段五测试与交付.md)。

两台服务器 SSH 安全基线按“盘点现况 → 安装并验证公钥 → 保持第二管理会话 → `sshd -t` 校验 → 关闭 root 密码登录 → 从新会话复测”的顺序执行。保留原会话直至新会话和应急入口验证成功；复核 FRP、备份专用 SFTP 和原有代理不受影响。本轮按此顺序完成，两端 root 仅允许公钥；未来如要改用非 root 管理员，应先验证其 sudo 和应急访问。现场结果记入 [P0 收尾记录](../../../notes/01-项目调研规划与开发建设记录/01-第一版-平台主体/05-功能开发与阶段交付/04-前端、上线与沉淀/06-P0收尾与M4验收记录.md)。

云服务器使用 SFTP 专用账号 `powersystem-backup` 保存密文，R730xd 的私钥路径为 `/etc/powersystem/backup-ed25519`。执行 `backup.sh` 前须显式设置 `POWERSYSTEM_BACKUP_TARGET` 或私有 `/etc/powersystem/backup-target`；脚本不含公开默认目标。备份包括 PostgreSQL、TDengine、网关与模拟器 SQLite 队列、模型、`.env` 与三个通知私有文件，整体以 GPG AES256 加密后传至接收端。备份口令须在 R730xd 之外另行保管，不包含在密文包内。现场已核对专用 SFTP 目录、更新前后两份密文的异地 SHA256、独立恢复数据与私有文件；`powersystem-backup.timer` 已启用。2026-09-29 项目所有者已反馈完成服务器外独立离线口令副本及 `MATCH` 核对，见 [阶段六测试与交付](../../../notes/01-项目调研规划与开发建设记录/01-第一版-平台主体/05-功能开发与阶段交付/06-公网安全与恢复能力/03-阶段六测试与交付.md)。

第二云接入时可另设 `/etc/powersystem/backup-secondary-target` 或 `POWERSYSTEM_BACKUP_SECONDARY_TARGET`，脚本将同一密文上传两处；任一上传失败均保留本地密文并返回失败。第二云专用 FRP 客户端、备份接收端和域名待备案配置见[独立云服务器部署](../../../notes/04-系统部署发布备份与变更记录/独立云服务器部署/00-索引·独立云服务器部署.md)，旧共享 FRP 不在此次变更范围。

恢复验证只启动带独立卷的 `powersystem-restore-*` Compose 项目，不覆盖生产数据库：

```bash
bash stages/04-frontend/deploy/backup.sh
bash stages/04-frontend/deploy/restore-verify.sh /var/backups/powersystem/powersystem-<UTC时间>.tar.gz.gpg
```

恢复脚本输出隔离项目名、数据库行数、队列文件数及私有文件校验结果。Ops独立库在隔离三服务就绪后先恢复，以保留原15分钟新项目门禁；大业务库后续恢复耗时不改变该守卫。外层执行器须给大库COPY和索引足够时间，超时尝试保留证据，另用r2等新隔离根，不原地续签。新版本现场结果记录到第一版 M4 复验页。手动清理隔离演练资源前先确认其项目名，不能对生产 Compose 执行 `down -v`。

## 四、发布检查

每次更新先在本地运行前端、Go、Python 测试，备份当前数据，记录镜像版本；在服务器执行 Compose 配置校验和构建。上线后检查管理员/操作员权限、总览电量与健康分、设备实时值、告警确认、预测、WebSocket、Grafana、飞书通知和 `https://8.138.10.222/` 入口，再观察运行日志。回退时恢复上一版本源码和镜像，保持数据卷与备份不动；如新版本迁移了数据库，回退前核对旧版本是否能读取新增表。

## 五、V2-M2可选解读链路

使用 `stages/07-agent/deploy/agent.compose.yaml` 叠加基座并启用 `agent` profile。先备份，再配置独立服务/监控文件和审计目录，执行 `005_agent_interpretations.sql` 兼容迁移。Go 不依赖 Agent 健康而启动，Agent 不新增公网端口，Nginx 显式拒绝 `/internal/`。

独立监控令牌由 Agent 与 Alertmanager 读取；文件属组 `10001`、权限 `440`，Alertmanager 仅增加该附加组。模型及服务令牌权限 `400`。父目录保持仅管理员可访问，不把令牌内容放入 Compose 或命令输出。

新版备份在发现 Agent 服务凭据后增加三个私有文件、审计及完整 Redis RDB；PostgreSQL 全库备份自然包含新增任务、结果与已读表。恢复验证使用独立 Redis 卷，旧备份不要求存在新文件。回退关闭新增链路并恢复原镜像及 Alertmanager 配置，保留新增表和业务卷；禁止生产 `down -v`。

详细操作和实际验收见[阶段二部署笔记](../../../notes/01-项目调研规划与开发建设记录/02-第二版-智能体升级/问答及调度业务主智能体/废案-旧智能体方案/02-功能开发与阶段交付/02-告警智能解读与大屏联动/04-服务器部署与故障隔离.md)。目前源码方案已实现，服务器切换状态以测试与交付笔记为准。

## 多智能体发布规划入口（2026-10-12历史）

后续阶段9部署/恢复设计见[[notes/01-项目调研规划与开发建设记录/02-第二版-智能体升级/多智能体协作建设/02-技术选型与架构设计/06-部署、观测、备份与回退]]。当前只是规划，未新增Compose服务、端口、数据库或发布动作；生产继续使用本README现行入口。新服务只在项目内网，沿用prepare/build/activate/rollback和实际备份隔离恢复，本次不提供可执行的新增生产命令。

## 五分区目录兼容（2026-10-13本机修复）

backup.sh读取资产/设备知识/手册来源清单.json，尚未迁移的服务器仅在新清单不存在时回退旧设备知识路径。实际私有PDF、模型、审计和发布目录继续使用服务器runtime及配置中的真实挂载源；本机资产/runtime仅为历史压缩包归档。

本轮只有本地路径选择、Shell语法和离线fixture检查，没有执行备份、SSH、Docker操作或服务器更新。详见[[notes/04-系统部署发布备份与变更记录/部署与运维日志/01-部署基线与仓库发行/02-五分区目录迁移与本地基线（原02+03）]]。

## 2026-10-11 BND-M2增量发布

BND-M2已在R730XD验收发布，公网入口8.138.42.212。本机及最终Linux Agent98项、前端19项、类型构建和Go全包通过；最终公网11类/19项、重启历史10项、实际公网浏览器9项、源码/镜像实际回退、最终加密备份隔离恢复及300秒固定镜像观察通过。五页阶段二笔记已同步，未提交或推送Git。正式证据见[[notes/01-项目调研规划与开发建设记录/02-第二版-智能体升级/问答及调度业务主智能体/智能体边界建设/04-功能开发与阶段交付/02-日常问题调用边界/03-阶段2测试与交付]]；指定入口https://8.138.42.212/agent，业务仍在R730XD。只切换API、前端和通用Agent，其他业务容器及持久卷保持原基线。更新前和最终本地加密包均完成摘要核对，最终包完成独立隔离恢复；本轮使用LOCAL_ONLY，不把此结果写成新云异地上传完成。

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

配置校验失败时停止，不以reload退出码判定恢复。核对API/前端容器ID、镜像与静态资产版本保持，使用既有可信HTTPS入口验证ping200，并在本人已登录页面检查WSS实时连接正常；模型请求502另按模型链故障取证。维护修订3补单/双/三参数恢复入口计数校验，隔离恢复十项结果及失败保留见[[notes/01-项目调研规划与开发建设记录/02-第二版-智能体升级/多智能体协作建设/06-联合发布验收/02-服务器发布与恢复验收/03-MA-M4测试与交付]]。

## 2026-10-16 · 回答可读性增量发布

[[notes/01-项目调研规划与开发建设记录/02-第二版-智能体升级/问答及调度业务主智能体/当前方案-Agent与RAG重建/04-增量优化与补充交付/01-回答可读性与任务结果展示/03-测试与交付]]记录本批实际结果。该批生产指针assistant-readability-20261016-r1，通过本页唯一入口及双HTTPS/WSS门禁；直接上一版本interpretation-layout-20261016-r1保留，仅frontend重建。既有Executor同ID、同镜像登记重载，API及其他镜像保持。免重复测试不跳过必要构建和发布门禁，实际rollback未触发。

## 相关笔记

第三阶段发布使用[阶段七发布脚本与命令](../../07-agent/README.md)，统一备份同时保存受控知识索引、未命中问题持久目录与可选搜索密钥；恢复核对索引哈希与 JSONL 格式。实际镜像、回退和观察结果见[第三阶段交付](../../../notes/01-项目调研规划与开发建设记录/02-第二版-智能体升级/问答及调度业务主智能体/废案-旧智能体方案/02-功能开发与阶段交付/03-使用引导与值班问答/05-V2-M3测试与交付.md)。

- 阶段四总索引：[README.md](../README.md)
- 运维与使用：[平台运维与二次开发手册](../../../notes/03-平台使用运维与二次开发手册/平台运维与二次开发手册.md) · [平台使用手册](../../../notes/03-平台使用运维与二次开发手册/平台使用手册.md)
- 部署证据：[04-第15周·部署上线](../../../notes/01-项目调研规划与开发建设记录/01-第一版-平台主体/05-功能开发与阶段交付/04-前端、上线与沉淀/04-第15周·部署上线.md) · [05-运维与恢复](../../../notes/01-项目调研规划与开发建设记录/01-第一版-平台主体/05-功能开发与阶段交付/04-前端、上线与沉淀/05-运维与恢复.md)
- P0 收尾：[06-P0收尾与M4验收记录](../../../notes/01-项目调研规划与开发建设记录/01-第一版-平台主体/05-功能开发与阶段交付/04-前端、上线与沉淀/06-P0收尾与M4验收记录.md)
- M4 测试与交付：[07-M4测试与交付](../../../notes/01-项目调研规划与开发建设记录/01-第一版-平台主体/05-功能开发与阶段交付/04-前端、上线与沉淀/07-M4测试与交付.md)
- 飞书通知：[阶段五运行说明](../../05-feishu-notification/README.md) · [阶段五测试与交付](../../../notes/01-项目调研规划与开发建设记录/01-第一版-平台主体/05-功能开发与阶段交付/05-飞书告警通知/03-阶段五测试与交付.md)
- 安全恢复：[阶段六运行说明](../../06-security-recovery/README.md) · [阶段六测试与交付](../../../notes/01-项目调研规划与开发建设记录/01-第一版-平台主体/05-功能开发与阶段交付/06-公网安全与恢复能力/03-阶段六测试与交付.md)

## 2026-10-15 前端 UI 增量发布

深空值班台UI及四项校对修正已在指定HTTPS入口发布。前端固定镜像、真实旧版回退/同候选恢复、HTTPS/WSS、备份隔离恢复及300.7秒观察通过；其他18个原业务服务保持基线，已有Executor仅为资产登记读取受控重启、镜像与容器ID未变。后台r6/v5、生产R0与关闭写窗口不变。原阶段签收和模型质量边界保留，不据本次UI发布重新声明全版本验收。详见[[notes/04-系统部署发布备份与变更记录/部署与运维日志/02-界面展示与通知/07-前端UI深空值班台上线记录]]。

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

已部署环境不得使用本页初装的单份基础compose重建API。生产必须包含阶段4、阶段7 Agent/search、阶段8和阶段9叠加，以及当前各服务固定镜像；实际环境文件从健康agent-rag标签读取。先核对[[notes/04-系统部署发布备份与变更记录/部署与运维日志/01-部署基线与仓库发行/06-API版本回退恢复记录]]。

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

设计原理、逐条要求与审查边界见[[02-工程规范边界与验收治理/项目工程规范/08-发布工具与故障恢复说明]]；本页只维护具体操作命令和参数。

长期约束统一见[[notes/02-工程规范边界与验收治理/项目工程规范/02-发布仓库与界面验收规范#生产发布防回退与自动验收门禁（2026-10-15，强制）]]。入口为`/opt/powersystem/stages/04-frontend/deploy/production-stack.py`，状态目录为`/opt/powersystem/runtime/production-stack/`，仅在指定R730XD以root运行。

### 当前基线与只读查看

2026-10-22 22:09 现行：生产指针 `report-duration-20261022-r1`，父版本 `natural-alarm-20261022-r2`，pending=false，**首个使用 `--tier frontend-fast` 的发布**。仅前端更新：统计报表的运行/停机/故障/未知时长改为「X时Y分Z秒」，为零的时、分省略。prepare 17.8 s、前端小包 21.5 MB 双上传、publish 100.4 s，全批约 2.5 分钟；基线 `powersystem-20261022T134233Z`（24 小时 RPO），隔离恢复演练 `ma_restore_passed=true`。同批修复控制器 `timing_receipt()`：原实现按 `*.tar.gz.gpg.timing.json` 找凭证，与 `backup.sh` 实际输出 `*.timing.json` 不符，导致快路径永远报 `fast_baseline_must_follow_current_release`。详情见[报表时长与快路径启用记录](../../../notes/04-系统部署发布备份与变更记录/部署与运维日志/05-发布工具链与文档治理/33-发布控制器演进（原33+36）.md)。

2026-10-22 21:13 历史：生产指针 `natural-alarm-20261022-r2`，父版本 `agent-rag-sse-20261022-r2`，pending=false。更新 `api`、`frontend`、`simulator`：模拟器在北京时间 09/11/13/15/17 五个十分钟窗口向当日轮转的合成设备注入越限遥测，由 Go 既有自然规则创建/恢复 `origin=normal` 告警；同时删除旧 `fault_ramp` 故障排程，新增独立 `--window-mode`（默认 on），`--fault-mode` 降为兼容空操作——生产命令仍带 `--fault-mode off`，窗口照常注入。新全量加密备份（`powersystem-20261022T130048Z`，主 118.8 s / 次 116.1 s）双目标上传、正式双 HTTPS/WSS 门禁通过；迁移后车队规则 572 条（571 启用）、策略设备 143 条；模拟器启动日志 `window_mode=on`、`device_count=143`。详情见[自然规则告警上线记录](../../../notes/04-系统部署发布备份与变更记录/部署与运维日志/04-供数与业务批次/34-自然规则告警候选与上线（原34+35）.md)。

2026-10-22 13:55 历史：生产指针 `ui-devcode-20261022-r1`，父版本 `ui-paging-20261022-r1`，pending=false，仅前端更新。设备管理、健康排行、统计报表三页的设备编码与名称改为分段渲染（常量弱化、序号强调）并加场站色点，三个长列表加 stripe；本地类型检查与 70 项单测通过，更新前双目标密文备份、正式双 HTTPS/WSS 门禁和容器分块核对通过。其他镜像、编排、卷与设备台账未变；顺带清理了生产源码内遗留的 `src/_sync_tmp/`。详情见[视觉区分上线记录](../../../notes/04-系统部署发布备份与变更记录/部署与运维日志/02-界面展示与通知/31-前端列表可读性（原31+32）.md)。

2026-10-22 12:48 现行：生产指针 `ui-paging-20261022-r1`，父版本 `interpretation-official-20261021-r4`，pending=false，仅前端更新。健康排行与统计报表两页长列表改为客户端分页（20/50/100 可选），健康排行表头新增场站与设备编号/名称筛选；本地类型检查与 62 项单测通过，更新前双目标密文备份、正式双 HTTPS/WSS 门禁和公网路由分块核对通过。其他镜像、编排、卷与设备台账未变；详情见[分页上线记录](../../../notes/04-系统部署发布备份与变更记录/部署与运维日志/02-界面展示与通知/31-前端列表可读性（原31+32）.md)。以下早先基线按日期保留。

2026-10-21 10:52 现行：生产指针 `interpretation-auto-20261021-r1`，父版本 `fleet-mqtt-lockfix-20261021-r1`，pending=false，仅旧agent镜像更新。新密文备份双云上传、正式门禁、30项定向测试、合成probe及线上解读通过；13条活动失败任务11条answered、2条原校验拒绝保留。其他镜像与设备台账未变；详情见[告警解读修复上线](../../../notes/04-系统部署发布备份与变更记录/部署与运维日志/03-智能体与模型调用链/18-解读模型通道兼容与输出预算（原18+28+29）.md)。以下早先基线按日期保留。

2026-10-20 22:31 现行：生产指针 `fleet-release-20261020-r2`，直接父版本 `alarm-schedule-restore-20261020-r1`，pending=false。新增四站 140 台合成设备供数，全平台 143 台当前观测到达；更新 API、frontend、simulator、agent-rag、ops-agent，双云备份、双 HTTPS/WSS 门禁和 14 项短核对通过。正式事实见 [扩容上线记录](../../../notes/04-系统部署发布备份与变更记录/部署与运维日志/04-供数与业务批次/26-四场站140台设备扩容上线.md)。以下早先基线按日期保留。

2026-10-19 21:05:40 +08:00 当前生产指针 `ops-model-migration-20261019-r2`，修正安全审查按实际步骤绑定证据，运维与安全模型仍为 `deepseek-v4.1-flash`。仅更新安全服务，双云备份、双 HTTPS/WSS 门禁、设备查询和固定版本观察通过；基础设施完整任务仍有能力不可用及超时失败；R0/写关闭保持。见 [模型切换发布记录](../../../notes/04-系统部署发布备份与变更记录/部署与运维日志/03-智能体与模型调用链/22-运维与安全审查模型切换.md)。

2026-10-19 20:49首次切换记录：当时生产指针为 `ops-model-migration-20261019-r1`，直接上一版 `report-performance-20261019-r1`。仅更新运维和安全审查服务，均使用 `deepseek-v4.1-flash`，当前生产许可/策略 v6，原私有目录中新建版本配置，保留旧文件及备份兼容。双云备份、双 HTTPS/WSS 门禁和设备查询通过，观察已完成，修订验证见现行记录，R0/写关闭保持。见 [[notes/04-系统部署发布备份与变更记录/部署与运维日志/03-智能体与模型调用链/22-运维与安全审查模型切换]]。

2026-10-19 19:55历史记录：生产指针为 `report-performance-20261019-r1`，直接上一版 `fr-bnd-release-20261019-r2`。只更新 API 和前端，七天窗口可用率由 38.2 秒降至约 1 秒，同窗口时长/比率与 CSV 一致；新双云密文备份及正式双 HTTPS/WSS 门禁通过，观察通过，生产 R0/写关闭保持。见 [[notes/04-系统部署发布备份与变更记录/部署与运维日志/04-供数与业务批次/21-统计报表性能优化]]。

2026-10-19 19:10历史记录：生产指针为 `fr-bnd-release-20261019-r2`，直接上一版 `fr-bnd-20261019-r3` 保留。四服务 FR 修复/BND 批次二经本页唯一入口、更新前双云密文备份和正式双 HTTPS/WSS 门禁发布，生产 R0/写窗口关闭不变；原运维模型 HTTP 503，质量专项未签收。知识索引、恢复和观察实际结论见 [[notes/04-系统部署发布备份与变更记录/部署与运维日志/04-供数与业务批次/19-FR与BND两批发布（原19+20）]]。以下 10 月 18 日为上一轮历史记录，实际指针以 `status` 为准。

2026-10-18白天循环与故障注入解耦已通过统一入口发布；当前指针 `daylight-fault-decouple-20261018-r1`，上一版 `interpretation-role-20261018-r1` 保留，未触发rollback。仅指定 simulator，私有完整编排参数追加仅含模拟器命令的覆盖层，显式 `--fault-mode off` 并保留60分钟白天循环；不改已验收冻结文件和其他服务配置。原卷、账本、历史及模型阈值保持，必要构建、10项隔离检查、75个合成窗口、更新前双云密文备份、正式门禁及发布后一轮正常预测通过。事实统一见[[notes/04-系统部署发布备份与变更记录/部署与运维日志/04-供数与业务批次/42-白天循环与故障日程（原补充业务02）]]，不另建笔记。当前指针及待恢复事务由下列命令读取：

```bash
python3 /opt/powersystem/stages/04-frontend/deploy/production-stack.py status
```

### 后续发布

本节保留full路径示例；兼容普通修复按本页开头standard-fast流程执行，不要求每次重复导出全平台数据。

设备扩容追加层为本目录 `fleet.compose.yaml`：为 API 与模拟器只读装载同一正式清单，保留 `scheduled` 及原模拟器账本。该文件必须叠加在当前完整编排末尾，并通过 `--from-args` 提交给统一入口；不得单独启动。清单建议使用 `runtime/production-stack/fleet-manifests/<批次>.json` 的版本化路径并设置 `POWERSYSTEM_FLEET_FILE`，或生成等价的绝对路径 JSON 覆盖层，以纳入现有备份和 `/run/` 只读引用摘要校验。

扩容不修改智能体 R0/写关闭配置。台账导入是单独授权的数据事务，应用回退不自动删除已导入设备和遥测。上线后的短核对入口为：

```bash
python3 /opt/powersystem/stages/04-frontend/deploy/check-fleet-live.py --output /opt/powersystem/runtime/<本次批次>/live-check.json
```

检查只读设备、场站、总览和告警，最多等一分钟接收当前遥测，不请求模型或执行容量测试。脚本登录使用现有服务器私有口令文件，不输出密码、JWT 或完整响应。

开发阶段观察（所有者2026-10-29调整）：取消原两小时待办和后续AI驻守长观察，真实服务器自然运行；遇到真实问题按[错误与功能定位清单](../../../notes/04-系统部署发布备份与变更记录/部署与运维日志/05-发布工具链与文档治理/57-开发阶段运行错误与功能定位清单.md)反馈。需要遥测短观察时，由本人在业务VM启动：

```sh
python3 /opt/powersystem/stages/04-frontend/deploy/observe-telemetry.py \
  --duration 300 --interval 10 \
  --output-dir /opt/powersystem/runtime/telemetry-observation-$(date -u +%Y%m%dT%H%M%SZ)
```

仅在实际业务VM以有Docker读取权限的管理用户执行，输出目录必须不存在；不开放新端口。脚本只读指标、原始积压/逐设备队首、能源质量/更新及TD实际最新样本（每轮5台轮转），写入 `samples.jsonl` / `summary.json`；把两份文件交回Codex复核。每条SQL3秒、HTTP2—3秒、日志有界投影，失败也留记录；超慢轮次可能使总耗时略超参数，中断与缺项均不判通过。退出0为当次短观察通过，1为异常/未完成，不自动验收、清pending、重启或修数据。不含口令、JWT、原始载荷或完整日志；现有容器环境内TD凭据只用于本机精确只读查询，绝不写输出。离线测试：`python -m unittest discover -s stages/04-frontend/deploy -p test_observe_telemetry.py`（仓库根执行）。

原 `watch-fleet-soak.py`保留为总览接口历史诊断器，默认也改五分钟，不再作为长稳前置。历史容量夹具与原批次证据保留，不能拿生产Compose或旧m4-load-setup.sql直接写生产；历史数字见[容量与长时间测试记录](../../../notes/01-项目调研规划与开发建设记录/03-第三版-设备扩容/03-容量与稳定性验证/08-容量与长时间测试记录.md)，不要求为本次修复重跑。

先独立构建并审查新镜像。API、gateway、frontend、agent-rag、ops-agent、security-agent 使用下面的完整源码快照流程；服务器 `/opt/powersystem/stages/` 保留历史资料及运行引用，**不能用作构建上下文**。旧目录的五个主 Dockerfile 在服务器上是拒绝直接构建的提示文件；仓库中的 Dockerfile 不变。gateway 与 API 共用 02 的 Dockerfile，工具为 gateway 固定传入 `APP=gateway`，不改变现行网关构建方式。

#### 完整源码快照与构建回执

先在有 Git 仓库的受控工作区确定并审查完整的 40 位提交，再导出五阶段快照。不得把旧服务器目录当底树只补几个改动文件：

```sh
python stages/04-frontend/deploy/source-build.py export \
  --repo . --revision <已核验的40位提交> --store artifacts/<本次批次>/source-builds
```

导出同时检查这五阶段的工作区。存在未提交改动时默认拒绝，避免默默忽略本次修改；逐文件复核后，用重复 `--overlay stages/.../文件` 明确纳入新增或修改文件，用 `--delete stages/.../文件` 明确纳入删除。改名先拆为删除与新增；未列明的改动仍拒绝。无须为了构建而暂存、提交或推送。文本增量规范为 LF，私有配置、密钥和生成目录不允许混入。

输出 `source`、`sha256`、`revision` 和文件数。快照在 `source-builds/contexts/<sha256>/`，包含 `source-manifest.json` 与五阶段完整文件。按上传规范核查路径与占用后，将该目录按原摘要放到业务机 `/opt/powersystem/runtime/source-builds/contexts/<sha256>/`，先验证，再构建：

```sh
python3 /opt/powersystem/stages/04-frontend/deploy/source-build.py verify \
  --source /opt/powersystem/runtime/source-builds/contexts/<sha256>
python3 /opt/powersystem/stages/04-frontend/deploy/source-build.py build \
  --source /opt/powersystem/runtime/source-builds/contexts/<sha256> \
  --store /opt/powersystem/runtime/source-builds \
  --service api --tag powersystem-api:<本次独立版本>
```

`build` 逐文件校验，并把同一批已验字节通过 tar stdin 交给 Docker，不从旧目录读取，不部署或启动服务。09 的上下文是五阶段快照根，包含 08/09/10 交叉契约；其他四服务使用各自阶段子目录。只支持明确版本标签，已有标签不覆盖。工具核实际 image ID、源码摘要/版本/服务标签后，保存 `source-builds/receipts/<镜像摘要>.json`。也可在本机独立构建后流式导入镜像，但必须同时传入完整快照及对应回执，服务器重新核对实际镜像标签。

`prepare` 对上述六服务中**实际换镜像**的项目强制验证回执、完整快照与实际 image ID 的绑定，失败报 `source_build_required`，在创建候选及备份前停止。冻结清单保存 `source_builds` 摘要；自行 `docker build` 而没有回执的镜像不能进入发布。已有运行镜像和既有已验历史版本不要求补造回执，原 `rollback` 仍按原冻结编排及门禁执行。源码快照与回执须随仍引用它们的候选、历史镜像保留。

前端候选还有一层产物门禁：`prepare` 会比对候选镜像与当前运行镜像的逻辑分块名（剥掉构建哈希），候选缺少任何一个即以 `frontend_assets_missing:<名字>` 停止；确实要下线页面时显式加 `--allow-asset-shrink`。

`prepare`只解析配置、读取Docker元数据和冻结候选；可传递已构建镜像标签，工具会解析为实际镜像ID。以下新运行编号与镜像标签须替换，不能原样当成现成版本：

```bash
python3 /opt/powersystem/stages/04-frontend/deploy/production-stack.py prepare \
  --id release-YYYYMMDD-rN --image api=reviewed-api:release-YYYYMMDD-rN
```

同批其他镜像使用重复`--image frontend=...`等参数；仅配置变更使用`--service api`等显式指定应用。需要新的集成环境文件时，传入受审完整Compose参数JSON的`--from-args`，仍须包含原顺序的五份基础编排。已接受版本引用的私有文件不可原位修改，应使用新路径准备候选；数据库迁移、公开端口、持久卷变化不在此入口范围。

`prepare`在冻结候选前会跑三项只读短检（失败即停在`preflight:*`，不写候选目录，可直接用同一 id 重跑）：

| 检查 | 判据 |
|---|---|
| `preflight.private_layout` | 私有 env 位于 `<ma-m4-*>/private/`；`private/executor/` 存在且有 `executor.json`；`ops-executor` 的 `/run/ma-executor` 挂载源与该目录一致 |
| `preflight.asset_ledger` | 主机 `registry.json` 的容器资产版本不超过 DB `ma_assets` 已提交版本 +1（+1 视为未提交导入） |
| `preflight.migration_entry` | 从 API 镜像内取 `/app/deploy/postgres/*.sql`，按 `store.Migrate` 的按分号全切规则复算；注释内分号、`$$` 块、非关键字碎片一律拒绝，并要求 `telemetry_inbox` 建表语句存在 |

备份前可单独复核一次：

```bash
python3 /opt/powersystem/stages/04-frontend/deploy/production-stack.py preflight --id release-YYYYMMDD-rN
```

候选准备完成后新建并核对更新前加密备份再发布，`--backup`必须是业务机既有备份目录内、候选准备后且24小时内生成、附有匹配SHA256的密文。发布会自动执行门禁，成功才移动当前指针：

```bash
python3 /opt/powersystem/stages/04-frontend/deploy/production-stack.py publish \
  --id release-YYYYMMDD-rN \
  --backup /var/backups/powersystem/powersystem-YYYYMMDDTHHMMSSZ.tar.gz.gpg
```

全量档的候选是 V4 活跃时，这次全备会停复 RAG/Ops，运行状态因此不同于候选冻结快照。`publish` 读取同一个密文旁的 `<archive>.v4-maintenance.json`：只有当回执绑定同一父版本与冻结清单、`before` 等于候选 `before`、`after` 等于当前状态、且只有 RAG/Ops 的启动时间变化时，才把这段差异认作停写维护。两处基线（父版门禁、候选快照）都按同一份回执判定，其余漂移、换镜像、换容器 ID 或改重启次数一律拒绝；门禁照旧全跑，冻结清单不改。

### 一次性备份延后授权（2026-10-26，两阶段及一次修正重发；不是默认）

> **仅本次例外**：项目所有者 2026-10-26（UTC）明确要求“激进一点，连做大阶段后验证，本次不要全量备份，效果先呈现，等他确认可以结束后再备份”。该指令覆盖本页“先备份再发布”的常规顺序，只对本节两个候选有效；不得据此推导通用 `--skip-backup`/`--force`，也不改变其他任何发布、回退、门禁或后续批次的默认要求。效果确认后仍须补做全量加密备份，本批在此之前没有新恢复点。

范围写死在控制器常量里，无法用参数扩大：

- 候选 ID 只能是 `v4-production-compat-20261026-r1` 或 `v4-business-schedule-20261026-r1`，且只能 `--tier full`；
- 变更服务必须恰为 `api`、`agent-rag`、`ops-agent`、`security-agent` 四项；
- 授权文件必须位于 `/opt/powersystem/runtime/` 下（受 `owned` 约束），由所有者签署后随候选冻结。

授权 JSON 字段（缺一即拒，全部为显式值，不做推断）：

```json
{
  "schema": 1,
  "kind": "backup_deferral_authorization",
  "candidate": "v4-business-schedule-20261026-r1",
  "parent": "<prepare 时的 active 版本名，必须精确相等>",
  "services": ["api", "agent-rag", "ops-agent", "security-agent"],
  "owner_requested_defer_until_effects_accepted": true,
  "owner_instruction": "激进一点，连做大阶段后验证，本次不要全量备份，效果先呈现，等他确认可以结束后再备份",
  "recorded_at": "2026-10-26T06:20:00Z"
}
```

`prepare` 加 `--defer-backup-authorization`，在任何 Docker 操作前先校验（失败报 `backup_deferral_*`，不建候选目录，可用同一 ID 重跑）：

```bash
python3 /opt/powersystem/stages/04-frontend/deploy/production-stack.py prepare \
  --id v4-business-schedule-20261026-r1 \
  --service api --service agent-rag --service ops-agent --service security-agent \
  --image api=reviewed-api:v4-business-schedule-20261026-r1 \
  --defer-backup-authorization /opt/powersystem/runtime/<本批>/private/backup-deferral-authorization.json
```

冻结时把授权文件**逐字节**复制为候选目录内 `backup-deferral.json`，加入 manifest `files` 摘要表，并在 `backup_deferral` 中记录同一摘要、候选、父版本、`recorded_at` 与指令摘要。指针绑定 `digest(manifest)`，因此授权内容和绑定在 freeze 之后无法改写：改文件触发 `frozen_artifact_changed`，改绑定触发 `frozen_backup_deferral_unbound`，用未冻结的源文件顶替触发 `backup_deferral_frozen_file_changed`。源授权文件此后可另行归档。

存在合法冻结授权时，`publish` 可省略 `--backup`：

```bash
python3 /opt/powersystem/stages/04-frontend/deploy/production-stack.py publish \
  --id v4-business-schedule-20261026-r1
```

此时事务记录 `backup_deferred`（含授权摘要、`recorded_at`、原始指令、`effects_accepted: false`），计时旁车写 `notes.backup_deferred`，**不写** `predeployment_backup`，也不伪造任何备份通过。因为本批没有停复写者，两处基线判定反而更严：运行状态必须精确等于候选冻结快照，否则以 `candidate_baseline_drift` 停止。因此 prepare 与 publish 之间不要跑全量备份（包括 `powersystem-backup.timer` 的定时全备），否则需改用正常 `--backup` 路径重新发布。

其余全部不变：部署锁、父版本/来源回执、排空（含 `assistant_runs`）、服务集与镜像身份、健康、双 HTTPS/WSS 门禁、失败自动回退与 pending 处理一律照旧。传了 `--backup` 仍走原有核验，等于所有者随时可以取消延后；没有冻结授权的候选（含全部历史清单）不传 `--backup` 报 `predeployment_backup_required`，旧行为不变。


### 分级发布：仅前端镜像快路径（2026-10-22 已启用）

默认 `--tier full` 保持上面的候选后新全量备份。只有本次唯一变更服务是 `frontend`、只替换其固定镜像 ID、完整冻结 Compose/私有环境/挂载/端口与父版一致时，才可显式选 `--tier frontend-fast`。控制器在创建候选前逐项比对配置；不确定范围、涉及 API/迁移/数据/私有配置时仍用 `full`。快路径不裁剪排空、全服务身份及健康、双公网 HTTPS/WSS 或失败回退门禁。

快路径引用一份**当前版本激活之后**、24 小时内完成且双异地上传成功的全量密文；边界取指针 `published_at`（应用激活时间），不是 `updated_at`——`verify-backup` 重签会推进 `updated_at`，用它当边界等于要求备份晚于它自己触发的重签。无 `published_at` 的历史指针按发布目录里的 `backup-maintenance-*.json` 链回溯激活时间；若父版也是前端快发布，可沿用其已验证基线。该基线只承诺最多 24 小时的数据恢复点，不补录期间的数据库、遥测或队列变化；项目所有者已明确接受这项前端-only RPO。首次使用须等待新计时旁车证明双异地上传的全量备份，旧包无凭据不能冒充。候选本身另生成包含固定前端镜像、冻结清单和引用基线摘要的加密小包，并传至两处；任何核对失败即停，不自动降级到弱检查。后续 `full` 发布不继承快路径基线。

```bash
python3 /opt/powersystem/stages/04-frontend/deploy/production-stack.py prepare \
  --id ui-YYYYMMDD-rN --tier frontend-fast --image frontend=reviewed-ui:ui-YYYYMMDD-rN
python3 /opt/powersystem/stages/04-frontend/deploy/frontend-release-backup.py --id ui-YYYYMMDD-rN
python3 /opt/powersystem/stages/04-frontend/deploy/production-stack.py publish \
  --id ui-YYYYMMDD-rN \
  --backup /var/backups/powersystem/powersystem-frontend-YYYYMMDDTHHMMSSZ.tar.gz.gpg
```

小包由私有脚本输出精确路径，不应根据上面的占位名猜测。脚本使用与全量备份相同的口令、SSH 身份和主备目标，不打印密钥内容。若基线失效、配置漂移或上传失败，重新评估为全量发布；已冻结快路径候选不能原位改档，须另用新候选 ID。上传前按 [[notes/02-工程规范边界与验收治理/项目工程规范/05-上传服务器规范]] 核对服务器目录索引、LF 与文件摘要。

续基线后先跑轻量连续性检查，不启动业务容器：

```bash
python3 stages/04-frontend/deploy/verify-backup-integrity.py \
  /var/backups/powersystem/powersystem-<UTC>.tar.gz.gpg \
  --output /tmp/integrity.json
```

它校验密文摘要、GPG 解密、内层 `SHA256SUMS`、必需私有文件、队列快照、`pg_restore -l` 可读性和 SQLite 行数，`status=ok` 只代表“备份可读、结构完整”，**不能替代** `restore-verify.sh` 的完整隔离恢复演练。完整演练按上传规范 7.2.1 的触发条件执行，不按日历排期。

灾难恢复时，全量基线负责其快照时刻**备份范围内**的数据与私有环境，快路径小包负责本版前端镜像、控制器和冻结文件；两份密文与摘要缺一不可。先在隔离环境验证全量基线，再解密小包并核对内层 `capsule.json` 的镜像 tar、控制器、manifest 和冻结文件摘要，导入前端镜像并核对固定 image ID，最后重建完整编排及门禁；不得把只恢复前端镜像当成数据恢复，也不得直接把备份里的生产指针激活。全量包没有保存所有非前端 Docker 镜像，也未承诺恢复 EMQX/Grafana/Prometheus 数据卷；这些按既有恢复计划另行核对，不可把24小时RPO外推到未备份范围。首次生产启用快路径前须做一次该组合的隔离恢复短演练，记录边界，不重跑全后端业务链路；该演练已于 2026-10-22 用基线 `powersystem-20261022T134233Z` 完成（`ma_restore_passed=true`），后续按换基线或工具链变更时复核。

该入口只重建指定应用，使用冻结JSON及全部固定镜像；自动检查Nginx并更新已登记容器身份，通过同ID Executor重载刷新登记。真实操作员凭据仅从`/etc/powersystem/demo-password`读取，不输出密码、JWT或ticket。无`--skip-test`、`--force`或跳过门禁选项。`publish`在排空任务前会重跑同一组短检，已冻结候选不能绕过。

### 分段计时

`prepare`、`preflight`、`publish`、`rollback` 每次结束都写一条 `runtime/production-stack/timing/<ns>-<phase>.json`（0600，保留最近 200 条，成功与失败都写），stdout 同时给出 `total_seconds`、`timing_file`、`slowest`。`publish` 会把同批备份的旁车记录合并进 `notes.backup`，并记 `prepare_to_publish_seconds`。

`backup.sh` 每段写一行 NDJSON，退出时合成 `/var/backups/powersystem/<run>.timing.json`，覆盖 dump、复制、校验、压缩、加密、两次上传（含 `megabytes_per_second`）与保留清理；失败时带 `failed_stage`/`failed_line`。开始前另有 `disk_headroom` 前检（默认要求 4 GiB 可用，`POWERSYSTEM_BACKUP_MIN_FREE_KB` 可调）。

```bash
python3 /opt/powersystem/stages/04-frontend/deploy/production-stack.py timing --limit 5
```

计时只记录阶段名、耗时、字节数与计数，不落命令输出、路径或凭据。

### 回退及中断恢复

发布门禁失败会自动回退并重新验收。进程中断或自动回退失败时，`pending.json`保留，后续发布被拒绝；不要删除该标记。下列命令优先完成未结束事务；没有未结束事务时回到当前版本的直接上一份已验收版本：

```bash
python3 /opt/powersystem/stages/04-frontend/deploy/production-stack.py rollback
```

回退保留所有业务数据和失败证据，不降级数据库。旧私有引用缺失或被改写时停止，由维护者先恢复相应材料，不用新凭据或新配置假冒旧版。未来门禁覆盖HTTPS/WSS与配置一致性，不主动调用模型；模型、容量、固定版本观察等专项按变更范围另行执行。

> 发布防回退的业务背景与流程见 [[notes/01-项目调研规划与开发建设记录/补充业务/05-生产发布防回退/01-设计与发布流程]]。具体运行命令继续在源码 README 维护，原阶段签收范围不变。

## V4-S09发布与恢复导航

[发布门禁与证据](v4-release-readiness.md)登记具体范围、影响、备份与回退、观察计划。仍只走本页统一production-stack入口；新增V4动作/运维/调度或本机档不能随发布启用。当前状态见[唯一状态](../../../notes/00-当前系统状态.md)。

S09产物目录须显式设置`RAG_ARTIFACT_HOST_DIR`，指向经定位核对的runtime持久目录；容器内固定`/artifacts`。V4运行或产物启用后，全备会先取知识快照，再核验并暂时停止RAG/Ops写者，完成库与文件快照后先恢复Ops再恢复RAG；失败会保留需要恢复的实例身份并使备份失败，不吞重启错误。该暂停窗口属于发布影响范围，必须在生产批次确认中说明。独立RAG库及真实文件先恢复核对，再进入后续隔离探针，不启动V4动作/调度worker。

[本人最小浏览器批次](v4-s09-browser-acceptance.md)只用于本机冻结候选。具体Linux镜像、源码归属、失败/恢复证据及保留清理情况登记在S09候选台账，不能用本机通过替代生产全备、发布门禁或实际观察。

### V4全备后的原门禁复核

V4全备会停复两个写者，`StartedAt`因此改变。`backup.sh`保留绑定当前已验指针、暂停前全服务状态及同ID复服状态的`.v4-maintenance.json`旁车；只有RAG/Ops启动时间可以改变，镜像、ID、重启次数及其他服务不变。

**full候选自带全备时**：publish按其回执完成两处基线判定。已发布版本的成功定时全备若只造成精确登记的停复变化，可用本页开头reconcile-backup短重核继承原功能验收，不默认重跑完整HTTPS；需要完整重验时仍用下列verify-backup。两者保持原published_at；失败或未知漂移按原事务诊断，不删除pending。实际工具启用状态只见唯一状态页。

```bash
python3 /opt/powersystem/stages/04-frontend/deploy/production-stack.py verify-backup \
  --backup /var/backups/powersystem/powersystem-<本次实际UTC>.tar.gz.gpg
```

压缩/上传失败时，已核实同ID复服的草稿回执只允许回直接父版，不可用于重新验收。恢复失败先按保留身份修复Ops→RAG，不伪造健康或删除pending；精确复服后按相应回执执行：

```bash
python3 /opt/powersystem/stages/04-frontend/deploy/production-stack.py rollback \
  --backup-maintenance /var/backups/powersystem/powersystem-<本次实际UTC>.tar.gz.gpg.v4-maintenance.json
```

普通发布/回退仍用本页既有命令。最终全备、适用隔离恢复和维护复核全部结束后，才重新固定连续30分钟观察窗口。新规范知识资料以冻结字节挂到`/run/v4-knowledge`，不覆盖旧资料挂载；该新根被控制器和实际RAG配置备份按文件摘要覆盖。

### 全量备份并行与压缩参数

在主副目标均已核实且非local-only时，新的backup.sh默认将同一密文并行上传既有两端，必须都成功；已安装pigz时默认4线程标准gzip，否则用单线程gzip。可显式设置POWERSYSTEM_BACKUP_PARALLEL_UPLOADS=0或POWERSYSTEM_BACKUP_GZIP_THREADS=1降低资源占用，旧的1和4配置继续有效。两端各记时，总时长按最大结束offset，不把重叠耗时相加；异常时保留归属进程与恢复记录，不冒充双副本成功。现有备份格式/范围不变，不扩出站；定时备份与普通发布分别管理。


## 独立VM生产主机与迁机验收

运行位置、VMID与DHCP实址只见[唯一状态](../../../notes/00-当前系统状态.md)；本批迁机事实见[[notes/04-系统部署发布备份与变更记录/部署与运维日志/01-部署基线与仓库发行/47-PVE宿主项目迁入独立虚拟机]]。源7070管理SSH继续到PVE宿主机，不能当新VM应用部署入口；新VM通过PVE/QGA或已登记内网角色管理。

`host-profile.py`在`/etc/powersystem/host-profiles.json`逐机绑定hostname、原始machine-id摘要、DMI UUID及virtualization，分别登记release/backup/restore/registry/observe/migration能力；不全局取消主机校验。源机停服栅栏和restart=no保留，目标新数据出现后禁止直接启源旧副本。密文中的主机登记仅作恢复证据，不自动启用。

初次冷迁只走受限`production-stack.py`阶段：`migration-stage --id <独立批次> --receipt <受保护实核收据>` → `migration-start --group data` → `migration-checkpoint --group data --evidence <实际检查>` → readonly组及其检查点 → 切两条项目web FRP并保留源SSH/其他代理 → background组 → `migration-accept`原完整门禁。每条命令均指定同一`--id`；收据须真实覆盖源停服/栅栏、双备份、13卷摘要、20固定镜像与所有权/角色文件，不能用离线PASS填充。

迁机基线无同机父版。最终全备维护重验只接受绑定该同一新VM基线的精确回执；它不豁免全服务重启。今后通过PVE页面迁移整台VM，虚拟盘随迁；关机迁移/重启仍须另建计划维护与完整重验，不能复用本次冷迁收据或把全体StartedAt变化冒充V4备份停复。目标资源、CPU及本地磁盘传输条件须当次检查，不承诺零中断。

备份保留实际RAG双环境引用、角色新key目录、原Executor布局、独立知识alias及真实产物。MA密文增加`host-runtime`控制工具/采集器/专属web FRP/显式角色引用和云端精确路由材料；所有内容仅归档校验、禁止自动激活。适用恢复仍按上文现行隔离脚本，V4 worker不启、原批准/工具/模型步骤不重放；独立RAG恢复仅挂本地模型、向量快照与新恢复只读DB角色。

### 原编排输入与冻结基线的一致性

迁机基线的冻结文件与继承的多文件编排参数须分别核对。`prepare`会固定实际镜像并去掉build；仅查看原始`compose config`中的历史镜像字段，不能据此推断统一入口会晋升这些镜像。但原输入仍应固定已验镜像、移除生产build声明，并保持挂载列表与冻结版本的精确顺序，避免误用原模板重建和前端快路径被顺序差异阻断。

修正只落在未冻结的运行模板：先逐件核原摘要，在本批受保护候选内解析并证明与已验冻结配置精确等值，再持部署锁留原件、安装和重验；不得修改已接受的manifest、host-identity、私有env或用放宽比较放行。前端发布成功后只将未冻结镜像覆盖模板中的frontend镜像同步为新active固定ID，复核完整解析精确等于新冻结配置。源宿主历史模板保留，不随目标VM整改覆盖。

`frontend-release-backup.py`与全量备份共用已登记主机的backup能力校验，校验root、机器四元组、项目范围与源机栅栏；不再用旧宿主机名判断新VM资格。该修复不改小包格式、加密或恢复过程。执行记录见[VM配置对齐与前端快发布](../../../notes/04-系统部署发布备份与变更记录/部署与运维日志/05-发布工具链与文档治理/48-VM配置源对齐与前端审批弹窗快发布.md)。

本轮feature-r1因新增身份/桥目录被umask收紧为0700启动失败，已完整自动回退compat。修正chmod0755并实际非root读取通过后，新增固定重发编号`v4-business-schedule-20261026-r2`，仍绑定同四服务及精确父版/原话授权；原失败候选不改写。此次兼容→能力两阶段及r2真实结果见唯一状态页与日志50，不能把延后备份变为后续发布默认。

### 全备的发布构件与实际配置闭合

`backup.sh`从当前active的冻结编排获取实际服务，避免原始单份Compose模板缺镜像或混入历史构建设置。两个SQLite队列通过`snapshot-production-queues.py`调用当前固定Ops镜像的一次性辅助器，用SQLite backup API采集；辅助器无网络、源卷只读、退出即删除，不要求生产编排包含`backup-helper`。

全备的`release-assets/`保存当前frontend固定镜像tar、五应用完整源码快照/构建回执与固定镜像映射；`verify-release-backup-assets.py`只验证来源、镜像内容及摘要，不激活生产指针。它不声称所有基础设施镜像和未列卷都已归档。MA的`actual_release_configuration`从四个实际服务的只读`/run/`挂载逐文件归档，含S06动作key；源限于runtime。两项既有`/etc/powersystem` API secrets只按精确source/target映射并核实与已有config归档摘要一致，不扩为任意文件读取。恢复逐份核哈，不挂生产凭据、不运行V4动作/调度worker。

任何采集阶段失败，复服后也保留真实失败计时和维护草稿，不能用草稿替代成功备份。若旧失败在保留草稿前退出，受限失败复服连续性检查仅在明确证据路径、原候选基线等于已验父版、无密文/无上传、仅RAG/Ops同ID/镜像/重启数变化时，允许下一次真实全备记录连续维护链；它不签发验收。仍须新的成功双端全备和原完整发布门禁，禁止把此机制当成通用漂移豁免。

## 遥测前滚发布（TR，2026-10-28）

实际状态见[唯一状态页](../../../notes/00-当前系统状态.md)，本轮授权与实际证据见[日志53](../../../notes/04-系统部署发布备份与变更记录/部署与运维日志/05-发布工具链与文档治理/53-遥测可靠性修复发布与前滚门禁.md)。标准自动回旧版不适用于014已提交且父版不兼容的情况：本轮所有者已明确接受失败时冻结遥测写入、保留pending并前滚恢复，不启动旧worker、不降schema。该选择不是跳过新全备、HTTPS/WSS或来源门禁。

先从明确40位提交与逐文件审查增量导出完整快照，通过source-build受控构建API/gateway两镜像；Prometheus/Alertmanager镜像保持，只变版本化只读规则与路由配置。新增遥测规则只路由空receiver，本轮不实发新外部通知；旧Feishu/Agent路由保持。计划内API停机和新遥测规则用精确30分钟静默，不静默磁盘或其他告警，成功激活前和失败恢复后均主动清除并GET证实过期；清理失败显式阻断验收并留恢复记录。

`--telemetry-transition`指向runtime下本轮独立证据JSON：format=1、kind=telemetry-forward-only/v1、固定candidate白名单r2/r3/r4、精确parent/source_sha256、failure_policy=freeze、实际用户选择、固定900秒离线预算、绑定main.go/协议实现摘要、同量级真实迁移与实际Linux排空/恢复证据、实际监控旧文件摘要与新配置摘要。prepare将其按字节冻结，篡改/缺项/源码不符均拒绝；仅四项服务/full新全备可用。

```bash
python3 /opt/powersystem/stages/04-frontend/deploy/production-stack.py prepare   --id tr-s04-20261028-r2   --image api=powersystem-api:tr-s04-20261028-r2   --image gateway=powersystem-gateway:tr-s04-20261028-r2   --service prometheus --service alertmanager   --telemetry-transition /opt/powersystem/runtime/<本轮独立批次>/telemetry-transition-evidence.json
# 然后按既有标准生成候选后的新全量加密备份并核对主备凭据；不复用旧例外。
python3 /opt/powersystem/stages/04-frontend/deploy/production-stack.py publish   --id tr-s04-20261028-r2   --backup /var/backups/powersystem/<当次实际新全备>.tar.gz.gpg
```

publish在原部署锁下先核无未授权长历史积压/既有维护，再停旧API/GW，使用候选API的实际`-migrate-only -migration-timeout=900s`入口，成功后才启动正常服务。普通API仍30秒；任何部分迁移/未知结果必须先锁读真实schema，不能以旧镜像健康代替兼容性。一次性迁移超时只停明确归属的容器，停止不明就保留pending拒绝回旧版。014前失败可走原父版恢复；014后/探测未知则冻结/停写，监控可单独回父版配置，不冒充业务恢复。

首次升级前旧schema全备仍按旧协议，不称GW冻结；S04生效后的全备继续走backup.sh：双重互斥（与发布互斥、备份自身串行），RAG/Ops暂停前先以维护共享锁排空遥测并干净停止GW，采集后同ID恢复并仅凭本轮updated_at所有权恢复原维护值。回执显式绑定GW前后状态，仅放行真实排空导致的启动时间变化；ID/镜像/重启/强杀/操作员后续改门均不豁免。失败保留冻结与恢复身份，不清队列或伪造成功。


### 队列大文件快照与失败停复核验

队列辅助器固定实际Ops镜像、无网络、源卷只读、1核/512MiB，按本轮名称/标签/镜像/输出绑定核验归属；完成、超时或取消均确认辅助器退出，不能只杀Docker CLI。SQLite固定WAL只读事务并分块复制，保留完整原行和quick_check，内部预算600秒/外层660秒；不清队列、不改时间、不暂停模拟器。无法证明退出则保留快照和恢复记录，备份失败。

失败备份若已规范同ID停复，仅对应24小时内失败计时、无密文/上传、完整before/after旁车可申请同版完整HTTPS/WSS重核：

```bash
python3 /opt/powersystem/stages/04-frontend/deploy/production-stack.py verify-resume --receipt /var/backups/powersystem/powersystem-<失败当轮UTC>.tar.gz.gpg.v4-maintenance.json
```

该命令不换服务/镜像、不签备份成功；门禁失败不改active，成功保留原published_at并留前后指针审计。之后仍须重新prepare并生成候选后的新全备；不能以失败旁车代偿备份。


### 同一已迁移候选的冻结恢复

`resume-telemetry`仅允许本轮tr-s04-20261028-r3和显式r1/r2/r3尝试。它保持原manifest、镜像、应用源码、pending和原候选后全备：证据绑定pending/冻结失败记录实哈、当前20服务身份、维护updated_at所有权和本次恢复工具摘要。只接受已完整迁移且新API/GW停止、其他服务健康的原现场，不重新迁移、不启动旧worker。源码来源/原全备/前置检查/排空仍验，恢复后完整HTTPS/WSS通过才移动active和清pending；失败重冻，旧失败和每次尝试记录均保留。恢复预算为120秒正常等待加60秒同实例启动等待，不降低ready标准。

```bash
python3 /opt/powersystem/stages/04-frontend/deploy/production-stack.py resume-telemetry --id tr-s04-20261028-r3 --attempt r1 --evidence /opt/powersystem/runtime/<本轮恢复工具批次>/resume-evidence.json
```

恢复工具按独立来源清单审查/安装，原r3应用快照和镜像不改；不能以修改原manifest、删除pending或切旧镜像代偿。每次计划静默独立命名并绑定本候选证明，UTC到期时间按Alertmanager实际毫秒精度比较，删除后GET证实expired。

### 已迁移现场的新镜像前滚修复

同候选恢复失败且应用代码需要修订时，仅本轮r3→r4支持`prepare-telemetry-repair`及`publish-telemetry-repair`。它们始终持原部署锁，绑定原active/pending/冻结失败实哈、20实例、维护updated_at及完成迁移/停止证明；不修改r3。新API/GW须从同一已审完整源码受控构建，原协议/大表迁移证据与实际新增验证分开登记。

```bash
python3 /opt/powersystem/stages/04-frontend/deploy/production-stack.py prepare-telemetry-repair --id tr-s04-20261028-r4 --evidence /opt/powersystem/runtime/<当轮>/frozen-repair-evidence.json --image api=<固定已审镜像> --image gateway=<同源固定已审镜像> --telemetry-transition /opt/powersystem/runtime/<当轮>/telemetry-transition-evidence.json
POWERSYSTEM_TELEMETRY_REPAIR_EVIDENCE=/opt/powersystem/runtime/production-stack/releases/tr-s04-20261028-r4/telemetry-repair.json bash /opt/powersystem/stages/04-frontend/deploy/backup.sh
python3 /opt/powersystem/stages/04-frontend/deploy/production-stack.py publish-telemetry-repair --id tr-s04-20261028-r4 --backup /var/backups/powersystem/<候选之后的新双端全备>.tar.gz.gpg
```

新全备使用原标准入口；pending仅在该严格绑定场景下可备份。API/GW保持停止、维护true不变，RAG/Ops仍按原冻结停复协议。备份标明实际失败候选的应用镜像/来源与未变active，不自动激活；任一身份/令牌漂移即拒绝。新全备双端成功、来源/预检/排空及原完整HTTPS/WSS通过后才激活r4并清pending；失败继续停止新写者、保留两次事务及现场。完整恢复演练缺口仅按本轮本人已选延后，不外推后续豁免。

### 常规固定解读与服务器手动演示

自动告警解读默认使用本机固定模板，普通每日五时槽不调用模型；原官方v2模型配置、证据校验与每任务最多两次预算保留。明确的手动model演示才进入该通道；旧已完成结果不重写。功能及业务边界见[补充业务05](../../../notes/01-项目调研规划与开发建设记录/补充业务/05-告警固定解读与手动演示/01-告警固定解读与手动演示.md)，实际部署只见唯一状态页。

以下命令在已登记业务VM执行，脚本动态核对active与实际API，再调用运行容器内本机命令；不在PVE宿主或电脑直接执行。创建会产生明确标记的演示告警、正常通知与解读任务。

```bash
# 默认：真正调用保留的模型API，返回alarm_id、request_id与详情路径。
python3 /opt/powersystem/runtime/production-stack/demo-alarm.py create --device INV-1001 --level major

# 零模型演示：通过相同业务和推送通道，便于先验证展示。
python3 /opt/powersystem/runtime/production-stack/demo-alarm.py create --device INV-1001 --analysis fixed

# 将ALARM_ID设为上述输出的编号；ACTOR为自己现有的admin/operator用户名。
python3 /opt/powersystem/runtime/production-stack/demo-alarm.py status --alarm-id "$ALARM_ID"
python3 /opt/powersystem/runtime/production-stack/demo-alarm.py set-state --alarm-id "$ALARM_ID" --state acked --actor "$ACTOR"
python3 /opt/powersystem/runtime/production-stack/demo-alarm.py set-state --alarm-id "$ALARM_ID" --state recovered
```

同一设备只能有一个活动演示；先恢复旧演示再重新创建。`unhandled`由新建触发：也可用`set-state --state unhandled --device INV-1001`（不带alarm-id）；不擦除旧确认/恢复审计。新演示使用新request-id，断线/未知执行结果重试应加`--request-id`并沿用首次打印的键，防止重复创建/重复模型消费。`status`只读实际任务状态/model_calls/result，不追加模型调用；确认/恢复不重新分析。演示不占每日五次限额，不改遥测、规则或原日程。外部通知来自原业务outbox，网页推送由运行API核对已提交记录后发出；客户端断线仍需按实际状态刷新，不能把只改数据库当作通道验证。
