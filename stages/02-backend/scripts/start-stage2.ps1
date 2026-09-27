param([switch]$SkipPreflight)
. (Join-Path $PSScriptRoot 'stage2-common.ps1')
if (-not $SkipPreflight) { & (Join-Path $PSScriptRoot 'preflight-stage2.ps1') }
Set-Stage2Environment
$runDir = Stage2-RunDirectory
New-Item -ItemType Directory -Path $runDir -Force | Out-Null
$pidPath = Join-Path $runDir 'processes.json'
if (Test-Path -LiteralPath $pidPath) {
    $old = Get-Content -LiteralPath $pidPath -Raw | ConvertFrom-Json
    $alive = @(@($old.api_pid, $old.gateway_pid) | Where-Object { $_ -and (Get-Process -Id $_ -ErrorAction SilentlyContinue) })
    if ($alive.Count -gt 0) { throw '阶段二服务已在运行；先执行 stop-stage2.ps1。' }
}
$apiExe = Join-Path $ProjectRoot 'artifacts\bin\stage2-api.exe'
$gatewayExe = Join-Path $ProjectRoot 'artifacts\bin\stage2-gateway.exe'
$api = Start-Process -FilePath $apiExe -WorkingDirectory $Stage2Root -PassThru -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runDir 'api.jsonl') -RedirectStandardError (Join-Path $runDir 'api.stderr.log')
$gateway = Start-Process -FilePath $gatewayExe -WorkingDirectory $Stage2Root -PassThru -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runDir 'gateway.jsonl') -RedirectStandardError (Join-Path $runDir 'gateway.stderr.log')
@{api_pid=$api.Id;gateway_pid=$gateway.Id;started_at=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath $pidPath -Encoding utf8
Start-Sleep -Seconds 3
if ($api.HasExited -or $gateway.HasExited) { throw "阶段二服务提前退出；检查 $runDir 下的 stderr 日志。" }
$ping = Invoke-RestMethod -Uri 'http://127.0.0.1:8080/api/v1/ping' -TimeoutSec 5
if ($ping.code -ne 0) { throw 'API 健康检查失败。' }
Write-Host "阶段二服务就绪；API PID=$($api.Id)，网关 PID=$($gateway.Id)，日志：$runDir"
