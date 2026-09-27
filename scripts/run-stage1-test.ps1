param(
    [ValidateRange(1, 100000)][int]$SamplesPerDevice = 720,
    [ValidateRange(0.1, 3600)][double]$IntervalSeconds = 5,
    [switch]$NoFaults,
    [ValidateRange(0.1, 1000)][double]$TimeScale = 24,
    [ValidateRange(0, 100000)][double]$BrokerFaultAtSeconds = 1200,
    [ValidateRange(0, 100000)][double]$GatewayFaultAtSeconds = 2400,
    [ValidateRange(1, 3600)][double]$BrokerDowntimeSeconds = 30
)
. (Join-Path $PSScriptRoot 'stage1-common.ps1')

$runId = 'stage1_' + (Get-Date -Format 'yyyyMMdd_HHmmss') + '_' + ([Guid]::NewGuid().ToString('N').Substring(0, 4))
$runDir = Join-Path $ProjectRoot "artifacts\stage1\$runId"
$database = $runId
New-Item -ItemType Directory -Path $runDir -Force | Out-Null
$run = [ordered]@{
    run_id = $runId
    database = $database
    started_at = [DateTime]::UtcNow.ToString('o')
    ended_at = $null
    samples_per_device = $SamplesPerDevice
    interval_seconds = $IntervalSeconds
    time_scale = $TimeScale
    fault_injection = (-not $NoFaults)
    broker_fault_at_seconds = $BrokerFaultAtSeconds
    gateway_fault_at_seconds = $GatewayFaultAtSeconds
    broker_downtime_seconds = $BrokerDowntimeSeconds
    status = 'running'
    note = ''
}
$runPath = Join-Path $runDir 'run.json'
$run | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $runPath -Encoding utf8
Write-RunEvent -RunDir $runDir -Event 'run_created' -Fields @{ database = $database; samples_per_device = $SamplesPerDevice }
Write-Host "运行编号：$runId"
Write-Host "自动日志目录：$runDir"

$docker = $null
$gateway = $null
$simulator = $null
$loggers = @()
$exitCode = 1
$failure = $null
$brokerStopped = $false
$brokerRestored = $false
$gatewayRestarted = $false
$expectedDuration = $SamplesPerDevice * $IntervalSeconds
$brokerAt = $BrokerFaultAtSeconds
$gatewayAt = $GatewayFaultAtSeconds
$lastMetric = [DateTime]::MinValue
$startTime = [DateTime]::UtcNow

function Start-GatewayProcess {
    $script:gatewaySegment++
    $exe = Join-Path $ProjectRoot 'artifacts\bin\gateway.exe'
    $args = @('-broker', 'tcp://127.0.0.1:1883', '-queue', (Join-Path $runDir 'gateway.sqlite'), '-shutdown-file', (Join-Path $runDir 'gateway.stop'), '-td-database', $database, '-client-id', "gateway-$runId")
    return Start-Process -FilePath $exe -ArgumentList $args -WorkingDirectory $ProjectRoot -PassThru -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runDir "gateway-$gatewaySegment.jsonl") -RedirectStandardError (Join-Path $runDir "gateway-$gatewaySegment.stderr.log")
}

function Record-Metric {
    $queue = Get-QueueStatus -RunDir $runDir
    $tdCount = -1
    try {
        $response = Invoke-TDSql -Sql "SELECT COUNT(*) FROM $database.telemetry" -Password $secret
        $tdCount = [int]$response.data[0][0]
    } catch { }
    $gateway.Refresh()
    $simulator.Refresh()
    $elapsedSeconds = [Math]::Round(([DateTime]::UtcNow - $startTime).TotalSeconds, 1)
    $line = @(
        [DateTime]::UtcNow.ToString('o'),
        $elapsedSeconds,
        $queue.generated,
        $queue.simulator_pending,
        $queue.gateway_pending,
        $tdCount,
        [int](-not $gateway.HasExited),
        [int](-not $simulator.HasExited),
        [int]($brokerStopped -and -not $brokerRestored)
    ) -join ','
    Add-Content -LiteralPath (Join-Path $runDir 'metrics.csv') -Value $line -Encoding utf8
    return [pscustomobject]@{ Generated = $queue.generated; PendingSimulator = $queue.simulator_pending; PendingGateway = $queue.gateway_pending; TDCount = $tdCount; Elapsed = $elapsedSeconds }
}

try {
    $gatewaySegment = 0
    & (Join-Path $PSScriptRoot 'preflight-stage1.ps1')
    $docker = Get-DockerExe
    $secret = Get-LocalSecret
    $env:TDENGINE_ROOT_PASSWORD = $secret
    Initialize-TDDatabase -Database $database -Password $secret
    Write-RunEvent -RunDir $runDir -Event 'database_initialized'
    $versions = [ordered]@{ docker = (& $docker version --format '{{.Client.Version}}/{{.Server.Version}}'); compose = (& $docker compose version --short); go = (& go version); python = (& (Get-PythonExe) --version) }
    $versions | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $runDir 'environment.json') -Encoding utf8

    foreach ($service in @('emqx', 'tdengine')) {
        $stdout = Join-Path $runDir "$service.log"
        $stderr = Join-Path $runDir "$service.stderr.log"
        $container = "powersystem-$service-1"
        $loggers += Start-Process -FilePath $docker -ArgumentList @('logs', '--follow', '--since', '1s', $container) -WorkingDirectory $ProjectRoot -PassThru -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    }
    $gateway = Start-GatewayProcess
    Write-RunEvent -RunDir $runDir -Event 'gateway_started' -Fields @{ pid = $gateway.Id }
    Start-Sleep -Seconds 2
    $python = Get-PythonExe
    $simArgs = @(
        '-u', (Join-Path $ProjectRoot 'simulator\simulator.py'),
        '--samples', "$SamplesPerDevice", '--interval-seconds', "$IntervalSeconds", '--time-scale', "$TimeScale",
        '--run-id', $runId, '--outbox', (Join-Path $runDir 'simulator.sqlite'), '--fault-labels', (Join-Path $runDir 'fault-labels.csv')
    )
    $simulator = Start-Process -FilePath $python -ArgumentList $simArgs -WorkingDirectory $ProjectRoot -PassThru -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runDir 'simulator.jsonl') -RedirectStandardError (Join-Path $runDir 'simulator.stderr.log')
    $startTime = [DateTime]::UtcNow
    'at_utc,elapsed_seconds,generated,simulator_pending,gateway_pending,tdengine_count,gateway_running,simulator_running,broker_down' | Set-Content -LiteralPath (Join-Path $runDir 'metrics.csv') -Encoding utf8
    Write-RunEvent -RunDir $runDir -Event 'simulator_started' -Fields @{ pid = $simulator.Id }
    Write-Host '测试已启动。脚本会自动写日志并执行故障注入；请保持此终端和电脑运行。'

    while ($true) {
        $simulator.Refresh()
        $gateway.Refresh()
        $elapsed = ([DateTime]::UtcNow - $startTime).TotalSeconds
        if (-not $NoFaults -and -not $brokerStopped -and $elapsed -ge $brokerAt) {
            & $docker compose stop emqx | Out-Null
            if ($LASTEXITCODE -ne 0) { throw '停止 EMQX 注入故障失败。' }
            $brokerStopped = $true
            $brokerDownAt = [DateTime]::UtcNow
            Write-RunEvent -RunDir $runDir -Event 'broker_stopped'
            Write-Host "已自动停止 EMQX，$BrokerDowntimeSeconds 秒后恢复。"
        }
        if ($brokerStopped -and -not $brokerRestored -and ([DateTime]::UtcNow - $brokerDownAt).TotalSeconds -ge $BrokerDowntimeSeconds) {
            & $docker compose start emqx | Out-Null
            if ($LASTEXITCODE -ne 0) { throw '恢复 EMQX 失败。' }
            $brokerRestored = $true
            Write-RunEvent -RunDir $runDir -Event 'broker_restored'
        }
        if (-not $NoFaults -and -not $gatewayRestarted -and $elapsed -ge $gatewayAt) {
            Set-Content -LiteralPath (Join-Path $runDir 'gateway.stop') -Value 'restart' -Encoding ascii
            try { Wait-Process -Id $gateway.Id -Timeout 10 -ErrorAction Stop } catch { Stop-Process -Id $gateway.Id -Force -ErrorAction SilentlyContinue }
            Remove-Item -LiteralPath (Join-Path $runDir 'gateway.stop') -Force
            Start-Sleep -Seconds 1
            $gateway = Start-GatewayProcess
            $gatewayRestarted = $true
            Write-RunEvent -RunDir $runDir -Event 'gateway_restarted' -Fields @{ pid = $gateway.Id }
        }
        if (([DateTime]::UtcNow - $lastMetric).TotalSeconds -ge 10) {
            $metric = Record-Metric
            $lastMetric = [DateTime]::UtcNow
            if ([int]$metric.Elapsed % 60 -lt 10) {
                Write-Host ("进度 {0:N0}s：生成 {1}，入库 {2}，模拟器待发 {3}，网关待写 {4}" -f $metric.Elapsed, $metric.Generated, $metric.TDCount, $metric.PendingSimulator, $metric.PendingGateway)
            }
        }
        if ($gateway.HasExited) { throw 'Go 网关意外退出，请检查 gateway.stderr.log。' }
        if ($simulator.HasExited) { break }
        if ($elapsed -gt ($expectedDuration + 180)) { throw '模拟器运行超时。' }
        Start-Sleep -Seconds 2
    }

    $simulator.Refresh()
    if ($simulator.ExitCode -ne 0) { throw "模拟器退出码 $($simulator.ExitCode)，请检查 simulator.stderr.log。" }
    $deadline = (Get-Date).AddSeconds(90)
    do {
        $metric = Record-Metric
        if ($metric.Generated -eq $SamplesPerDevice * 3 -and $metric.PendingSimulator -eq 0 -and $metric.PendingGateway -eq 0 -and $metric.TDCount -eq $SamplesPerDevice * 3) { break }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    if ($metric.Generated -ne $SamplesPerDevice * 3 -or $metric.PendingSimulator -ne 0 -or $metric.PendingGateway -ne 0 -or $metric.TDCount -ne $SamplesPerDevice * 3) {
        throw '最终数量或待补传队列未达标；详情见 metrics.csv 和对账报告。'
    }
    if (-not $NoFaults -and (-not $brokerRestored -or -not $gatewayRestarted)) {
        throw '故障注入尚未全部执行；请使用完整测试时长。'
    }
    $run.status = 'completed'
    $exitCode = 0
    Write-RunEvent -RunDir $runDir -Event 'run_completed'
} catch {
    $failure = $_.Exception.Message
    $run.status = 'failed'
    $run.note = $failure
    Write-RunEvent -RunDir $runDir -Event 'run_failed' -Fields @{ error = $failure }
    Write-Warning $failure
} finally {
    if ($brokerStopped -and -not $brokerRestored -and $docker) {
        try {
            & $docker compose start emqx | Out-Null
            Write-RunEvent -RunDir $runDir -Event 'broker_restored_during_cleanup'
        } catch {
            Write-Warning "清理时未能恢复 EMQX：$($_.Exception.Message)"
        }
    }
    foreach ($proc in @($simulator, $gateway) + $loggers) {
        if ($null -ne $proc) {
            try {
                $proc.Refresh()
                if (-not $proc.HasExited) {
                    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
                    Wait-Process -Id $proc.Id -Timeout 10 -ErrorAction SilentlyContinue
                }
            } catch { }
        }
    }
    $run.ended_at = [DateTime]::UtcNow.ToString('o')
    $run | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $runPath -Encoding utf8
    try {
        & (Join-Path $PSScriptRoot 'collect-stage1-test.ps1') -RunId $runId
        $reconciliation = Get-Content -LiteralPath (Join-Path $runDir 'reconciliation.json') -Raw | ConvertFrom-Json
        if (-not $reconciliation.passed) { $exitCode = 1 }
    } catch {
        $exitCode = 1
        Write-Warning "审查包自动收集失败：$($_.Exception.Message)。请稍后手动运行收集脚本。"
    }
    Write-Host "运行目录：$runDir"
}
exit $exitCode
