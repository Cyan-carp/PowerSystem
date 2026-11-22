---
tags: [源网智联, 前端, 生产发布]
status: 现行
scope: 部署运维
updated: 2026-11-16
---

# 116 · PersonalGate 前端试版发布

UTC 2026-11-16 14:27:42，VM117 从 `user-management-20261109-r1` 切到 `personalgate-ui-20261116-r1`。源码叠加及目录见 [[2026-11-16-PersonalGate前端试版上传定位卡]]。

桌面侧栏常显短标签，总览新增运行标题，登录页移除装饰渐变，公共卡片、表格、控件与深浅主题应用 PersonalGate 配色。

常规 prepare 返回 running_baseline_drift。服务自 11-13 已重启；21 容器 ID、镜像均与父版一致。持部署锁沿父版冻结编排只替换 frontend，回执核对其余 20 服务状态一致。生产 status 返回 pending=false、images_match_accepted=true。manifest `d1d78dc45a11712fd8b768149602573a02030043d711c945621b5d38a668f646`。

所有者授权直接发布。门禁、测试、回归和新备份均未执行。

## 回退与清理

所有者选定旧版。2026-11-16 14:31:47 UTC 仅重建 frontend，已恢复 `user-management-20261109-r1` 与镜像 `25ba7330…7664ca`；21 服务镜像匹配、pending=false，其他 20 个服务状态一致。VM117 本批上传暂存与切换脚本共 10 文件已删除，已核目录不存在。试版冻结发布记录及其引用的构建物留档。本地五个前端文件已核对还原。
