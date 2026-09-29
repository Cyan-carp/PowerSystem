---
tags:
  - 源网智联
  - 开发阶段5
  - 飞书通知
  - 源码运行说明
---

# 阶段五 · 飞书告警通知运行说明

> 本目录保存独立的飞书通知集成源码与配置。设计和验收笔记见 [阶段五索引](../../开发阶段5-飞书告警通知/00-索引·开发阶段5-飞书告警通知.md)。阶段四的 [Compose](../04-frontend/deploy/compose.yaml) 仍统一编排演示环境，并从本目录构建 `feishu-adapter`、挂载 `deploy/alertmanager.yml`；Prometheus 抓取和规则仍在阶段四部署目录。移动源码不更改原有服务名、消息内容或机器人凭据位置。

## 一、文件与链路

| 文件 | 用途 |
| --- | --- |
| `feishu-adapter/app.py` | 接收内网 Alertmanager Webhook，生成触发/恢复文本、签名并调用飞书群机器人 |
| `feishu-adapter/test_app.py` | 签名载荷和触发/恢复文案的本机测试；不访问真实机器人 |
| `feishu-adapter/Dockerfile` | 适配器容器构建 |
| `deploy/alertmanager.yml` | 接收组、聚合与恢复通知、内网适配器地址 |

链路为 `Prometheus 规则 → Alertmanager → feishu-adapter → 飞书群机器人`。目前覆盖 API 不可用与主机磁盘使用率规则；网页中的设备业务告警仍走 Go API、数据库和 WebSocket，**未自动发送到飞书**。

## 二、测试与部署入口

#### 步骤 1 · 在本机运行适配器测试

```bash
cd stages/05-feishu-notification/feishu-adapter
python -m unittest -v test_app.py
```

> 这两项测试不读取服务器密钥，也不发送群消息。

#### 步骤 2 · 在 R730xd 检查私有配置和 Compose

在服务器 `/opt/powersystem` 执行；仅检查文件是否存在及权限，不打印内容。首次部署前，运维人员须通过安全渠道准备 `/etc/powersystem/feishu-webhook-url` 与 `/etc/powersystem/feishu-sign-secret`，所有者为 root、权限为 600。

```bash
test -f /etc/powersystem/feishu-webhook-url
test -f /etc/powersystem/feishu-sign-secret
stat -c '%a %U %n' /etc/powersystem/feishu-webhook-url /etc/powersystem/feishu-sign-secret
docker compose --env-file .env -f stages/04-frontend/deploy/compose.yaml config --quiet
```

#### 步骤 3 · 构建并检查通知服务

> 构建会更新运行中的适配器；变更前核对现有 Compose 项目与上次镜像。真实触发测试会向群发送通知，应在约定的测试时段操作。

```bash
docker compose --env-file .env -f stages/04-frontend/deploy/compose.yaml up -d --build feishu-adapter alertmanager
docker compose --env-file .env -f stages/04-frontend/deploy/compose.yaml ps feishu-adapter alertmanager
```

本轮原始送达证据、失败边界和验收口径见 [阶段五验证记录](../../开发阶段5-飞书告警通知/02-验证与运维.md)。机器人 URL、签名密钥和真实测试口令只保存在服务器私有文件，不写入仓库、日志摘要或公开笔记。

## 相关笔记

- 专题索引：[开发阶段5-飞书告警通知](../../开发阶段5-飞书告警通知/00-索引·开发阶段5-飞书告警通知.md)
- 统一部署：[阶段四服务器部署说明](../04-frontend/deploy/README.md)
- 工程规范：[项目工程规范](../../项目工程规范.md)
