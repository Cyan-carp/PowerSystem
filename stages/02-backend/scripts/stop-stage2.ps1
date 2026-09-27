. (Join-Path $PSScriptRoot 'stage2-common.ps1')
$pidPath = Join-Path (Stage2-RunDirectory) 'processes.json'
if (-not (Test-Path -LiteralPath $pidPath)) { Write-Host '未找到阶段二进程记录。'; return }
$running = Get-Content -LiteralPath $pidPath -Raw | ConvertFrom-Json
foreach ($processId in @($running.api_pid,$running.gateway_pid)) {
    if ($processId) { Stop-Process -Id $processId -ErrorAction SilentlyContinue }
}
Remove-Item -LiteralPath $pidPath -Force
Write-Host '阶段二 API 与网关已停止；数据库容器和数据卷保留。'
