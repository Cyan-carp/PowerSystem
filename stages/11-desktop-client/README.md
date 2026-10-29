# 阶段十一 · Windows 桌面客户端

第五版源码、开发与验收命令唯一入口。当前状态见 [唯一状态页](../../notes/00-当前系统状态.md)，本阶段证据与判定见 [S03测试与交付](../../notes/01-项目调研规划与开发建设记录/05-第五版-Windows桌面客户端/02-分阶段实施/03-S03联合验收与团队交付/02-V5-S03测试与交付.md)。S02由所有者确认收尾，T20技术缺证保留；本轮只做S03，交接后停止。

## 能力与边界

0.2.0 / Windows x64 / 当前用户NSIS内部未签名候选。团队包固定 `https://www.cyancarp.cn/`；S02独立隔离包固定 `http://127.0.0.1:4175/`，只连本机合成夹具。两者不同应用标识、程序名、快捷方式与用户数据目录，不混称同一个安装包。S03本机Win11增量升级/回退；所有者确认第二台实际Win10，按首次安装/核心短验实际结果登记。原Win10/双系统完整矩阵取消。生产页面适配事实只见唯一状态页。

- 关闭窗口保留在托盘，首次说明；托盘可显示、隐藏、设置、显式退出。退出后重启须登录。
- v1五个窄方法；通知默认开、开机启动默认关，只存布尔偏好与窗口几何。受控窗口、顶层frame、精确来源双重核验；sandbox/隔离开，Node关，证书异常拒绝。
- 隐藏或未聚焦时提交通知；按内存会话代次+alarmId去重，锁屏采用固定通用文案。登录/注销/换号/401重置，旧通知回调失效。桌面不播放页面自录告警音，只由Windows通知发声；聚焦时保留页面提示。系统静音/免打扰优先，submitted不代表显示/已读；退出、休眠、断网不保证提醒，不补齐历史。
- CSV保留Bearer/状态/text-csv检查；助手产物保留归属、size/SHA256及原服务端失效检查。主进程原生选路径，在同目录随机.part暂存；完成时核received/total（若已知）/stat，仅当前会话可原子替换目标。取消/失败只清理自身临时文件；原生对话框分别说明成功、取消、失败。没有通用下载IPC，不以link.click当保存成功，不自动打开文件。
- 恢复只重连WebSocket、读取快照与重新附加已受理run ID，保留草稿/筛选。无整页自动刷新、重提任务、重放下载或批准。
- 不改后端API、数据库、模型、原批准权限；S03必要前端发布沿阶段四统一门禁。

## 源码与测试资产

| 文件 | 用途 |
| --- | --- |
| src/main.ts / preload.ts / policy.ts | 主进程、窄桥接、精确来源与窗口/frame |
| src/notification-session.ts / download-policy.ts | 内存代次去重、文件名/来源/大小策略 |
| src/preferences.ts / window-state.ts | 非敏感偏好与窗口位置 |
| scripts/build-isolated.cjs / package-isolated.cjs | 编译期独立来源与独立测试安装包 |
| scripts/s02-fixture.cjs | 本机合成REST/WebSocket及控制页，无生产上游 |
| tests/s02-runtime.cjs / s02-bootstrap.cjs | 真实Electron+阶段四构建集成；替换OS通知/对话框/启动项调用 |
| tests/s02-team-boundary.cjs | 团队编译五方法拒绝真实子frame与回环来源 |
| scripts/Collect-S02Evidence.ps1 | 真实用户视角安装/进程/摘要/偏好/固定码采证 |
| scripts/Owner-S02-Test.cmd / .cjs | ASCII/CRLF本人入口模板、固定服务就绪核验 |

S01原夹具与采证脚本保留作历史，不用于S02关闭/通知验收。所有S02产物与日志归Git忽略的 `artifacts/v5-desktop/s02-20261026-r1/`；原S01批次不改。

## 构建与技术验证（Codex执行）

工作目录为本页所在目录，使用Node>=22.12（本机24.22.1）。运行时无npm dependencies；Electron45.0.0/builder26.18.0/TS5.9.3沿用锁文件。首次安装或变更依赖才需npm ci；系统Node22.11不符合要求。

```powershell
$env:PATH = 'C:\Users\23103\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin;' + $env:PATH
$run = [IO.Path]::GetFullPath('../../artifacts/v5-desktop/s02-20261026-r1')
$env:ELECTRON_BUILDER_CACHE = Join-Path $run 'builder-cache'
$env:CSC_IDENTITY_AUTO_DISCOVERY = 'false'
node 'C:/Program Files/nodejs/node_modules/npm/bin/npm-cli.js' run typecheck
node 'C:/Program Files/nodejs/node_modules/npm/bin/npm-cli.js' test
```

先冻结阶段四构建，再启动夹具，测试期间不改被服务的构建：

```powershell
Push-Location ../04-frontend
node 'C:/Program Files/nodejs/node_modules/npm/bin/npm-cli.js' test
node 'C:/Program Files/nodejs/node_modules/npm/bin/npm-cli.js' run build -- --outDir ../../artifacts/v5-desktop/s02-20261026-r1/frontend
Pop-Location
node scripts/build-isolated.cjs
node scripts/s02-fixture.cjs
```

最后一项保持服务运行；另开终端执行：

```powershell
node tests/s02-runtime.cjs
node tests/s02-team-boundary.cjs
node 'C:/Program Files/nodejs/node_modules/npm/bin/npm-cli.js' run package:win
node scripts/package-isolated.cjs
node tests/package-smoke.cjs
```

每轮输出另存有序日志。runtime测试经真实preload/IPC、页面登录/注销/401、WebSocket事件与文件下载，但OS通知、原生保存框、开机项API使用测试替身，不能代签真实界面。模拟resume不是本人真实休眠。团队边界测试用S01仅测试协议处理器提供合成HTTPS页面，不访问生产，也不说明真实TLS异常已测。成品smoke用仅该进程不可达回环代理，不改系统网络；强制结束不等于正常退出验收。

团队与隔离包都启用asar/fuses、不含测试夹具/源码映射/秘密，无自动更新/发布。隔离脚本断言替换命中与唯一来源，测试来源不能通过运行参数或环境变量切换。团队asar静态检查全部JS不得含隔离来源，并核源摘要；独立测试包使用不同应用标识。

## 本人操作前的材料

由Codex先核安装包、SHA256.txt、installer-manifest.json与已装三文件摘要。本人不用手算摘要、理解JSON或运行命令。材料均在本轮目录：

- `S02-Test.cmd`：ASCII/CRLF，结束pause保留错误；检查4175是本轮服务，必要时启动隐藏的合成夹具，打开控制页。不改系统代理/hosts/证书。
- `isolated-package/PowerSystem-S02-Isolated-0.2.0-x64-Setup.exe`：先验此包；程序/快捷方式名称“源网智联 S02 隔离验收”。
- `package/PowerSystem-Desktop-0.2.0-x64-Setup.exe`：固定HTTPS团队候选，必要最后核托盘/保存/退出及已有服务端记录；没有线上页面适配的能力不能提前称生产通过。
- 控制页 `http://127.0.0.1:4175/__control`：只发合成告警、只中断测试服务、401及恢复；不产生生产动作。账号A/B，密码synthetic，勿输入真实凭据。

每组做完只回复简短结果。到“已打开/已隐藏/已退出/已卸载”节点暂停，由Codex采证并告诉你下一步；不自行跳到卸载，避免丢失安装态证据。

### 第1组 · 安装与托盘（T01/T02/T13部分）

1. 双击S02-Test.cmd，预期控制页显示“S02隔离控制台”，黑色窗口无错误。若报端口占用且不是本轮控制页，保持错误窗口反馈。
2. 双击独立隔离安装包。预期当前用户安装，不要求管理员权限；未签名提示如实记录，不绕过系统/组织策略。登录界面账号A、密码synthetic。
3. 回复“已安装并打开；安装是否提权：是/否；控制页是否正常：是/否”。Codex先核登记/路径/三摘要/进程。
4. 再按Codex指引点×，首次应说明保留托盘；确认后窗口隐藏，右下角托盘（可能在^折叠区）仍有图标。右键测试显示/隐藏/设置，暂不退出。回复“首次说明/托盘显示/菜单：正常或异常”。

### 第2组 · 通知（T03–T09/T21）

1. 账号A保持登录，在控制页点“5秒后新告警”，立即切回并聚焦客户端。预期页面提示出现、无系统通知、无自录音效。
2. 控制页再点延迟告警，立即点客户端×隐藏；预期Windows提醒，内容仅通用告警提示。先不要点击通知，控制页点“重复同一告警”，预期不再响/弹。
3. 点击原通知（或通知中心仍保留的条目），预期恢复窗口到对应告警详情。若系统没有显示，记录Windows通知/免打扰状态，不凭submitted判定。
4. 测未聚焦：保持客户端可见但聚焦控制页，点新告警。预期仍提醒。
5. 托盘设置切换通知关后，再发新告警；预期系统不提醒、页面仍有告警。恢复通知开。
6. Windows设置中临时开启免打扰或禁用本应用通知，再发一条；预期页面告警仍可用，不绕过Windows。恢复原Windows设置。无需更改整机网络。

反馈：“前台不弹/隐藏弹/未聚焦弹/重复不响/点击详情/关通知/免打扰：各正常或具体异常”。Windows可能按自身策略抑制通知，不补造通过。

### 第3组 · 文件三态与原摘要（T10–T12）

1. 隔离客户端统计报表页点“导出可用率CSV”，在原生框选一个容易找到的新文件，如桌面S02-save.csv。预期主进程提示保存成功，文件实际存在；回复文件位置，Codex核大小。
2. 再点导出，点保存框“取消”；预期“已取消保存”，不报失败、不重试，已有文件不变。
3. 保存失败由Codex准备只影响测试目录的拒绝写入位置后，再按指定路径保存；预期可读失败提示，无半成品、已有目标不变。不要自行改系统目录权限或填满磁盘。
4. 打开“定期任务”，选择已有合成运行；展开“产物状态”，下载s02-artifact.csv。预期仍由原生另存为完成；Codex把实际文件size/SHA256与fixture-manifest中原产物核对。本合成历史运行不是新建任务，也不证明真实服务端调度。

反馈：“CSV成功/取消/失败提示；助手文件已保存到……”。页面点击或后端审计不等于落盘成功。

### 第4组 · 设置、应用内换号与401（T13/T14/T18）

1. 托盘设置应初始通知开、开机启动关。切通知关；测试启动项时明确切开并由Codex核实际登记，再切回关。显式退出并回复“已退出，先不重开”。Codex核进程与启动项。
2. 重新打开，应需要登录，偏好保持。切通知开后登录A，隐藏发一条告警，保留该通知。
3. 通过页面“退出登录”注销，登录B；不退出应用。点击A时期旧通知，预期无旧详情跳转，或旧条目已清除。对B重复同ID应可作为新代次提醒。
4. 控制页点“下一请求返回401”；预期页面回登录。再登录B，旧通知无效。Codex核各会话入口固定事件，不能用重启代测换号。

反馈：“默认开/关；设置保持；应用内A→B；401；旧通知：正常或异常”。不要提供真实口令或令牌。

### 第5组 · 客户端局部断线与休眠（T15/T16）

1. 在智能助手输入草稿但不发送；控制页点“仅测试服务断线”，客户端显示断线。电脑、其他浏览器和生产不受影响。
2. 控制页点恢复，预期实时重新连接，草稿仍在，未增加任务、批准或下载。Codex对账夹具请求计数。
3. 实际休眠前保留草稿及测试入口。按Windows电源菜单“睡眠”，稍后唤醒；不拔扩展坞、不整体断网。预期自动重连、只读状态刷新，草稿仍在；没有自动发送、批准或弹保存框。模拟事件通过不能替代这一步。

反馈：“局部断线恢复/草稿保留/实际睡眠唤醒/有无自动操作：……”。

### 第6组 · 显式退出、服务端独立性与卸载（T17/T19/T20）

1. 从托盘“退出”，回复“已退出，先不重开”。预期窗口和托盘消失；Codex核目标进程0、偏好白名单及存储候选，重新打开须登录。
2. T19先只读检查本人已有启用定期任务，不为验收新建或批准。已有合适记录时，对比退出前后服务端计划与运行记录；没有/时槽未到则保留待验，不用合成ticks替代服务端任务执行。
3. T20核客户端退出期间已存在/新自然产生的服务端告警记录；重开仍能浏览，不要求补齐全部历史通知。不为验收制造生产业务写动作。没有实际记录就等待/如实未执行。
4. Codex确认材料齐全后，从Windows设置卸载隔离候选，回复“已卸载；是否提权：是/否”。由Codex核程序目录、HKCU/HKLM、快捷方式、进程/开机项、非敏感残留。团队包如需测试另按Codex分步执行，不混用包摘要。

## 采证与收尾（Codex执行）

真实用户视角采HKCU与进程；沙箱登记空不能作为已卸载证明。NSIS可能无InstallLocation，脚本仅在DisplayIcon是合法绝对路径且程序名匹配时定位目录。采证只输出摘要/文件大小、偏好三布尔和固定码；JWT候选计数不能独立证明没有凭据，须合并内存partition与退出重登。

```powershell
./scripts/Collect-S02Evidence.ps1 -Phase installed -Isolated
./scripts/Collect-S02Evidence.ps1 -Phase running -Isolated
./scripts/Collect-S02Evidence.ps1 -Phase closed -Isolated
./scripts/Collect-S02Evidence.ps1 -Phase uninstalled -Isolated
```

团队包去掉-Isolated。只清本轮精确定位的测试夹具/安装候选；用户数据默认保留，残留如实登记，不擅自删其他对话材料。入口模板要复制到本轮目录后运行，ASCII/CRLF和pause不得丢失。

交接须四件套、八项、五问、Workbuddy实际返回与逐项复核；未执行/失败/跳过/待反馈均原样记录。测试证据至少保留至2026-11-26或S02验收结束（二者较晚），清理前核引用。不执行S03或生产发布。

## 保存失败专用夹具（Codex执行）

只锁定本轮自建 `S02-locked-target.csv`，允许读写共享而不允许删除共享，供原生保存框选择覆盖；最终原子替换应失败，原目标保留。无需改目录ACL、填满磁盘或变更系统设置。启动前若目标/停止标记已存在，先复核旧证据，不覆盖重用。

```powershell
./scripts/Lock-S02SaveTarget.ps1
```

此命令保持锁定，最多30分钟；另一个终端完成本人保存失败反馈后，核下载失败固定码、目标原hash与.part清理，再释放本轮锁：

```powershell
New-Item -ItemType File -Path '../../artifacts/v5-desktop/s02-20261026-r1/stop-save-target-lock.flag'
```

如果系统原生框提前拒绝目标，按实际提示记录，不把取消当作主进程保存失败。夹具不入安装包，不影响生产与其他文件。

## 本人批量收尾采证（Codex执行）

本人希望批量完成时，先启动30分钟上限、精确隔离候选的只读监测器；会在运行/关闭/卸载稳定2秒后自动调用既有采证器，最多12份，不操纵窗口、不改变设置，卸载后结束。随后本人可一次完成局部断线/恢复、实际睡眠（若允许）/唤醒、显式退出和卸载，最后统一反馈。

```powershell
./scripts/Watch-S02OwnerEvidence.ps1
```

提前停止时，仅创建本轮精确停止标记：

```powershell
New-Item -ItemType File -Path '../../artifacts/v5-desktop/s02-20261026-r1/stop-owner-evidence-watch.flag'
```

电脑禁止重启；睡眠不能执行时如实记未执行，不用模拟resume替代。关闭和卸载之间至少停留8秒，保留自动采证时间。

## 真实服务端样本只读核对（Codex执行）

实际取证脚本保存在本轮忽略目录，只包含固定inventory/rag/alarms查询。经既有7070管理入口与PVE/QGA定位DL380G9/VM117，断言guest hostname=powersystem-prod；精确项目Postgres容器内BEGIN READ ONLY、10秒语句超时、SHOW transaction_read_only、COMMIT。只输出计数/时间/状态，不输出连接串/认证密封/业务正文，不上传项目文件。

```powershell
& 'C:/Users/23103/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' '../../artifacts/v5-desktop/s02-20261026-r1/server-readonly-probe.py' rag
& 'C:/Users/23103/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' '../../artifacts/v5-desktop/s02-20261026-r1/server-readonly-probe.py' alarms
```

该脚本绑定本次两段实际退出窗；新批次先重新核身份/时窗，不能沿用旧结论。没有有效任务或窗口内没有新增记录均不通过，不自动创建、恢复或批准任务。

## T16定向补测入口（R2）

`artifacts/v5-desktop/s02-20261027-r2/`沿用R1同SHA256的0.2.0隔离包与原样前端构建，证据独立。R2入口S02-T16-Test.cmd为ASCII/CRLF，自动核本批服务，固定4175不变。测试脚本的run-directory只选择本仓库artifacts/v5-desktop内证据目录，不修改原生包来源。

Codex准备检查入口：

```powershell
node --check scripts/s02-fixture.cjs
node --check scripts/Owner-S02-Test.cjs
node scripts/Owner-S02-Test.cjs --run-directory '../../artifacts/v5-desktop/s02-20261027-r2'
```

本人：打开R2入口并安装同摘要隔离包，A/synthetic登录后输入草稿不发送；控制页断开测试服务，确认断线；保持断线进入Windows睡眠30–60秒，唤醒后再点恢复测试服务，等实时重新连接并确认草稿/页面状态。保持客户端打开后统一反馈，由Codex核系统电源时刻与新ticket/WebSocket/只读请求时序，无任务/批准/下载重放。R2增加method/path/time合成请求日志，不记录认证或正文。不把睡眠时间延长本身当通过；必须实际观察到醒后恢复路径。

T19需核真正退出窗口时，由Codex启动只读监测器（精确两个桌面程序名，30分钟上限；首次关闭后重开即结束）。点窗口×只是隐藏，不作为退出。

```powershell
./scripts/Watch-S02TaskExit.ps1 -RunDirectory '../../artifacts/v5-desktop/s02-20261027-r2'
```

本人申请恢复已有暂停任务后仍须原审批界面本人批准。记下下次触发，托盘右键退出所有项目客户端，跨该时间后再打开网页核历史并暂停；Codex对照进程0窗口和真实slot/run/artifact时刻。监测器不改变计划或代点批准。

## T20定向代查（R3）

R3仅核自然新告警与当前退出状态；没有样本不能签收，不自动生成告警、改阈值或恢复任务。当前快照不等于期间连续退出，审计若有重开须重新绑定实际窗口。以下是本次固定查询；后续运行须用新证据目录并重新登记时窗，不覆盖R3。

```powershell
./scripts/Collect-S02Evidence.ps1 -Phase closed -Isolated -RunDirectory '../../artifacts/v5-desktop/s02-20261027-r3'
& 'C:/Users/23103/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' '../../artifacts/v5-desktop/s02-20261027-r3/server-readonly-probe.py' alarms
```

## S03执行入口与冻结候选

以下为S03本轮操作；上面S02分组和命令保留作历史，不原样重跑。S03复用冻结0.2.0安装包，不重新打包覆盖S01/S02；原electron-builder默认输出仍为S02路径，任何后续新包必须显式指定一个新目录再冻结版本和摘要。

- `artifacts/v5-desktop/s03-20261027-r1/`：本机回退/升级、团队交付、Workbuddy和正式材料。
- `s03-20261027-r2/`：中间前端候选与失败证据；`s03-20261027-r3/`：最终前端构建、兼容和发布。
- 团队包SHA256：045f1aa62ba42428cbf43cffb07b05d6a44ee0545952ab59713880873a71ca23。
- 0.1.0基础回退包SHA256：87f21ead5f0dee049510390810068962e6eff05e2646da1b6d25607466a60be5；无托盘、通知、原生保存和S02恢复。
- S02隔离包SHA256：f200579791a5f9c38237f70e5911c1ae5b47824feb571be5c9fc5b6b0d79b6e3，只在原独立测试目录使用，不进入团队ZIP。

### 程序验证（Codex）

工作目录阶段十一。证据目录参数只给测试/构建工具，不进入团队应用；须落在本仓库artifacts/v5-desktop子目录。不要在同一已冻结前端被夹具服务时重建它。

```powershell
$env:PATH = 'C:\Users\23103\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin;' + $env:PATH
$env:POWERSYSTEM_TEST_RUN_DIRECTORY = [IO.Path]::GetFullPath('../../artifacts/v5-desktop/s03-20261027-r3')
node 'C:/Program Files/nodejs/node_modules/npm/bin/npm-cli.js' run typecheck
node --test tests/test-run-directory.test.cjs
node scripts/build-isolated.cjs
node scripts/s02-fixture.cjs --run-directory $env:POWERSYSTEM_TEST_RUN_DIRECTORY
```

最后一项保持仅本机4175服务。另一终端同环境执行：

```powershell
node tests/s02-runtime.cjs
node tests/s03-compat.cjs
node tests/s02-team-boundary.cjs
```

s03-compat使用无preload的sandboxed Chromium及浏览器/S01方法表/未知协议三种模式，只登录读取合成页面并注销；不冒充S01实装生产验收。集成19项的OS通知/保存对话框/启动项仍为替身，沿S02真实界面证据和本轮必要本人短验分别判断。测试数据/脚本不打包入团队安装程序。

阶段四最终前端构建命令仍从阶段十一入口维护，工作目录先切到阶段四：

```powershell
Push-Location ../04-frontend
node 'C:/Program Files/nodejs/node_modules/npm/bin/npm-cli.js' test
node 'C:/Program Files/nodejs/node_modules/npm/bin/npm-cli.js' run build -- --outDir ../../artifacts/v5-desktop/s03-20261027-r3/frontend
Pop-Location
```

本轮沿用原安装包，不执行打包。如以后确需新候选，须改版本并把输出覆盖参数指向新运行目录，禁止默认写S02：

```powershell
node 'C:/Program Files/nodejs/node_modules/npm/bin/npm-cli.js' run package:win -- --config.directories.output=../../artifacts/v5-desktop/NEW-UNUSED-RUN/package
```

NEW-UNUSED-RUN是占位，不能视为本轮已产出新包。服务器构建/冻结/备份/发布/回退只按[阶段四部署README](../04-frontend/deploy/README.md)及当批目录定位卡执行；本轮已审驱动脚本和固定证据归R3目录，不能重放到下一批。无备份延后或资源缩减例外。

### 本人本机与B机入口

本轮团队ZIP不含A机证据或隔离包。解压后先双击S03-Start-B.cmd，脚本核冻结安装包摘要后开启45分钟只读监测；无需Node或开发命令。窗口保留错误，节点稳定至少8秒，按TEAM-GUIDE完成首装/登录/页面/CSV另存/托盘/退出重登，只做简短验证。出现错误保留原文，不自行删注册表或安装目录。

A机升级与回退：先启动R1/team-delivery/S03-Start-A.cmd、通知关、托盘退出；装R1/rollback的0.1.0并登录看页面、关闭退出；再装同冻结0.2.0、重新登录，核偏好/窗口位置/单托盘后恢复通知开。NSIS模板没有默认版本比较闸门，但只对本轮实际安装路线作结论，不外推任意未来版本。

采证器/监测器的开发调用（本人不需要运行）：

```powershell
./scripts/Collect-S03Evidence.ps1 -RunDirectory '../../artifacts/v5-desktop/s03-20261027-r1/team-delivery/evidence' -Machine A -Node after-upgrade
./scripts/Watch-S03Evidence.ps1 -RunDirectory '../../artifacts/v5-desktop/s03-20261027-r1/team-delivery/evidence' -Machine A
```

真实用户视角执行。只读HKCU登记/合法DisplayIcon、程序和asar摘要、进程/窗口、三布尔偏好、窗口bounds及固定事件码；不读取凭据值或输出浏览器存储。快照不是连续退出证明，须结合watch时序及本人观察。B机证据通过已验证渠道传给所有者/Codex，单独归档machine-B-return，不公开原始文件。

关闭监测窗口或在对应evidence目录创建stop-watch.flag即可停止；不会退出业务客户端。系统不重启、不整体断网。S03证据保留至少到2026-11-27或S03最终验收结束的较晚者；任何清理先核引用与实际归属。
