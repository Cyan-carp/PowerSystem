---
tags:
  - 开发实战
  - 阶段一
  - 环境
  - Git
  - Docker
---

# 01 · 环境与 Git

> 回到 [[00-索引·开发实战]]；对应路线图第 1 周。

## 本机启动

项目根目录是 `C:\Users\23103\Desktop\新能源设备智能运维平台\PowerSystem`。在此处执行 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\preflight-stage1.ps1`。脚本兼容 Windows 自带 PowerShell 5.1，也可在 PowerShell 7 运行。它检查 Docker Engine 和 Compose，启动 EMQX、TDengine，验证 MQTT 1883 端口及 TDengine REST SQL，准备 Python 虚拟环境并编译 Go 网关。首次拉镜像和下载依赖需要网络。

Compose 将服务端口只绑定 `127.0.0.1`，有独立 Docker 数据卷。TDengine 首次启动密码从根目录忽略的 `.env` 读取；预检在没有该文件时随机生成。不要在笔记、日志或 Git 中写入真实密码。可用 `docker compose ps` 查看状态；`docker compose stop` 停止服务。

## 数据库手工核验

脚本以 `deploy/tdengine/init.sql` 为每次运行创建独立数据库。手工 SQL 收发核验记录在短测的 `events.jsonl`、`metrics.csv`、`telemetry.csv` 和 `reconciliation.json`；完整查询语句在 [[03-Go网关与时序入库]]。一小时测试以独立数据库对账，避免短测数据混入。

## Git 约定

本地仓库 `main` 保存已验收的阶段一成果，`dev` 继续承载后续开发；功能工作在 `feature/stage1-*` 分支完成后合入 `dev`。提交身份只在此仓库配置为 `Carp <2310314536@qq.com>`。`.env`、虚拟环境、编译文件和 `artifacts/` 均被忽略；`go.sum` 和依赖锁定文件提交。未配置 GitHub 远端。首次一小时 M1 于 2026-09-27 审查通过后，`dev` 已合入本地 `main` 并标阶段完成标签，验收证据见 [[04-阶段一测试与交付]]。

迁移前先读 [[05-项目文件位置与迁移清单]]。Docker 卷和忽略文件不在 Git 历史中。
