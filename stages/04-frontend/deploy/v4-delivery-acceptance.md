# V4-S05 本机产物交付补验入口

当前状态以仓库 `notes/00-当前系统状态.md` 为准，正式门槛见 S05 测试与交付和阶段交接。本轮人工浏览器补验已签收，方法和记录见证据manual-browser-20261024-r1及正式交接；下文保留有需要时的定向复验入口，无需重跑已签收项目。不要重复未受影响的历史大套件，不进入 S06，不上传部署、暂存、提交、推送。

## 独立环境

- PostgreSQL：`127.0.0.1:25435/v4_s05_20261023_r1_test`，用户 `s05_test`；数据目录 `%TEMP%/powersystem-v4-s05-20261023-r1/pgdata`。只读借用 S04 的 PostgreSQL 二进制；不得改用旧测试库或生产库。运行前核对保留期限和实际进程。
- Go 测试宿主：`stages/02-backend/internal/api/s05_delivery_fixture_test.go`，仅显式 `S05_BROWSER_SERVE=true`、`S05_TEST_DSN` 为上述专用库时监听 `127.0.0.1:28952`。通过真实 JWT/当前用户复核及生产 report handler 生成文件；TD REST 为合成样本夹具。编译后单独运行二进制须以 `stages/02-backend/internal/api` 为工作目录；正常 go test 自动在该目录运行。
- runs 服务：仓库根用明确 Python 运行时执行 `stages/08-agent-rag/scripts/accept-v4-delivery.py`，只监听 `127.0.0.1:28951`。Store.open 应用004迁移并重复核对；合成和官方分支共享真实 business_report 适配器。官方分支仅登记的 stage8_assistant 凭据引用，不读阶段7。
- Vite：`VITE_API_TARGET=http://127.0.0.1:28952`，`--host 127.0.0.1 --port 5175 --strictPort`。本轮使用已登记 Node24。生成开关仅本机开启，生产默认关闭。

## 补验重点

用合成登录的本人账号在助手提出明确范围：可用率、告警时效、发电量按站/设备、告警调查；产物区展开后分别下载 CSV/XLSX，确认实际保存文件并独立打开对照。本人不弹审批；切另一账号不得读取原文件；过期/损坏显示不可用，不能生成伪成功链接。

在新产物任务运行时先停止接收，再恢复、刷新、切会话；并发两个目标以不同速度完成，文件始终留在原问题下，单正文不重复。HTTP绑定/快照/乱序已经有本轮证据；浏览器补验只验证新增交界，不重复所有旧S04场景。工具策略停止时保留缺口，不规避或拿截图替代。

## 证据与清理

本轮证据 `artifacts/v4-智能助手/S05-业务分析-20261023-r1/` 为忽略目录。新增补验使用独立批号，不覆盖failed_batch、超时或现有下载。下载审计的stream_served仅表示服务端输出，不能宣称客户端保存。

先核对 `/drill/identity` 中 instance/PID 与环境登记，再停止本轮服务；runs 支持 `POST /drill/shutdown`。清理以cleanup.json为准，不假定旧服务仍运行。运行时/专用库保留至2026-10-30，证据至2026-11-22或复核完成（较晚者），不得删除原S02/S03/S04材料。
