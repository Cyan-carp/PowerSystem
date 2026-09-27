# PowerSystem · 新能源设备智能运维平台

本仓库按开发阶段管理代码与运行说明。根目录保留研究、需求、技术选型和架构设计笔记；每个开发阶段在 `stages/` 下拥有独立源码、服务配置、脚本与 README。跨阶段确实复用的接口或代码，经验证后再放入共享目录。

| 阶段 | 代码与运行说明 | Obsidian 笔记 | 状态 |
| --- | --- | --- | --- |
| 阶段一：数据链路 | [stages/01-data-chain/README.md](stages/01-data-chain/README.md) | [[开发阶段1-数据链路/00-索引·开发阶段1-数据链路]] | M1 已通过，一小时 2160/2160 对账 |
| 阶段二：后端主体 | [stages/02-backend/README.md](stages/02-backend/README.md) | [[开发阶段2-后端主体/00-索引·开发阶段2-后端主体]] | 开发与 M2 验证中 |
| 后续阶段 | 在 `stages/03-*` 等独立目录开发 | 按阶段建立索引与编号专题 | 尚未开始 |

阶段一的预检、测试、断电后收集和停止服务命令，均见[阶段一运行说明](stages/01-data-chain/README.md)。项目生成物与迁移方法见 [[开发阶段1-数据链路/05-项目文件位置与迁移清单]]。

公开仓库为 [Cyan-carp/PowerSystem](https://github.com/Cyan-carp/PowerSystem)，阶段一发布见 [阶段1-数据链路](https://github.com/Cyan-carp/PowerSystem/releases/tag/stage1-data-chain)。首次 M1 验收记录见 [[开发阶段1-数据链路/04-阶段一测试与交付]]。
