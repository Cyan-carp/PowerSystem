---
tags:
  - 开发阶段1
  - 阶段一
  - 环境
  - Git
  - Docker
---

# 01 · 环境与 Git

> 回到 [[开发阶段1-数据链路/00-索引·开发阶段1-数据链路]]；对应路线图第 1 周。

本页回答两个问题：**怎么把环境一键跑起来**，以及**代码怎么管**。

## 一、本机启动

项目根目录是 `C:\Users\23103\Desktop\新能源设备智能运维平台\PowerSystem`。在此处执行：

```
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\stages\01-data-chain\scripts\preflight-stage1.ps1
```

> 脚本兼容 Windows 自带 PowerShell 5.1，也可在 PowerShell 7 运行。

| 脚本会做什么 | 说明 |
| --- | --- |
| 检查 Docker Engine 和 Compose | 首次拉镜像、下载依赖需要联网 |
| 启动 EMQX、TDengine | 端口只绑定 `127.0.0.1`，有独立数据卷 |
| 验证 MQTT 1883 端口及 TDengine REST SQL | 连通性核验 |
| 准备 Python 虚拟环境 | 隔离依赖 |
| 编译 Go 网关 | 产出可执行文件 |

## 二、密码与数据卷

| 项目 | 约定 |
| --- | --- |
| TDengine 首启密码 | 从根目录忽略的 `.env` 读取；预检在没有该文件时随机生成 |
| 密码纪律 | 不要在笔记、日志或 Git 中写入真实密码 |
| 服务状态 | 在仓库根目录执行 `docker compose --project-name powersystem --env-file .env --file .\stages\01-data-chain\compose.yaml ps` 查看；将 `ps` 改为 `stop` 可停止 |

## 三、数据库手工核验

脚本以 `stages/01-data-chain/deploy/tdengine/init.sql` 为**每次运行**创建独立数据库。手工 SQL 收发核验记录在短测的 `events.jsonl`、`metrics.csv`、`telemetry.csv` 和 `reconciliation.json` 中；完整查询语句见 [[开发阶段1-数据链路/03-Go网关与时序入库]]。一小时测试用独立数据库对账，避免短测数据混入。

## 四、Git 约定

| 分支/项 | 约定 |
| --- | --- |
| `main` | 阶段一 M1 已验收并合入，保留 `stage1-m1-passed` 标签 |
| `dev` | 承载阶段一整理和未来阶段的集成工作 |
| `feature/stage1-*` | 功能分支，完成后合入 `dev` |
| 提交身份 | 仅在此仓库配置为 `Carp <2310314536@qq.com>` |
| 忽略项 | `.env`、虚拟环境、编译文件、`artifacts/` 均忽略 |
| 提交项 | `go.sum` 和依赖锁定文件提交 |
| 远端 | `origin` 指向公开仓库 `https://github.com/Cyan-carp/PowerSystem.git` |

> 首次一小时 M1 已于 2026-09-27 审查通过，当时 `main` 与 `dev` 均指向合并提交 `0a367e8`；本次目录整理不会改写该提交或标签。

## 相关笔记

- 迁移前先读：[[开发阶段1-数据链路/05-项目文件位置与迁移清单]]
- 下一章：[[开发阶段1-数据链路/02-逆变器模拟器]]
