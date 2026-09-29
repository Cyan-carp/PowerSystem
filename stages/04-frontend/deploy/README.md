---
tags:
  - 源网智联
  - 开发阶段4
  - 部署
---

# 第 15 周服务器部署

> 本目录编排 R730xd 上的阶段四演示环境。上级总索引见 [README.md](../README.md)，阶段笔记见 [04-第15周·部署上线](../../../开发阶段4-前端、上线与沉淀/04-第15周·部署上线.md)。数据库与模拟器使用新的持久卷，不迁移本机验收库。模型文件 `model.json`、`metadata.json` 来自本机被 Git 忽略的 `artifacts/stage3/model/`，上线时另行传至服务器 `runtime/model/`。

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

旧公网演示地址为 `http://8.138.10.222:3667`。2026-09-29 第一版现场已为 `8.138.10.222:443` 安装可信 IP 证书：云端 [cloud-ip-https.conf](cloud-ip-https.conf) 在 Nginx 终止 TLS，向本机 FRP 端口转发；[cloud-public-firewall.service](cloud-public-firewall.service) 拦截外部直连 3667；[cloud-cert-renew.timer](cloud-cert-renew.timer) 定时续期。外部证书校验和 `/api/v1/ping` 成功，旧 HTTP 直连超时。域名不要求。更换云主机、IP 或续期方式后须重新核对证书、Nginx、IPv4/IPv6 防火墙和 WebSocket，不能只复制此配置就视为安全通过。现场明细见[阶段六测试与交付](../../../开发阶段6-公网安全与恢复能力/03-阶段六测试与交付.md)。

## 三、监控、备份与恢复

Prometheus 每 15 秒采集 API 和主机指标，规则包含 API 不可用及磁盘使用率超过 80%；已移除需要宿主机高权限挂载的 cAdvisor。Grafana 看板由本目录配置自动装载。阶段五的设备/AI 业务事件由 PostgreSQL 投递表交给内网适配器，失败会重试；平台监控事件仍由 Alertmanager 发送。2026-09-29 旧 API 暂停/恢复与新业务链路均已核对，见[阶段五测试与交付](../../../开发阶段5-飞书告警通知/03-阶段五测试与交付.md)。

两台服务器 SSH 安全基线按“盘点现况 → 安装并验证公钥 → 保持第二管理会话 → `sshd -t` 校验 → 关闭 root 密码登录 → 从新会话复测”的顺序执行。保留原会话直至新会话和应急入口验证成功；复核 FRP、备份专用 SFTP 和原有代理不受影响。本轮按此顺序完成，两端 root 仅允许公钥；未来如要改用非 root 管理员，应先验证其 sudo 和应急访问。现场结果记入 [P0 收尾记录](../../../开发阶段4-前端、上线与沉淀/06-P0收尾与M4验收记录.md)。

云服务器使用 SFTP 专用账号 `powersystem-backup` 保存密文，R730xd 的私钥路径为 `/etc/powersystem/backup-ed25519`。执行 `backup.sh` 前须显式设置 `POWERSYSTEM_BACKUP_TARGET` 或私有 `/etc/powersystem/backup-target`；脚本不含公开默认目标。备份包括 PostgreSQL、TDengine、网关与模拟器 SQLite 队列、模型、`.env` 与三个通知私有文件，整体以 GPG AES256 加密后传至接收端。备份口令须在 R730xd 之外另行保管，不包含在密文包内。现场已核对专用 SFTP 目录、更新前后两份密文的异地 SHA256、独立恢复数据与私有文件；`powersystem-backup.timer` 已启用。整机灾难恢复仍待项目所有者完成解密口令的独立离线副本，见 [阶段六测试与交付](../../../开发阶段6-公网安全与恢复能力/03-阶段六测试与交付.md)。

恢复验证只启动带独立卷的 `powersystem-restore-*` Compose 项目，不覆盖生产数据库：

```bash
bash stages/04-frontend/deploy/backup.sh
bash stages/04-frontend/deploy/restore-verify.sh /var/backups/powersystem/powersystem-<UTC时间>.tar.gz.gpg
```

恢复脚本输出隔离项目名、数据库行数、队列文件数及私有文件校验结果。新版本现场结果记录到第一版 M4 复验页。手动清理隔离演练资源前先确认其项目名，不能对生产 Compose 执行 `down -v`。

## 四、发布检查

每次更新先在本地运行前端、Go、Python 测试，备份当前数据，记录镜像版本；在服务器执行 Compose 配置校验和构建。上线后检查管理员/操作员权限、总览电量与健康分、设备实时值、告警确认、预测、WebSocket、Grafana、飞书通知和 `https://8.138.10.222/` 入口，再观察运行日志。回退时恢复上一版本源码和镜像，保持数据卷与备份不动；如新版本迁移了数据库，回退前核对旧版本是否能读取新增表。

## 相关笔记

- 阶段四总索引：[README.md](../README.md)
- 运维与使用：[平台运维与二次开发手册](../../../平台运维与二次开发手册.md) · [平台使用手册](../../../平台使用手册.md)
- 部署证据：[04-第15周·部署上线](../../../开发阶段4-前端、上线与沉淀/04-第15周·部署上线.md) · [05-运维与恢复](../../../开发阶段4-前端、上线与沉淀/05-运维与恢复.md)
- P0 收尾：[06-P0收尾与M4验收记录](../../../开发阶段4-前端、上线与沉淀/06-P0收尾与M4验收记录.md)
- M4 测试与交付：[07-M4测试与交付](../../../开发阶段4-前端、上线与沉淀/07-M4测试与交付.md)
- 飞书通知：[阶段五运行说明](../../05-feishu-notification/README.md) · [阶段五测试与交付](../../../开发阶段5-飞书告警通知/03-阶段五测试与交付.md)
- 安全恢复：[阶段六运行说明](../../06-security-recovery/README.md) · [阶段六测试与交付](../../../开发阶段6-公网安全与恢复能力/03-阶段六测试与交付.md)
