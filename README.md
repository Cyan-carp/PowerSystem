# PowerSystem · 新能源设备智能运维平台

本仓库按开发阶段管理代码与运行说明。根目录保留研究、需求、技术选型和架构设计笔记；每个开发阶段在 `stages/` 下拥有独立源码、服务配置、脚本与 README。跨阶段确实复用的接口或代码，经验证后再放入共享目录。

项目级手册：[[平台使用手册]]（网页功能与简单测试） · [[平台运维与二次开发手册]]（架构、运维与改源码） · [[运行产物说明]]（`artifacts/` 文件清单）。阶段四目录总索引见 [前端与部署 README](stages/04-frontend/README.md)。

| 阶段 | 代码与运行说明 | Obsidian 笔记 | 状态 |
| --- | --- | --- | --- |
| 阶段一：数据链路 | [stages/01-data-chain/README.md](stages/01-data-chain/README.md) | [[开发阶段1-数据链路/00-索引·开发阶段1-数据链路]] | M1 已通过，一小时 2160/2160 对账 |
| 阶段二：后端主体 | [stages/02-backend/README.md](stages/02-backend/README.md) | [[开发阶段2-后端主体/00-索引·开发阶段2-后端主体]] | M2 已通过，两小时 4320/4320 对账；最终代码短回归 38/38 用例 |
| 阶段三：AI 预测 | [stages/03-ai-prediction/README.md](stages/03-ai-prediction/README.md) | [[开发阶段3-AI预测模块/00-索引·开发阶段3-AI预测模块]] | M3 入库、告警、恢复及重启去重联调已通过；公开数据独立验证待补 |
| 阶段四：前端、上线与沉淀 | [stages/04-frontend/README.md](stages/04-frontend/README.md) | [[开发阶段4-前端、上线与沉淀/00-索引·开发阶段4-前端、上线与沉淀]] | 第 13、14 周前端和第 15 周服务器部署已实施；第 16 周与 M4 待验收 |

阶段一的预检、测试、断电后收集和停止服务命令，均见[阶段一运行说明](stages/01-data-chain/README.md)。阶段一隔离短回归 180/180 通过；现有 TDengine vnode 已满，标准脚本新建验收库受限，详情见 [[开发阶段2-后端主体/04-M2测试与交付]]。项目生成物与迁移方法见 [[开发阶段1-数据链路/05-项目文件位置与迁移清单]]。

公开仓库为 [Cyan-carp/PowerSystem](https://github.com/Cyan-carp/PowerSystem)，阶段一发布见 [阶段1-数据链路](https://github.com/Cyan-carp/PowerSystem/releases/tag/stage1-data-chain)。首次 M1 验收记录见 [[开发阶段1-数据链路/04-阶段一测试与交付]]。
