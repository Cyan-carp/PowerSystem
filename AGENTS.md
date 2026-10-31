# Codex 与 Workbuddy 协作约定

在本仓库开始新的对话或任务时，必须先遵守以下约定：

1. 先读 `notes/00-当前系统状态.md`：生产指针、阶段状态、设备口径与已知限制的**唯一事实源**。README 与各手册只保留指针，不得把 README 的状态描述当作部署基线。
2. 阅读 `shared-memory/README.md` 和 `shared-memory/workbuddy-collaboration.md`。
3. 阅读 `shared-memory/project-memory.md`，获取跨对话共享的项目长期记忆。
4. 每个新对话都在 `shared-memory/conversations/<YYYY-MM-DD>/` 新建独立的 Markdown 记忆文件（按天建文件夹，同一天的放一起，不跨天混放）；不得复用其他对话文件。文件名使用 `YYYY-MM-DD-<简短主题>.md`，同日重名时追加 `-02`、`-03`。
5. 对话进行中，在形成重要决策、约束、已完成事项、验证结果或明确待办后，及时更新当前对话记忆；不要记录密码、令牌、私钥、完整隐私数据或未经脱敏的运行日志。
6. 只有对所有对话长期有效的稳定事实，才写入 `shared-memory/project-memory.md`；对话过程、临时判断和任务进度只写当前对话记忆。
7. 委派 Workbuddy 前，提示它先读取 `shared-memory/workbuddy-collaboration.md`、`shared-memory/project-memory.md` 和当前对话记忆文件，再执行任务。任务结束时，Workbuddy 可以把需要与 Codex 共享的结论直接追加到当前对话记忆，并在返回结果中说明写入内容；Codex 负责复核。未经 Codex 复核，Workbuddy 不得直接修改 `project-memory.md`。

正式工程约定见 `notes/02-工程规范边界与验收治理/项目工程规范/01-目录边界与笔记治理.md`。

凡涉及向服务器上传项目文件或生产部署，操作前必须阅读并遵守 `notes/02-工程规范边界与验收治理/项目工程规范/05-上传服务器规范.md`；每次上传当场核对并更新其中的服务器目录定位卡，不沿用旧批次路径。具体命令以 `stages/04-frontend/deploy/README.md` 为准。

## 本地后端与Docker开发入口

所有者要求：后续在本机启动后端、连接或管理开发Docker前，先阅读并按 `stages/02-backend/deploy/home-dev/README.md` 执行。默认开发依赖使用 `home-dev`，不要直接沿用旧本地Docker预检/Compose启动步骤；按手册选择本机后端或远端API方式。生产VM117仍按唯一状态页及生产上传规范，不能套用home-dev命令。
