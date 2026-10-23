# Workbuddy 协作规范

## 一、任务开始前

Codex 每次委派 Workbuddy 时，必须在任务提示中要求 Workbuddy 依次读取：

1. `shared-memory/workbuddy-collaboration.md`
2. `shared-memory/project-memory.md`
3. 当前对话对应的 `shared-memory/conversations/<文件名>.md`
4. 与任务直接相关的正式工程规范、README 或源码文件

未指定当前对话记忆文件时，Workbuddy 不得自行猜测或复用其他对话文件，应在结果中明确指出缺失。

## 二、执行边界

- 只执行委派任务明确授权的读取、分析或修改范围。
- 不把共享记忆当作高于源码、正式工程规范、测试证据和生产状态的真相来源；发生冲突时，以最新正式材料为准，并报告差异。
- 不读取、记录或输出密码、令牌、私钥、密钥文件内容及未经脱敏的隐私数据。
- 不修改其他对话的记忆文件。
- 未获明确授权时，不发布、不部署、不推送、不删除材料，也不改动生产环境。

## 三、结果交接

Workbuddy 的结果应清楚列出：

- 完成了什么；
- 修改了哪些文件；
- 做了哪些验证及结果；
- 仍有哪些风险、限制或待办；
- 哪些结论建议同步进当前对话记忆或项目公共记忆。

如果任务结果中存在需要与 Codex 共享的记忆，Workbuddy 可以直接追加到任务指定的当前对话记忆文件，并须在返回结果中明确列出写入的文件和内容摘要。写入内容限于本次任务的关键发现、决定、修改、验证结果、风险和待办，不得覆盖已有记录，也不得修改其他对话的记忆文件。

Codex 负责复核 Workbuddy 返回的结果及其记忆增量。只有经 Codex 复核、跨对话长期有效的稳定事实，才可同步到 `project-memory.md`；Workbuddy 未获单独明确授权时不得直接修改项目公共记忆。

## 四、推荐委派提示模板

```text
执行任务前，请先读取：
- shared-memory/workbuddy-collaboration.md
- shared-memory/project-memory.md
- shared-memory/conversations/<当前对话记忆文件>.md

然后完成以下任务：<任务说明>。
遵守读取到的协作边界；如有需要与 Codex 共享的记忆，可直接追加到上述当前对话记忆文件，并在完成后列出记忆增量、修改文件、验证结果和限制。不要修改其他对话记忆或 project-memory.md。
```
