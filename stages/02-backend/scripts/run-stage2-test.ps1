param(
    [ValidateRange(1,100000)][int]$SamplesPerDevice = 1440,
    [ValidateRange(0.1,3600)][double]$IntervalSeconds = 5,
    [switch]$NoFaults,
    [int]$BrokerFaultAtSeconds = 1200,
    [int]$GatewayFaultAtSeconds = 2400,
    [int]$APIFaultAtSeconds = 3600,
    [int]$BrokerDowntimeSeconds = 30
)
. (Join-Path $PSScriptRoot 'stage2-common.ps1')
& (Join-Path $PSScriptRoot 'start-stage2.ps1')
Set-Stage2Environment
$dockerExe = Get-DockerExe
$pythonExe = Get-PythonExe
$runId = 'stage2_' + (Get-Date -Format 'yyyyMMdd_HHmmss') + '_' + ([Guid]::NewGuid().ToString('N').Substring(0,4))
$runDir = Join-Path $ProjectRoot "artifacts\stage2\$runId"
New-Item -ItemType Directory -Path $runDir -Force | Out-Null
$run = [ordered]@{run_id=$runId;started_at=[DateTime]::UtcNow.ToString('o');samples_per_device=$SamplesPerDevice;interval_seconds=$IntervalSeconds;database='powersystem_stage2';status='running';fault_injection=(-not $NoFaults)}
$run | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runDir 'run.json') -Encoding utf8
function Event([string]$name) { @{at=[DateTime]::UtcNow.ToString('o');event=$name} | ConvertTo-Json -Compress | Add-Content -LiteralPath (Join-Path $runDir 'events.jsonl') -Encoding utf8 }
function Restart-Stage2Process([string]$name) {
    $pidPath = Join-Path (Stage2-RunDirectory) 'processes.json'
    $state = Get-Content -LiteralPath $pidPath -Raw | ConvertFrom-Json
    $oldId = if ($name -eq 'api') { $state.api_pid } else { $state.gateway_pid }
    Stop-Process -Id $oldId -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    $exe = Join-Path $ProjectRoot "artifacts\bin\stage2-$name.exe"
    $new = Start-Process -FilePath $exe -WorkingDirectory $Stage2Root -PassThru -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runDir "$name-restart.jsonl") -RedirectStandardError (Join-Path $runDir "$name-restart.stderr.log")
    if ($name -eq 'api') { $state.api_pid = $new.Id } else { $state.gateway_pid = $new.Id }
    $state | ConvertTo-Json | Set-Content -LiteralPath $pidPath -Encoding utf8
    Event "$name`_restarted"
}
$simulator = $null
$failed = $false
try {
    $args = @('-u',(Join-Path $ProjectRoot 'stages\01-data-chain\simulator\simulator.py'),'--samples',"$SamplesPerDevice",'--interval-seconds',"$IntervalSeconds",'--time-scale','1','--run-id',$runId,'--outbox',(Join-Path $runDir 'simulator.sqlite'),'--fault-labels',(Join-Path $runDir 'fault-labels.csv'))
    & $pythonExe (Join-Path $PSScriptRoot 'smoke-stage2.py') --output (Join-Path $runDir 'cases.csv')
    if ($LASTEXITCODE -ne 0) { throw '阶段二 API / MQTT / WebSocket 用例失败。' }
    $simulator = Start-Process -FilePath $pythonExe -ArgumentList $args -WorkingDirectory $ProjectRoot -PassThru -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runDir 'simulator.jsonl') -RedirectStandardError (Join-Path $runDir 'simulator.stderr.log')
    Event 'simulator_started'
    $started = [DateTime]::UtcNow
    $brokerFaulted = $false; $gatewayFaulted = $false; $apiFaulted = $false
    'at_utc,elapsed_seconds,simulator_running,api_running,gateway_running' | Set-Content -LiteralPath (Join-Path $runDir 'metrics.csv') -Encoding utf8
    while (-not $simulator.HasExited) {
        Start-Sleep -Seconds 5
        $simulator.Refresh()
        $elapsed = ([DateTime]::UtcNow - $started).TotalSeconds
        if (-not $NoFaults -and -not $brokerFaulted -and $elapsed -ge $BrokerFaultAtSeconds) {
            Event 'broker_stopped'; & $dockerExe @Stage2Compose stop emqx | Out-Null
            Start-Sleep -Seconds $BrokerDowntimeSeconds
            & $dockerExe @Stage2Compose start emqx | Out-Null; Event 'broker_restored'; $brokerFaulted = $true
        }
        if (-not $NoFaults -and -not $gatewayFaulted -and $elapsed -ge $GatewayFaultAtSeconds) { Restart-Stage2Process 'gateway'; $gatewayFaulted = $true }
        if (-not $NoFaults -and -not $apiFaulted -and $elapsed -ge $APIFaultAtSeconds) { Restart-Stage2Process 'api'; $apiFaulted = $true }
        $state = Get-Content -LiteralPath (Join-Path (Stage2-RunDirectory) 'processes.json') -Raw | ConvertFrom-Json
        $apiAlive = [int][bool](Get-Process -Id $state.api_pid -ErrorAction SilentlyContinue)
        $gatewayAlive = [int][bool](Get-Process -Id $state.gateway_pid -ErrorAction SilentlyContinue)
        "$([DateTime]::UtcNow.ToString('o')),$([Math]::Round($elapsed,1)),$([int](-not $simulator.HasExited)),$apiAlive,$gatewayAlive" | Add-Content -LiteralPath (Join-Path $runDir 'metrics.csv') -Encoding utf8
        if (-not $apiAlive -or -not $gatewayAlive) { throw 'API 或网关意外退出。' }
    }
    $simulator.WaitForExit()
    $simulator.Refresh()
    Event 'simulator_finished'
    Start-Sleep -Seconds 5
    Copy-Item -LiteralPath (Join-Path (Stage2-RunDirectory) 'api.jsonl') -Destination (Join-Path $runDir 'api.jsonl') -ErrorAction SilentlyContinue
    Copy-Item -LiteralPath (Join-Path (Stage2-RunDirectory) 'api.stderr.log') -Destination (Join-Path $runDir 'api.stderr.log') -ErrorAction SilentlyContinue
    Copy-Item -LiteralPath (Join-Path (Stage2-RunDirectory) 'gateway.jsonl') -Destination (Join-Path $runDir 'gateway.jsonl') -ErrorAction SilentlyContinue
    Copy-Item -LiteralPath (Join-Path (Stage2-RunDirectory) 'gateway.stderr.log') -Destination (Join-Path $runDir 'gateway.stderr.log') -ErrorAction SilentlyContinue
    $run.status = 'completed'
    $run.ended_at = [DateTime]::UtcNow.ToString('o')
    $run | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runDir 'run.json') -Encoding utf8
    Event 'run_completed'
    $reconciled = $false
    for ($attempt = 0; $attempt -lt 24; $attempt++) {
        & $pythonExe (Join-Path $PSScriptRoot 'reconcile-stage2.py') $runDir --docker $dockerExe --gateway-queue (Join-Path (Stage2-RunDirectory) 'gateway.sqlite')
        if ($LASTEXITCODE -eq 0) { $reconciled = $true; break }
        Start-Sleep -Seconds 5
    }
    if (-not $reconciled) { throw '阶段二在两分钟排空窗口后仍未对账通过。' }
} catch {
    $failed = $true
    $run.status = 'failed'
    $run['error'] = $_.Exception.Message
    Event 'run_failed'
    Write-Error $_
} finally {
    $run.ended_at = [DateTime]::UtcNow.ToString('o')
    $run | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runDir 'run.json') -Encoding utf8
    if ($simulator -and -not $simulator.HasExited) { Stop-Process -Id $simulator.Id -ErrorAction SilentlyContinue }
    Write-Host "阶段二运行目录：$runDir"
}
if ($failed) { exit 1 }
