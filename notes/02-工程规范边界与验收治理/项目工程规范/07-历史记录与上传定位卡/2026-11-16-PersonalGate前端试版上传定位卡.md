---
tags: [源网智联, 前端, 生产发布]
status: 现行
scope: 部署运维
updated: 2026-11-16
---

# PersonalGate 前端试版上传定位卡

父版 user-management-20261109-r1。上传前现场确认 DL380G9、VM117 powersystem-prod、active/pending、当前镜像及无 frontend 挂载。本批 runtime 与 release 目录均为空缺。5 个源文件按 LF 核父版基线后通过 PVE SSH / QGA 标准输入传至 VM117，无 PVE 落盘中转。

- 上传目录 `/opt/powersystem/runtime/personalgate-ui-20261116-r1/`，overlay.json 逐文件记录摘要。
- 构建上下文 `/opt/powersystem/runtime/source-builds/contexts/e3d993ad8bd66b39f88935c5b84a83e41c456166080437353f5a54f2263e86ca/`。
- 前端镜像 `sha256:06ab57e4562b087348971f37f0b6d2dda76ebd402e47cb314ace8ee6071b1a61`，构建回执 SHA-256 `be0f656860462edc65a80e528d70a27bf6ff6b4a47f6e238572eebc3e16ac8af`。
- 冻结配置、激活回执位于 `/opt/powersystem/runtime/production-stack/releases/personalgate-ui-20261116-r1/`。回退复用父镜像 `25ba7330…7664ca`。

发布与后续清理见 [[116-PersonalGate前端试版发布]]。

选版结果及清理已登记至 [[116-PersonalGate前端试版发布]]，本批上传目录已删除。
