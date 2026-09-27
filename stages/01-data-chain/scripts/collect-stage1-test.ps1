param([Parameter(Mandatory)][string]$RunId)
. (Join-Path $PSScriptRoot 'stage1-common.ps1')

if ($RunId -notmatch '^stage1_[0-9]{8}_[0-9]{6}_[0-9a-f]{4}$') { throw '运行编号格式不正确。' }
$runDir = Join-Path $ProjectRoot "artifacts\stage1\$RunId"
if (-not (Test-Path -LiteralPath $runDir)) { throw "运行目录不存在：$runDir" }
$runPath = Join-Path $runDir 'run.json'
$run = Get-Content -LiteralPath $runPath -Raw | ConvertFrom-Json
if ($run.status -eq 'running') {
    $run.status = 'interrupted'
    $run.note = '收集脚本发现原启动脚本未写入结束状态；可能发生强制结束或断电。'
    $run | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $runPath -Encoding utf8
}
$secret = Get-LocalSecret
$env:TDENGINE_ROOT_PASSWORD = $secret
$python = Get-PythonExe
$output = & $python (Join-Path $StageRoot 'scripts\reconcile-stage1.py') $runDir $run.database $run.samples_per_device
if ($LASTEXITCODE -ne 0) { throw '对账脚本执行失败，请把运行目录路径交给我。' }
$result = Get-Content -LiteralPath (Join-Path $runDir 'reconciliation.json') -Raw | ConvertFrom-Json
if ($run.status -eq 'completed' -and -not $result.passed) {
    $run.status = 'failed'
    $run.note = '运行计数达到目标，但逐条数据对账未通过。'
    $run | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $runPath -Encoding utf8
}
$summary = @(
    '# 阶段一测试摘要',
    '',
    "- 运行编号：$RunId",
    "- 运行状态：$($run.status)",
    "- 时序数据库：$($run.database)",
    "- 预期条数：$($result.expected_total)",
    "- 模拟器已生成：$($result.generated_total)",
    "- TDengine 已入库：$($result.tdengine_total)",
    "- 模拟器待发：$($result.simulator_pending)",
    "- 网关待写：$($result.gateway_pending)",
    "- 数据对账通过：$($result.passed)",
    "- 数据库查询错误：$($result.td_error)",
    '',
    '请把本目录的 review-bundle.zip 绝对路径发给我审查。'
)
Set-Content -LiteralPath (Join-Path $runDir 'summary.md') -Value $summary -Encoding utf8
$bundle = Join-Path $runDir 'review-bundle.zip'
if (Test-Path -LiteralPath $bundle) { Remove-Item -LiteralPath $bundle -Force }
$paths = @(Get-ChildItem -LiteralPath $runDir -Force | Where-Object { $_.Name -ne 'review-bundle.zip' } | ForEach-Object FullName)
Compress-Archive -LiteralPath $paths -DestinationPath $bundle -Force
Write-Host "审查包：$bundle"
Write-Host "状态：$($run.status)；数据对账：$($result.passed)；已入库：$($result.tdengine_total)/$($result.expected_total)"
