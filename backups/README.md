# 备份归档

> 用途：集中存放**大版本**的全量备份，并登记「小改动暂不单独备份」的事项。
> 位置：项目根目录 `backups/`。实际备份包不入库（见根 `.gitignore`），只保留本说明。

## 约定

1. **小改动不单独备份**：只在本文件登记一行（日期、改动、落点、回退方式）。生产镜像本身即可回退，无需额外打包。
2. **大版本统一全量备份**：发布大版本前，在本目录建 `backups/<版本号>-<日期>/` 存放完整备份，不再散落到别处。
3. 备份包体积大且含环境信息，**不要提交 git**；目录下除本 README 外均被忽略。
4. 每次全量备份必须附 `SHA256SUMS`，并在本文件的「全量备份」表登记。

## 小改动登记（未单独备份）

| 日期 | 改动 | 本机 | 服务器 | 回退方式 |
| --- | --- | --- | --- | --- |
| 2026-10-26 | 时长显示由「137时14分28秒」改为 `137:14:28`（统一 `HH:MM:SS`，补零、秒字去掉）；改 `src/lib/format.ts` 的 `durationText()` 与对应单测 | 已改，`vitest` 5/5、`npm run build` 通过 | 新 VM `powersystem-prod`（VM117 / DL380G9）：源码 `/opt/powersystem/stages/04-frontend/src/lib/format.ts` 已同步；frontend 容器静态资源已整包替换并 `nginx -s reload` | 重建 frontend 容器即回到镜像内原资源（`docker rm -f powersystem-stage4-frontend-1` 后按原 `compose.json` 起回），源码用 git 回退 |

> 说明：本次未走 `production-stack.py prepare / backup / publish`，按所有者要求跳过大版本备份流程；`runtime/production-stack` 的发布记录仍指向 `frontend-cloud-20261026-r1`（镜像 `43ffad3c05bc`），仅容器内静态文件被替换。

## 全量备份登记

| 版本 | 日期 | 存放目录 | 内容 | 校验 |
| --- | --- | --- | --- | --- |
| （待大版本） | — | — | — | — |

大版本备份建议包含：

- 源码快照（`git archive` 对应 tag + 未提交改动清单）
- 前端构建产物与镜像 ID、后端各服务镜像 ID
- PostgreSQL / TDengine 数据导出（按现行备份脚本）
- `compose.json`、`.env`、`/etc/powersystem/*` 密钥文件清单（密钥本体另存，勿入库）
- 备份包 `SHA256SUMS`
