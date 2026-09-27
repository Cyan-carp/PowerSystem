param(
    [ValidateRange(1, 1000)][int]$SamplesPerDevice = 60,
    [ValidateRange(0.1, 60)][double]$IntervalSeconds = 1,
    [switch]$NoFaults
)
. (Join-Path $PSScriptRoot '..\..\01-data-chain\scripts\stage1-common.ps1')

$runId = 'stage1_iso_' + (Get-Date -Format 'yyyyMMdd_HHmmss') + '_' + ([Guid]::NewGuid().ToString('N').Substring(0, 4))
$runDir = Join-Path $ProjectRoot "artifacts\stage1\$runId"
$container = 'powersystem-stage1-isolated-regression'
$port = 6042
$gateway = $null
$simulator = $null
$brokerStopped = $false
$brokerRestored = $false
$gatewayRestarted = $false
$containerCreated = $false
$result = [ordered]@{ run_id = $runId; mode = 'isolated_tdengine'; database = $runId; expected = 3 * $SamplesPerDevice; status = 'running'; started_at = [DateTime]::UtcNow.ToString('o'); ended_at = $null; devices = @(); queue = $null; faults = @(); note = '' }
New-Item -ItemType Directory -Path $runDir -Force | Out-Null
$secretFile = Join-Path $runDir 'tdengine.env'

function Invoke-IsolatedSql {
    param([string]$Sql)
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("root:$secret"))
    $response = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$port/rest/sql" -Headers @{ Authorization = "Basic $encoded" } -ContentType 'text/plain' -Body $Sql -TimeoutSec 15
    if ($response.code -ne 0) { throw "Isolated TDengine SQL failed: $($response.code) $($response.desc)" }
    return $response
}

function Start-IsolatedGateway {
    $script:gatewaySegment++
    return Start-Process -FilePath (Join-Path $ProjectRoot 'artifacts\bin\gateway.exe') -ArgumentList @(
        '-broker', 'tcp://127.0.0.1:1883', '-queue', (Join-Path $runDir 'gateway.sqlite'),
        '-shutdown-file', (Join-Path $runDir 'gateway.stop'), '-td-url', "http://127.0.0.1:$port",
        '-td-database', $runId, '-client-id', "gateway-$runId"
    ) -WorkingDirectory $ProjectRoot -PassThru -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runDir "gateway-$gatewaySegment.jsonl") -RedirectStandardError (Join-Path $runDir "gateway-$gatewaySegment.stderr.log")
}

try {
    $docker = Get-DockerExe
    $python = Get-PythonExe
    $secret = Get-LocalSecret
    $env:TDENGINE_ROOT_PASSWORD = $secret
    if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot 'artifacts\bin\gateway.exe'))) { throw 'Stage1 gateway binary missing. Run stage1 preflight first.' }
    $existing = & $docker ps -a --filter "name=^/$container`$" --format '{{.Names}}'
    if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect Docker containers.' }
    if ($existing) { throw "Container name already in use: $container" }
    [System.IO.File]::WriteAllText($secretFile, "TAOS_ROOT_PASSWORD=$secret`n", [System.Text.UTF8Encoding]::new($false))
    $createdId = & $docker run --rm -d --name $container --env-file $secretFile -p "127.0.0.1:$($port):6041" tdengine/tsdb:3.4.2.8
    if ($LASTEXITCODE -ne 0 -or -not $createdId) { throw 'Failed to start isolated TDengine container.' }
    $containerCreated = $true
    Write-RunEvent -RunDir $runDir -Event 'isolated_tdengine_started' -Fields @{ port = $port }
    $deadline = (Get-Date).AddSeconds(90)
    do {
        try { $null = Invoke-IsolatedSql 'SELECT SERVER_VERSION()'; break } catch { Start-Sleep -Seconds 2 }
    } while ((Get-Date) -lt $deadline)
    if ((Get-Date) -ge $deadline) { throw 'Isolated TDengine did not become ready.' }
    $template = Get-Content -LiteralPath (Join-Path $StageRoot 'deploy\tdengine\init.sql') -Raw
    $statements = ($template -replace '(?m)^--.*$', '' -replace '\{\{DATABASE\}\}', $runId) -split ';'
    foreach ($sql in $statements) { if ($sql.Trim()) { $null = Invoke-IsolatedSql $sql.Trim() } }
    Write-RunEvent -RunDir $runDir -Event 'database_initialized'
    $gatewaySegment = 0
    $gateway = Start-IsolatedGateway
    Start-Sleep -Seconds 2
    $simulator = Start-Process -FilePath $python -ArgumentList @(
        '-u', (Join-Path $StageRoot 'simulator\simulator.py'), '--samples', "$SamplesPerDevice",
        '--interval-seconds', "$IntervalSeconds", '--time-scale', '1', '--run-id', $runId,
        '--outbox', (Join-Path $runDir 'simulator.sqlite'), '--fault-labels', (Join-Path $runDir 'fault-labels.csv')
    ) -WorkingDirectory $ProjectRoot -PassThru -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runDir 'simulator.jsonl') -RedirectStandardError (Join-Path $runDir 'simulator.stderr.log')
    $started = [DateTime]::UtcNow
    Write-RunEvent -RunDir $runDir -Event 'simulator_started'
    $brokerAt = [Math]::Max(5, $SamplesPerDevice * $IntervalSeconds * 0.25)
    $gatewayAt = [Math]::Max(10, $SamplesPerDevice * $IntervalSeconds * 0.65)
    while ($true) {
        $elapsed = ([DateTime]::UtcNow - $started).TotalSeconds
        if (-not $NoFaults -and -not $brokerStopped -and $elapsed -ge $brokerAt) {
            & $docker @ComposeArgs stop emqx | Out-Null
            if ($LASTEXITCODE -ne 0) { throw 'Failed to stop EMQX.' }
            $brokerStopped = $true
            $brokerDownAt = [DateTime]::UtcNow
            Write-RunEvent -RunDir $runDir -Event 'broker_stopped'
        }
        if ($brokerStopped -and -not $brokerRestored -and ([DateTime]::UtcNow - $brokerDownAt).TotalSeconds -ge 8) {
            & $docker @ComposeArgs start emqx | Out-Null
            if ($LASTEXITCODE -ne 0) { throw 'Failed to restart EMQX.' }
            $brokerRestored = $true
            Write-RunEvent -RunDir $runDir -Event 'broker_restored'
        }
        if (-not $NoFaults -and -not $gatewayRestarted -and $elapsed -ge $gatewayAt) {
            Set-Content -LiteralPath (Join-Path $runDir 'gateway.stop') -Value 'restart' -Encoding ascii
            try { Wait-Process -Id $gateway.Id -Timeout 10 -ErrorAction Stop } catch { Stop-Process -Id $gateway.Id -Force -ErrorAction SilentlyContinue }
            Remove-Item -LiteralPath (Join-Path $runDir 'gateway.stop') -Force
            Start-Sleep -Seconds 1
            $gateway = Start-IsolatedGateway
            $gatewayRestarted = $true
            Write-RunEvent -RunDir $runDir -Event 'gateway_restarted'
        }
        $gateway.Refresh(); $simulator.Refresh()
        if ($gateway.HasExited) { throw 'Stage1 gateway exited unexpectedly.' }
        if ($simulator.HasExited) { break }
        if ($elapsed -gt ($SamplesPerDevice * $IntervalSeconds + 180)) { throw 'Stage1 simulator timed out.' }
        Start-Sleep -Seconds 2
    }
    if (-not (Select-String -LiteralPath (Join-Path $runDir 'simulator.jsonl') -Pattern '"event": "simulator_stopped"' -Quiet)) { throw 'Stage1 simulator did not finish cleanly.' }
    $deadline = (Get-Date).AddSeconds(90)
    do {
        $queue = Get-QueueStatus -RunDir $runDir
        $td = Invoke-IsolatedSql "SELECT COUNT(*) FROM $runId.telemetry"
        $count = [int]$td.data[0][0]
        if ($queue.generated -eq $result.expected -and $queue.simulator_pending -eq 0 -and $queue.gateway_pending -eq 0 -and $count -eq $result.expected) { break }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    $result.queue = $queue
    if ($count -ne $result.expected -or $queue.generated -ne $result.expected -or $queue.simulator_pending -ne 0 -or $queue.gateway_pending -ne 0) { throw "Count/queue mismatch: TD=$count expected=$($result.expected), generated=$($queue.generated), pending=$($queue.simulator_pending)/$($queue.gateway_pending)" }
    $rows = (Invoke-IsolatedSql "SELECT device_id, COUNT(*), MIN(seq), MAX(seq) FROM $runId.telemetry PARTITION BY device_id").data
    foreach ($row in $rows) {
        $result.devices += [ordered]@{ device_id = [string]$row[0]; count = [int]$row[1]; min_seq = [int]$row[2]; max_seq = [int]$row[3] }
        if ([int]$row[1] -ne $SamplesPerDevice -or [int]$row[2] -ne 0 -or [int]$row[3] -ne $SamplesPerDevice - 1) { throw 'Per-device sequence mismatch.' }
    }
    if ($result.devices.Count -ne 3) { throw 'Expected three stage1 devices.' }
    if (-not $NoFaults -and (-not $brokerRestored -or -not $gatewayRestarted)) { throw 'Fault recovery events incomplete.' }
    $result.status = 'passed'
    Write-RunEvent -RunDir $runDir -Event 'regression_passed'
} catch {
    $result.status = 'failed'
    $result.note = $_.Exception.Message
    Write-RunEvent -RunDir $runDir -Event 'regression_failed' -Fields @{ error = $result.note }
    Write-Warning $result.note
} finally {
    if ($brokerStopped -and -not $brokerRestored) {
        try { & $docker @ComposeArgs start emqx | Out-Null; Write-RunEvent -RunDir $runDir -Event 'broker_restored_during_cleanup' } catch { Write-Warning "Failed to restore EMQX: $($_.Exception.Message)" }
    }
    foreach ($proc in @($simulator, $gateway)) {
        if ($null -ne $proc) { try { $proc.Refresh(); if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue } } catch { } }
    }
    if ($containerCreated) { try { & $docker stop $container | Out-Null } catch { Write-Warning "Failed to stop isolated TDengine: $($_.Exception.Message)" } }
    if (Test-Path -LiteralPath $secretFile) { Remove-Item -LiteralPath $secretFile -Force }
    $result.ended_at = [DateTime]::UtcNow.ToString('o')
    $result.faults = @(Get-Content -LiteralPath (Join-Path $runDir 'events.jsonl') | ForEach-Object { ($_ | ConvertFrom-Json).event } | Where-Object { $_ -match '^(broker|gateway)_' })
    $result | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $runDir 'isolated-reconciliation.json') -Encoding utf8
    Write-Host "Stage1 isolated regression: $($result.status); run=$runId; evidence=$runDir"
}
if ($result.status -ne 'passed') { exit 1 }
