# PowerSystem Docker远程连接手册

更新：2026-10-30（America/Los_Angeles；北京时间2026-10-31）。当前开发中间件位于home-dev，生产仍在独立VM117。本文件是后续本地启动后端、连接和管理开发Docker的固定命令入口。先读本文件再选择开发方式；本机默认使用home-dev远端依赖，本机Docker Desktop可保持关闭。

## 1. 环境与连接

- 本机保留代码、Git、文档及开发工具；本机Docker Desktop可关闭。
- SSH别名home-dev连接家用Windows电脑DESKTOP-LHHD9GR。地址由本机.ssh/config维护；无需重建context。不要把FRP跳板机当成目标机。
- 目标仓库C:\home-dev\powersystem；已有C:\home-dev\env.ps1和项目.env。密码不在手册中保存，不覆盖已有.env。
- 远端5个中间件容器，另有Windows宿主stage2-api进程监听127.0.0.1:8080；API不在这5个容器中。
- 生产VM117及公网部署不属于home-dev；生产身份、发布及权限仍按notes/00-当前系统状态.md。

```powershell
ssh -o BatchMode=yes -o ConnectTimeout=10 home-dev hostname
docker --context home-dev ps
```

已有context无需重复创建。只有context不存在时才执行：

```powershell
docker context create home-dev --docker "host=ssh://home-dev"
```

默认context仍可保持desktop-linux；本机引擎退出时默认docker ps失败是正常现象。日常显式--context home-dev，不全局切换，避免旧脚本操作错误环境。

## 2. 选择一种开发方式

### 方式A：本机前端连接远端现有API（优先）

先在一个PowerShell窗口保持隧道运行：

```powershell
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -L 127.0.0.1:18080:127.0.0.1:8080 home-dev
```

另开窗口，从仓库根目录执行：

```powershell
Invoke-RestMethod http://127.0.0.1:18080/api/v1/ping
Set-Location stages/04-frontend
$env:VITE_API_TARGET='http://127.0.0.1:18080'
npm run dev
```

依赖未安装时先npm ci。浏览器访问http://127.0.0.1:5173。该方式不需要本机数据库隧道或本机后端，也不需要先关闭占8080的mock，因为这里采用18080；但前端必须使用上述目标并重启Vite。

### 方式B：本机开发后端，连接远端中间件

保持同端口隧道：

```powershell
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 `
  -L 127.0.0.1:5432:127.0.0.1:5432 `
  -L 127.0.0.1:6379:127.0.0.1:6379 `
  -L 127.0.0.1:6041:127.0.0.1:6041 `
  -L 127.0.0.1:1883:127.0.0.1:1883 `
  -L 127.0.0.1:18083:127.0.0.1:18083 `
  -L 127.0.0.1:16333:127.0.0.1:16333 home-dev
```

本机已有.env须与迁移后的开发库凭据匹配，不打印密码。后端默认PG/Redis/TD/MQTT均回环同端口；RAG使用RAG_QDRANT_URL=http://127.0.0.1:16333。该方式运行本机应用会写远端开发库；不与远端采集/模拟器重复启动，先确认各进程职责。

旧preflight-stage1/2会自动Compose up，run-stage2-test等会停止/恢复Broker，不能作为home-dev的常规入口；start-stage2默认还会调用preflight。编译后端可独立执行以下命令，不会启动Docker、迁移数据库或启动应用：

```powershell
New-Item -ItemType Directory -Force artifacts/bin | Out-Null
Push-Location stages/02-backend
go build -o ../../artifacts/bin/stage2-api.exe ./cmd/api
go build -o ../../artifacts/bin/stage2-gateway.exe ./cmd/gateway
Pop-Location
```

若已确认远端应用职责、本机配置、数据库结构兼容且需要本机同时启动API与网关，再使用原start-stage2.ps1 -SkipPreflight；它仍会启动应用并写远端开发库，不是只读检查，也不是本次已实测的启动步骤。不要执行-migrate-only作为日常连接验证。

### 端口冲突时的偏移方案

```powershell
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 `
  -L 127.0.0.1:15432:127.0.0.1:5432 `
  -L 127.0.0.1:16379:127.0.0.1:6379 `
  -L 127.0.0.1:16041:127.0.0.1:6041 `
  -L 127.0.0.1:21883:127.0.0.1:1883 `
  -L 127.0.0.1:28083:127.0.0.1:18083 `
  -L 127.0.0.1:26333:127.0.0.1:16333 home-dev
```

本机后端.env中修改以下非敏感连接值；RAG设置放在其实际使用的环境配置中。不要同时保留两套本机监听。

```dotenv
POSTGRES_PORT=15432
REDIS_ADDR=127.0.0.1:16379
TDENGINE_URL=http://127.0.0.1:16041
MQTT_BROKER=tcp://127.0.0.1:21883
RAG_QDRANT_URL=http://127.0.0.1:26333
```

前端连接本机API时用VITE_API_TARGET=http://127.0.0.1:8080；连接远端API时用方式A的独立18080隧道。中间件迁移不意味着API也必须远程运行。

## 3. 中间件、端口与卷

| 服务 | 远端回环发布端口→容器端口 | 卷（实际完整名称） |
| --- | --- | --- |
| PostgreSQL | 5432→5432 | powersystem_postgres-data |
| Redis | 6379→6379 | powersystem_redis-data |
| TDengine | 6041→6041 | powersystem_tdengine-data、powersystem_tdengine-log |
| EMQX | 1883→1883、18083→18083 | powersystem_emqx-data、powersystem_emqx-log |
| Qdrant | 16333→6333 | powersystem-agent-rag_qdrant-data |

5容器合计7个挂载卷。日志卷与数据卷分开理解，不能按容器数推断卷数。实际凭据以已有私有配置为准；不把EMQX出厂密码当作当前密码，不在命令中粘贴真实密码。

## 4. 日常只读检查

```powershell
docker --context home-dev ps --format '{{.Names}} {{.Status}}'
docker --context home-dev logs --tail 50 powersystem-postgres-1
docker --context home-dev stats --no-stream
docker --context home-dev volume ls
docker --context home-dev exec powersystem-postgres-1 pg_isready -U powersystem -d powersystem
docker --context home-dev exec powersystem-postgres-1 psql -X -U powersystem -d powersystem -c "SELECT count(*) FILTER (WHERE deleted_at IS NULL) active_devices FROM devices;"
docker --context home-dev exec powersystem-redis-1 redis-cli dbsize
docker --context home-dev exec powersystem-emqx-1 emqx ctl status
ssh home-dev "curl.exe --fail --silent --show-error --max-time 10 http://127.0.0.1:8080/api/v1/ping"
ssh home-dev "curl.exe --fail --silent --show-error --max-time 10 http://127.0.0.1:16333/collections"
```

Redis当前容器已有REDISCLI_AUTH，命令因此无需再写口令，不代表Redis无认证。PG容器内socket目前允许该查询，不等于远程TCP免认证。TDengine先看health，不再使用远端cmd下单引号SQL的错误curl示例；需要SQL检查时用应用已有认证配置，不在终端参数泄露凭据。

Test-NetConnection仅检验本地TCP监听，不证明后端认证、库名或数据正确；配合实际协议/接口检查。交互exec -it在真实终端使用，自动化/重定向会缺TTY；该限制不是context特有。

## 5. Compose管理与备份

当前操作习惯是SSH进入目标机，在目标仓库执行Compose，减少副本和路径混淆。Compose支持远程context；文件/env由客户端解析，而bind源须在远端存在，不能把本机目录自动共享过去。当前3个Compose使用named volumes。不得因“context不支持Compose”的错误解释去更换引擎或全局上下文。

以下是会改变运行态的管理命令，不是健康检查，只在确需重建时使用。目标SSH默认为cmd：

```bat
cd /d C:\home-dev\powersystem
docker compose -p powersystem --env-file .env -f stages/01-data-chain/compose.yaml -f stages/02-backend/compose.yaml up -d --wait
docker compose -p powersystem-agent-rag -f stages/08-agent-rag/compose.yaml up -d --wait
```

--wait只对已有healthcheck的服务给健康结论；EMQX/Qdrant另查协议。不要down -v、volume rm，或为排障执行prune。system prune -a会删除未使用镜像，并非必然删除运行容器的5个镜像。

PG只备份示例（不会备份TD/Redis/EMQX/Qdrant，不代表完整恢复点）：

```powershell
ssh home-dev "if not exist C:\home-dev\backup mkdir C:\home-dev\backup"
ssh home-dev "docker exec powersystem-postgres-1 pg_dump -U powersystem -d powersystem --no-owner > C:\home-dev\backup\pg.sql"
# 先确认该导出命令成功，再下载到本机已存在且可写的目录。
scp home-dev:C:/home-dev/backup/pg.sql .\pg-home-dev.sql
```

Windows PowerShell 5.1不应直接管道传递二进制归档。全量备份还要包含各存储及其适用一致性/恢复校验；本次仅修文档，不新增备份或执行恢复。

## 6. 重启恢复与验收边界

先确认目标机联网/FRP，再SSH，再Docker引擎/5容器，最后分别检查原生API与依赖。Docker Desktop当前依赖用户登录启动；unless-stopped只能在引擎启动后恢复未被手动停止的容器，不能启动Windows原生API。整机重启、休眠恢复及API自动启动未实测，不写“重启必恢复”。

已验证：目标身份、5容器/7卷、3健康、EMQX、Qdrant6集合、API running、临时6端口隧道/PG协议/Qdrant。开发库当前346台总记录（3未删、343软删）、inbox83162、alarms443，Redis105键；这些是检查时快照，不作为固定基线。

尚缺：迁移前后逐表/时序范围/逐集合点数和别名对账，以及整机重启恢复。源库数据对账证据不齐时不声称完整一致；开发有效设备3台不套用生产143台。本地引擎已退出，因此没有重新启动它去核实已删除的源容器/卷。原手册“唯一副本”应以实际备份清单为准，不凭推测。

官方参考：https://docs.docker.com/engine/storage/bind-mounts/ 、https://docs.docker.com/compose/intro/compose-application-model/ 、https://docs.docker.com/reference/cli/docker/container/exec/
