---
tags:
  - 源网智联
  - 开发阶段4
  - 部署
---

# 第 15 周服务器部署

> 本目录编排 R730xd 上的阶段四演示环境。上级总索引见 [README.md](../README.md)，阶段笔记见 [04-第15周·部署上线](../../../开发阶段4-前端、上线与沉淀/04-第15周·部署上线.md)。数据库与模拟器使用新的持久卷，不迁移本机验收库。模型文件 `model.json`、`metadata.json` 来自本机被 Git 忽略的 `artifacts/stage3/model/`，上线时另行传至服务器 `runtime/model/`。

## 一、目录与配置

服务器项目目录为 `/opt/powersystem`，私有配置由 `init-server.sh` 首次生成到该目录 `.env`。脚本不会覆盖现有配置。数据库、JWT、Grafana 密码只存在于服务器私有文件中；备份口令另存 `/etc/powersystem/backup-passphrase`。飞书群机器人地址与签名密钥分别由运维人员在 R730xd 写入 `/etc/powersystem/feishu-webhook-url`、`/etc/powersystem/feishu-sign-secret`，均设 `chmod 600`，内容不写入 `.env`、命令历史、仓库或笔记。Compose 只把这两个文件挂给内网适配器；**首次 `compose config` 和 `up` 前须准备好文件**。飞书适配器源码与 Alertmanager 配置已独立归入 [阶段五](../../05-feishu-notification/README.md)，本目录保留服务器统一 Compose 编排。

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

公网演示地址为 `http://8.138.10.222:3667`。当前使用 HTTP 与合成演示数据；按项目所有者决定，域名和 HTTPS/WSS 延期，不能列为本轮通过。公网 Nginx 拒绝注册接口并对登录限速，触发时返回 429；演示账号只从服务器内部 API 操作。本轮已复测注册 403、限速 429 和两台服务器监听端口，证据见 P0 收尾记录；后续更新仍须复测。

## 三、监控、备份与恢复

Prometheus 每 15 秒采集 API、主机和容器指标，规则包含 API 不可用及磁盘使用率超过 80%。Grafana 看板由本目录配置自动装载。阶段五的 Alertmanager 配置与适配器将触发、恢复事件经 Compose 内网转为签名飞书消息；适配器不映射公网端口，发送失败返回 502 供 Alertmanager 重试。2026-09-29 已用真实 API 暂停/恢复核对 Prometheus firing、适配器两次 200 与群内两条消息；原始记录及复测边界见[阶段五验证笔记](../../../开发阶段5-飞书告警通知/02-验证与运维.md)。后续变更仍须复测，不得把配置校验写成已送达。

两台服务器 SSH 安全基线按“盘点现况 → 安装并验证公钥 → 保持第二管理会话 → `sshd -t` 校验 → 关闭 root 密码登录 → 从新会话复测”的顺序执行。保留原会话直至新会话和应急入口验证成功；复核 FRP、备份专用 SFTP 和原有代理不受影响。本轮按此顺序完成，两端 root 仅允许公钥；未来如要改用非 root 管理员，应先验证其 sudo 和应急访问。现场结果记入 [P0 收尾记录](../../../开发阶段4-前端、上线与沉淀/06-P0收尾与M4验收记录.md)。

云服务器使用 SFTP 专用账号 `powersystem-backup` 保存密文，R730xd 的私钥路径为 `/etc/powersystem/backup-ed25519`。R730xd 每天执行 `backup.sh`，云服务器每天清理超过 7 天的密文。脚本对 PostgreSQL 执行 `pg_dump`、对 TDengine 执行 `taosdump`，用 SQLite 在线备份网关和模拟器的持久队列，连同模型打包后以 GPG AES256 加密传至云服务器。失败时原始数据卷不变。

恢复验证只启动带独立卷的 `powersystem-restore-*` Compose 项目，不覆盖生产数据库：

```bash
bash stages/04-frontend/deploy/backup.sh
bash stages/04-frontend/deploy/restore-verify.sh /var/backups/powersystem/powersystem-<UTC时间>.tar.gz.gpg
```

恢复脚本输出隔离项目名、数据库行数和队列文件数，结果记录到第 15 周笔记。手动清理隔离演练资源前先确认其项目名，不能对生产 Compose 执行 `down -v`。

## 四、发布检查

每次更新先在本地运行前端、Go、Python 测试，备份当前数据，记录镜像版本；在服务器执行 Compose 配置校验和构建。上线后检查管理员/操作员权限、总览电量与健康分、设备实时值、告警确认、预测、WebSocket、Grafana、飞书通知和 3667 公网入口，再观察运行日志。回退时恢复上一版本源码和镜像，保持数据卷与备份不动；如新版本迁移了数据库，回退前核对旧版本是否能读取新增表。

## 相关笔记

- 阶段四总索引：[README.md](../README.md)
- 运维与使用：[平台运维与二次开发手册](../../../平台运维与二次开发手册.md) · [平台使用手册](../../../平台使用手册.md)
- 部署证据：[04-第15周·部署上线](../../../开发阶段4-前端、上线与沉淀/04-第15周·部署上线.md) · [05-运维与恢复](../../../开发阶段4-前端、上线与沉淀/05-运维与恢复.md)
- P0 收尾：[06-P0收尾与M4验收记录](../../../开发阶段4-前端、上线与沉淀/06-P0收尾与M4验收记录.md)
- 飞书通知：[阶段五运行说明](../../05-feishu-notification/README.md) · [阶段五验证笔记](../../../开发阶段5-飞书告警通知/02-验证与运维.md)
