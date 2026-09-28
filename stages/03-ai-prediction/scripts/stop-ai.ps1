$ErrorActionPreference = 'Stop'
$stageRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$repoRoot = (Resolve-Path (Join-Path $stageRoot '..\..')).Path
$pidFile = Join-Path $repoRoot 'artifacts\stage3\runtime\ai-process.json'
if (-not (Test-Path $pidFile)) { Write-Host '没有运行记录'; return }
$saved = Get-Content -Raw $pidFile | ConvertFrom-Json
$process = Get-Process -Id $saved.pid -ErrorAction SilentlyContinue
$started = ([DateTime]$saved.started_at).ToUniversalTime()
if ($process -and [math]::Abs(($process.StartTime.ToUniversalTime() - $started).TotalSeconds) -lt 60) {
    Stop-Process -Id $saved.pid -ErrorAction Stop
    Write-Host "已停止 AI 服务 PID=$($saved.pid)"
}
Remove-Item -LiteralPath $pidFile -Force
