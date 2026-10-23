# Codex 与 Workbuddy 协作约定

在本仓库开始新的对话或任务时，必须先遵守以下约定：

1. 阅读 `shared-memory/README.md` 和 `shared-memory/workbuddy-collaboration.md`。
2. 阅读 `shared-memory/project-memory.md`，获取跨对话共享的项目长期记忆。
3. 每个新对话都在 `shared-memory/conversations/` 新建独立的 Markdown 记忆文件；不得复用其他对话文件。文件名使用 `YYYY-MM-DD-<简短主题>.md`，同日重名时追加 `-02`、`-03`。
4. 对话进行中，在形成重要决策、约束、已完成事项、验证结果或明确待办后，及时更新当前对话记忆；不要记录密码、令牌、私钥、完整隐私数据或未经脱敏的运行日志。
5. 只有对所有对话长期有效的稳定事实，才写入 `shared-memory/project-memory.md`；对话过程、临时判断和任务进度只写当前对话记忆。
6. 委派 Workbuddy 前，提示它先读取 `shared-memory/workbuddy-collaboration.md`、`shared-memory/project-memory.md` 和当前对话记忆文件，再执行任务。任务结束时，Workbuddy 可以把需要与 Codex 共享的结论直接追加到当前对话记忆，并在返回结果中说明写入内容；Codex 负责复核。未经 Codex 复核，Workbuddy 不得直接修改 `project-memory.md`。

正式工程约定见 `notes/02-工程规范边界与验收治理/项目工程规范/01-目录边界与笔记治理.md`。

凡涉及向服务器上传项目文件或生产部署，操作前必须阅读并遵守 `notes/02-工程规范边界与验收治理/项目工程规范/05-上传服务器规范.md`；每次上传当场核对并更新其中的服务器目录定位卡，不沿用旧批次路径。具体命令以 `stages/04-frontend/deploy/README.md` 为准。
