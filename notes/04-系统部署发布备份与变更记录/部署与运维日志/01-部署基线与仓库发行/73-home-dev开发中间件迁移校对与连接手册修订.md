---
tags: [源网智联, 部署运维, 开发环境]
status: 现行
scope: 部署运维
updated: 2026-10-30
---

# home-dev开发中间件迁移校对与连接手册修订

所有者已自行完成Docker迁移，本轮校对其桌面连接手册并授权修订、同步相关笔记。属于开发环境维护；不操作生产VM117，不重迁或清源，不修改服务器配置、密码或容器。

## 当前定位与检查

- home-dev经既有SSH别名连接DESKTOP-LHHD9GR；代码/Git保留本机，目标副本C:\home-dev\powersystem。
- 5个容器正常、PG/Redis/TD健康、EMQX状态started、Qdrant6集合。实际7个named mounts，端口仅回环。
- 远端还有Windows原生stage2-api，8080回环ping返回running/ready，独立于Docker容器启动。
- 临时偏移6端口隧道全部连通；PG协议返回合法N，Qdrant6集合。检查结束终止本次SSH进程，没有遗留临时隧道或临时文件。
- 开发PG总346台/未删3/软删343，inbox83162、alarms443，Redis105；仅当前快照，不证明迁移前后一致，不替换生产143台口径。

## 修订与唯一入口

具体命令统一到[[06-代码工作区导航与链接映射/stages-02-backend-deploy-home-dev-README|远程连接手册]]；正文已迁入stages/02-backend/deploy/home-dev/README.md，桌面PowerSystem-Docker远程连接手册.md改为入口指针。

补齐两种开发方式、MQTT偏移转发和RAG端口、7卷清单、隧道失败/断线检测、原生API重启边界；修正Compose/context与TTY绝对限制、Windows cmd单引号SQL、prune及免认证表述。旧preflight默认Compose up及回归停Broker均标明不适用日常home-dev；原本地隔离启动步骤仍保留适用范围。同步根/阶段README、运维手册及架构篇、唯一状态、长期记忆与两级日志索引。历史迁机与故障记录保持原事实，不全库替换历史“本地Docker”。

## 未验证与后续

尚无源库逐表/TD时序范围/Qdrant逐集合点数与别名对账，不能签数据完整一致。未做整机重启/休眠恢复或原生API自动启动验证。编译/启动示例为代码静态核对，未在本轮执行构建、迁移或应用启动；不会把文档同步当部署通过。开发中间件迁移现状见[[00-当前系统状态]]，生产指针不变。

## 相关

- [[00-索引·部署基线与仓库发行]]
- [[00-索引·部署与运维日志]]
- [[03-平台使用运维与二次开发手册/平台运维与二次开发手册]]

## 文档同步验证

545篇笔记的元数据/链接/代理/清单检查通过，图谱545节点、3961边、断链0；手册9段PowerShell示例语法解析通过，git diff --check通过。临时本机artifacts/update-home-dev-notes.py以System.IO.File.Delete直删，Test-Path=False；此前临时SSH隧道已终止。本轮没有远端临时上传或配置变更。桌面修订手册/校对报告及仓库笔记是交付正文，持续保留。旧桌面迁移计划文件路径现已不存在，未重建或覆盖。
