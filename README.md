# PowerSystem · 新能源设备智能运维平台

本仓库按开发阶段管理代码与运行说明。根目录保留研究、需求、技术选型和架构设计笔记；每个开发阶段在 `stages/` 下拥有独立源码、服务配置、脚本与 README。跨阶段确实复用的接口或代码，经验证后再放入共享目录。此 `main` 版本截至阶段三 M3；后续阶段在 `dev` 开发。

| 阶段 | 代码与运行说明 | Obsidian 笔记 | 状态 |
| --- | --- | --- | --- |
| 阶段一：数据链路 | [stages/01-data-chain/README.md](stages/01-data-chain/README.md) | [阶段一索引](开发阶段1-数据链路/00-索引·开发阶段1-数据链路.md) | M1 已通过，一小时 2160/2160 对账 |
| 阶段二：后端主体 | [stages/02-backend/README.md](stages/02-backend/README.md) | [阶段二索引](开发阶段2-后端主体/00-索引·开发阶段2-后端主体.md) | M2 已通过，两小时 4320/4320 对账；最终代码短回归 38/38 用例 |
| 阶段三：AI 预测 | [stages/03-ai-prediction/README.md](stages/03-ai-prediction/README.md) | [阶段三索引](开发阶段3-AI预测模块/00-索引·开发阶段3-AI预测模块.md) | 合成场景完整 M3 自动验收通过；公开数据已审计，真实场站提前预测未验证 |
| 后续阶段 | `stages/04-*` | 按阶段建立索引与编号专题 | 不包含在此 M3 版本；在 `dev` 开发 |

阶段一的预检、测试、断电后收集和停止服务命令，均见[阶段一运行说明](stages/01-data-chain/README.md)。阶段一隔离短回归 180/180 通过；现有 TDengine vnode 已满，标准脚本新建验收库受限，详情见 [M2 测试与交付](开发阶段2-后端主体/04-M2测试与交付.md)。项目生成物与迁移方法见 [文件位置与迁移清单](开发阶段1-数据链路/05-项目文件位置与迁移清单.md)。

公开仓库为 [Cyan-carp/PowerSystem](https://github.com/Cyan-carp/PowerSystem)，阶段一发布见 [阶段1-数据链路](https://github.com/Cyan-carp/PowerSystem/releases/tag/stage1-data-chain)，M3 正式发布见 [stage3-ai-prediction](https://github.com/Cyan-carp/PowerSystem/releases/tag/stage3-ai-prediction)。首次 M1 验收记录见 [阶段一测试与交付](开发阶段1-数据链路/04-阶段一测试与交付.md)。M3 的自动验收和外部数据边界见 [M3 测试与交付](开发阶段3-AI预测模块/04-M3测试与交付.md) 与 [公开数据审计](开发阶段3-AI预测模块/08-公开数据审计.md)。原始模型和运行证据在被 Git 忽略的 `artifacts/`，需在本机按[阶段三运行说明](stages/03-ai-prediction/README.md)复现。
