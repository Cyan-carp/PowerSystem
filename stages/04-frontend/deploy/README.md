# 第 15 周服务器部署

本目录编排 R730xd 上的阶段四演示环境。阶段笔记见 [[开发阶段4-前端、上线与沉淀/04-第15周·部署上线]]。数据库与模拟器使用新的持久卷，不迁移本机验收库。模型文件 `model.json`、`metadata.json` 来自本机被 Git 忽略的 `artifacts/stage3/model/`，上线时另行传至服务器 `runtime/model/`。

## 目录与配置

服务器项目目录为 `/opt/powersystem`，私有配置由 `init-server.sh` 首次生成到该目录 `.env`。脚本不会覆盖现有配置。数据库、JWT、Grafana 密码只存在于服务器私有文件中；备份口令另存 `/etc/powersystem/backup-passphrase`。不要将两者上传公开仓库。

从服务器项目目录执行：

```bash
bash stages/04-frontend/deploy/init-server.sh
docker compose --env-file .env -f stages/04-frontend/deploy/compose.yaml config --quiet
docker compose --env-file .env -f stages/04-frontend/deploy/compose.yaml up -d --build
docker compose --env-file .env -f stages/04-frontend/deploy/compose.yaml ps
curl -fsS http://127.0.0.1:18080/api/v1/ping
```

前端仅绑定 R730xd 的 `127.0.0.1:18080`。PostgreSQL、TDengine、Redis、EMQX、AI、Prometheus 与 Grafana 均不经 FRP 对外开放。API 的 `/metrics` 只供 Compose 内部 Prometheus 抓取。Grafana 可通过 SSH 本地转发 `13000` 访问。

## FRP 入口

只在 R730xd 现有 `/opt/frp/frpc.toml` 追加下面一条代理；保留其他代理与云端 `frps` 配置。更新前备份原配置，使用现有版本的 `frpc verify -c` 校验，重启 `frpc` 后核对旧代理仍可用。

```toml
[[proxies]]
name = "powersystem-stage4-web"
type = "tcp"
localIP = "127.0.0.1"
localPort = 18080
remotePort = 3667
```

公网演示地址为 `http://<云服务器IP>:3667`。当前使用 HTTP 与合成演示数据；域名和 HTTPS 是明确保留的后续验收项。公网 Nginx 拒绝注册接口，注册演示账号只从服务器内部 API 操作。

## 监控、备份与恢复

Prometheus 每 15 秒采集 API、主机和容器指标，规则包含 API 不可用及磁盘使用率超过 80%。Grafana 看板由本目录配置自动装载。尚未配置邮箱或 Webhook 通知渠道。

云服务器使用 SFTP 专用账号 `powersystem-backup` 保存密文，R730xd 的私钥路径为 `/etc/powersystem/backup-ed25519`。R730xd 每天执行 `backup.sh`，云服务器每天清理超过 7 天的密文。脚本对 PostgreSQL 执行 `pg_dump`、对 TDengine 执行 `taosdump`，用 SQLite 在线备份网关和模拟器的持久队列，连同模型打包后以 GPG AES256 加密传至云服务器。失败时原始数据卷不变。

恢复验证只启动带独立卷的 `powersystem-restore-*` Compose 项目，不覆盖生产数据库：

```bash
bash stages/04-frontend/deploy/backup.sh
bash stages/04-frontend/deploy/restore-verify.sh /var/backups/powersystem/powersystem-<UTC时间>.tar.gz.gpg
```

恢复脚本输出隔离项目名、数据库行数和队列文件数，结果记录到第 15 周笔记。手动清理隔离演练资源前先确认其项目名，不能对生产 Compose 执行 `down -v`。

## 发布检查

每次更新先在本地运行前端、Go、Python 测试，备份当前数据，记录镜像版本；在服务器执行 Compose 配置校验和构建。上线后检查登录、设备实时值、告警确认、预测、WebSocket、Grafana 和 3667 公网入口，再观察运行日志。回退时恢复上一版本源码和镜像，保持数据卷与备份不动。
