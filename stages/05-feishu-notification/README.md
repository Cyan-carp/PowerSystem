---
tags:
  - 源网智联
  - 开发阶段5
  - 飞书通知
  - 源码运行说明
---

# 阶段五 · 飞书业务与监控告警运行说明

> 第一版将设备越限、AI 风险的飞书通知列为主要业务。阶段四 [Compose](../04-frontend/deploy/compose.yaml) 统一编排，阶段五适配器接收 Go API 的持久业务事件及 Alertmanager 监控事件。设计与验收见 [阶段五索引](../../开发阶段5-飞书告警通知/00-索引·开发阶段5-飞书告警通知.md)。源码实现与服务器送达必须分别核对。

## 一、文件与链路

| 文件 | 用途 |
| --- | --- |
| `feishu-adapter/app.py` | 接收业务事件和 Alertmanager Webhook，校验内部令牌、生成文本、签名并调用飞书群机器人 |
| `feishu-adapter/test_app.py` | 签名载荷、监控及业务状态文案测试；不访问真实机器人 |
| `feishu-adapter/Dockerfile` | 适配器容器构建 |
| `deploy/alertmanager.yml` | 接收组、聚合与恢复通知、内网适配器地址 |
| `../02-backend/deploy/postgres/004_business_notifications.sql` | 业务通知持久投递表及待发索引 |
| `../02-backend/internal/api/business_notify.go` | Go API 后台投递与退避重试 |
| `deploy/verify-business-notifications.py` | 现场使用合成规则核对设备事件、失败重试及配置关闭；会发送测试群消息 |

业务链路为 `设备/AI 告警事务 → PostgreSQL outbox → Go API 后台 → feishu-adapter /business → 飞书群`；监控链路为 `Prometheus → Alertmanager → feishu-adapter /alertmanager → 飞书群`。两类事件在消息标题区分，业务告警触发、确认、自然恢复和配置关闭各有唯一事件键。发出失败保留在 outbox 重试；网络确认不确定时可能重复送达，事件 ID 可用于识别。

## 二、测试与部署入口

#### 步骤 1 · 在本机运行适配器测试

```bash
cd stages/05-feishu-notification/feishu-adapter
python -m unittest -v test_app.py
```

> 测试不读取服务器密钥，也不发送群消息。

#### 步骤 2 · 在 R730xd 检查私有配置和 Compose

在服务器 `/opt/powersystem` 执行；仅检查文件是否存在及权限，不打印内容。运维人员通过安全渠道准备 `/etc/powersystem/feishu-webhook-url` 与 `/etc/powersystem/feishu-sign-secret`；`init-server.sh` 为内部业务接口生成独立令牌 `/etc/powersystem/business-notify-token`。三个文件均为 root 所有、权限 600。

```bash
bash stages/04-frontend/deploy/init-server.sh
test -f /etc/powersystem/feishu-webhook-url
test -f /etc/powersystem/feishu-sign-secret
test -s /etc/powersystem/business-notify-token
stat -c '%a %U %n' /etc/powersystem/feishu-webhook-url /etc/powersystem/feishu-sign-secret /etc/powersystem/business-notify-token
docker compose --env-file .env -f stages/04-frontend/deploy/compose.yaml config --quiet
```

#### 步骤 3 · 构建并检查通知服务

> 构建会更新运行中的 API 与适配器；变更前先做加密备份并核对当前镜像。真实触发测试会向群发送通知，应在约定的测试时段操作。

```bash
docker compose --env-file .env -f stages/04-frontend/deploy/compose.yaml up -d --build feishu-adapter alertmanager api
docker compose --env-file .env -f stages/04-frontend/deploy/compose.yaml ps feishu-adapter alertmanager api
docker compose --env-file .env -f stages/04-frontend/deploy/compose.yaml exec -T postgres psql -U powersystem -d powersystem -Atqc "SELECT to_regclass('public.business_notification_outbox') IS NOT NULL"
```

部署后用可控设备规则和 AI 风险场景分别检查触发、确认、恢复、重复/失败重试，以及群内可见性。2026-09-29 已在 R730xd 用 `verify-business-notifications.py` 核对设备事件五项、适配器停机重试及机器人 HTTP 成功响应；随后真实模型对合成设备产生 AI 高风险与低风险恢复，两条业务事件均标记发送成功。项目所有者确认群里可见设备和 AI 新消息，见 [阶段五验证记录](../../开发阶段5-飞书告警通知/02-验证与运维.md)。机器人 URL、签名密钥与内部令牌不写入仓库、日志摘要或公开笔记。

## 相关笔记

- 专题索引：[开发阶段5-飞书告警通知](../../开发阶段5-飞书告警通知/00-索引·开发阶段5-飞书告警通知.md)
- 统一部署：[阶段四服务器部署说明](../04-frontend/deploy/README.md)
- 工程规范：[项目工程规范](../../项目工程规范.md)
