# V4-S04 本机工作台验收

仅用于独立合成环境，不是生产启动或部署入口。当前状态与正式判定见仓库 `notes/00-当前系统状态.md` 和 S04 测试与交付；S03 的模型质量、延迟与未签收旧套件边界继续保留。

## 环境与运行

- 独立 PostgreSQL：`127.0.0.1:25434/v4_s04_20261023_r1_test`，测试用户 `s04_test`；不得改成生产或历史库。经 `Store.open` 应用迁移。
- 阶段8夹具：在仓库根运行 `artifacts/v4-智能助手/S02-回答质量-20261023-r1/venv/Scripts/python.exe -X utf8 stages/08-agent-rag/scripts/accept-v4-workbench.py`。仅监听 `127.0.0.1:28941`。
- Go网关宿主：`S04_BROWSER_SERVE=true`、`S04_TEST_DSN` 指向上述库，运行 `go test ./internal/api -run '^TestS04BrowserGateway$' -timeout 0`；仅监听 `127.0.0.1:28942`，复用真实 JWT 鉴权与 runs/历史代理。合成登录仅存在此测试宿主，不导入生产路由。
- 前端：`VITE_API_TARGET=http://127.0.0.1:28942`，`vite --host 127.0.0.1 --port 5174 --strictPort`；建议使用已登记的 Node 24 运行时。
- HTTP检查：从仓库根运行 `stages/08-agent-rag/scripts/accept-v4-workbench-http.py`。官方样例仅从候选登记读取 `stage8_assistant` 凭据引用，官方 DeepSeek / deepseek-flash / thinking disabled；不复制密钥，不改生产模型。

## 浏览器检查

真实浏览器消费 Go 网关的 HTTP/SSE，使用 CUA 记录 DOM、轮次/运行绑定与截图；不能以 HTTP 202 或截图单独替代结果核对。桌面 1440×1000、窄屏390×844；结束恢复浏览器默认视口。

`/drill/*` 只存在显式隔离夹具：丢失回执、重复流帧、工具返回前后断流与短时503、快照延迟至事件过期、快照不可用、投影状态与旧历史。审批/产物/未知写结果均为合成投影显示，不构成新批准、文件生成、下载或写执行能力验收。浏览器完整断网由真实TCP断流和HTTP不可用窗口模拟，不改变主机网络设置。

## 证据与停止

证据位于 `artifacts/v4-智能助手/S04-工作台-20261023-r1/`，包含起始逐文件摘要、S03对照、各失败/中间批、最终测试、HTTP与浏览器记录、交付增量和清理记录。重跑须先保留旧报告，不覆盖失败为成功。

先核对 `/drill/identity` 的实例与PID，再停止拥有的服务；夹具支持 `POST /drill/shutdown`。仅停止本轮登记的 Go/Vite/PG 进程；不清理其他对话或原S02凭据。临时运行时保留至2026-10-30，验收证据保留至2026-11-22或交接复核完成（取较晚者）；本轮不删除测试库。
