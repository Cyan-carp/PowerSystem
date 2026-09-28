# PowerSystem · 新能源设备智能运维平台

本仓库按开发阶段管理代码与运行说明。根目录保留研究、需求、技术选型和架构设计笔记；每个开发阶段在 `stages/` 下拥有独立源码、服务配置、脚本与 README。跨阶段确实复用的接口或代码，经验证后再放入共享目录。

首次接手请按 [平台使用手册](平台使用手册.md) → [平台运维与二次开发手册](平台运维与二次开发手册.md) → [项目验收与收尾清单](项目验收与收尾清单.md) 阅读。运行证据位置见 [运行产物说明](运行产物说明.md)，各阶段命令从下表的源码 README 进入。阶段四部署入口是 [部署说明](stages/04-frontend/deploy/README.md)。

本仓库公开的是源码、配置模板和笔记；`artifacts/` 中的模型、测试账本和截图，以及服务器私有 `.env` 与数据卷，不随 Git 提供。首次部署前须先生成模型并准备私有配置。当前演示使用合成设备数据，不作为真实场站运维依据。

| 阶段 | 代码与运行说明 | Obsidian 笔记 | 状态 |
| --- | --- | --- | --- |
| 阶段一：数据链路 | [stages/01-data-chain/README.md](stages/01-data-chain/README.md) | [阶段一索引](开发阶段1-数据链路/00-索引·开发阶段1-数据链路.md) | M1 已通过，一小时 2160/2160 对账 |
| 阶段二：后端主体 | [stages/02-backend/README.md](stages/02-backend/README.md) | [阶段二索引](开发阶段2-后端主体/00-索引·开发阶段2-后端主体.md) | M2 已通过，两小时 4320/4320 对账；最终代码短回归 38/38 用例 |
| 阶段三：AI 预测 | [stages/03-ai-prediction/README.md](stages/03-ai-prediction/README.md) | [阶段三索引](开发阶段3-AI预测模块/00-索引·开发阶段3-AI预测模块.md) | 核心链路分段验证；完整 M3 自动验收与公开数据验证待补 |
| 阶段四：前端、上线与沉淀 | [stages/04-frontend/README.md](stages/04-frontend/README.md) | [阶段四索引](开发阶段4-前端、上线与沉淀/00-索引·开发阶段4-前端、上线与沉淀.md) | 第 13、14 周前端和第 15 周服务器部署已实施；第 16 周与 M4 待验收 |

阶段一的预检、测试、断电后收集和停止服务命令，均见[阶段一运行说明](stages/01-data-chain/README.md)。阶段一隔离短回归 180/180 通过；现有 TDengine vnode 已满，标准脚本新建验收库受限，详情见 [M2 测试与交付](开发阶段2-后端主体/04-M2测试与交付.md)。项目生成物与迁移方法见 [文件位置与迁移清单](开发阶段1-数据链路/05-项目文件位置与迁移清单.md)。

公开仓库为 [Cyan-carp/PowerSystem](https://github.com/Cyan-carp/PowerSystem)，阶段一发布见 [阶段1-数据链路](https://github.com/Cyan-carp/PowerSystem/releases/tag/stage1-data-chain)。首次 M1 验收记录见 [阶段一测试与交付](开发阶段1-数据链路/04-阶段一测试与交付.md)。
