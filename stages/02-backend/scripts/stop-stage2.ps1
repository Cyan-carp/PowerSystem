. (Join-Path $PSScriptRoot 'stage2-common.ps1')
$pidPath = Join-Path (Stage2-RunDirectory) 'processes.json'
if (-not (Test-Path -LiteralPath $pidPath)) { Write-Host '未找到阶段二进程记录。'; return }
$running = Get-Content -LiteralPath $pidPath -Raw | ConvertFrom-Json
$started = ([DateTime]$running.started_at).ToUniversalTime()
foreach ($entry in @(@{ id=$running.api_pid; name='stage2-api.exe' },
                    @{ id=$running.gateway_pid; name='stage2-gateway.exe' })) {
    if (-not $entry.id) { continue }
    $process = Get-Process -Id $entry.id -ErrorAction SilentlyContinue
    if (-not $process) { continue }
    if ([IO.Path]::GetFileName($process.Path) -ne $entry.name -or
        [math]::Abs(($process.StartTime.ToUniversalTime() - $started).TotalSeconds) -ge 60) {
        throw "PID $($entry.id) 与阶段二进程记录不匹配，未停止该进程。"
    }
    $process.Kill()
    if (-not $process.WaitForExit(5000)) { throw "PID $($entry.id) 停止超时。" }
}
Remove-Item -LiteralPath $pidPath -Force
Write-Host '阶段二 API 与网关已停止；数据库容器和数据卷保留。'
