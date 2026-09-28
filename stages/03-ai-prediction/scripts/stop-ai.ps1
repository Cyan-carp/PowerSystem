$ErrorActionPreference = 'Stop'
$stageRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$repoRoot = (Resolve-Path (Join-Path $stageRoot '..\..')).Path
$pidFile = Join-Path $repoRoot 'artifacts\stage3\runtime\ai-process.json'
if (-not (Test-Path $pidFile)) { Write-Host '没有运行记录'; return }
$saved = Get-Content -Raw $pidFile | ConvertFrom-Json
$process = Get-CimInstance Win32_Process -Filter "ProcessId=$($saved.pid)" -ErrorAction SilentlyContinue
if ($process -and $process.CommandLine -like '*prediction.app:app*') {
    Stop-Process -Id $saved.pid -ErrorAction Stop
    Write-Host "已停止 AI 服务 PID=$($saved.pid)"
}
Remove-Item -LiteralPath $pidFile -Force
