$ErrorActionPreference = 'Stop'
$runPath = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../../artifacts/v5-desktop/s02-20261026-r1'))
$stopFile = Join-Path $runPath 'stop-owner-evidence-watch.flag'
$start = [DateTime]::UtcNow
$last = ''
$pending = ''
$stableSince = $start
$captures = 0
$events = Join-Path $runPath 'owner-batch-watcher-events.jsonl'
[ordered]@{startedAt=$start.ToString('o');processId=$PID;scope='Exact S02 isolated test app only; read-only real-user snapshots';expiresAt=$start.AddMinutes(30).ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runPath 'owner-batch-watcher-manifest.json') -Encoding utf8
Write-Output 'S02 owner batch evidence watcher ready.'
while ([DateTime]::UtcNow -lt $start.AddMinutes(30) -and -not (Test-Path -LiteralPath $stopFile)) {
  $processes = @(Get-Process -Name 'PowerSystem S02 Test' -ErrorAction SilentlyContinue)
  $visible = @($processes | Where-Object {$_.MainWindowHandle -ne 0}).Count
  $registered = @(Get-ChildItem -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall' -ErrorAction SilentlyContinue | Get-ItemProperty | Where-Object {$_.DisplayName -eq '源网智联 S02 隔离验收'}).Count -gt 0
  $phase = if (-not $registered -and $processes.Count -eq 0) {'uninstalled'} elseif ($processes.Count -eq 0) {'closed'} else {'running'}
  $signature = "$phase/$visible/$registered"
  if ($signature -ne $pending) { $pending=$signature; $stableSince=[DateTime]::UtcNow }
  if ($pending -ne $last -and ([DateTime]::UtcNow-$stableSince).TotalSeconds -ge 2) {
    $last = $pending
    [ordered]@{at=[DateTime]::UtcNow.ToString('o');phase=$phase;processCount=$processes.Count;visibleWindows=$visible;registered=$registered} | ConvertTo-Json -Compress | Add-Content -LiteralPath $events -Encoding utf8
    if ($captures -lt 12) {
      & (Join-Path $PSScriptRoot 'Collect-S02Evidence.ps1') -Phase $phase -Isolated
      $captures++
    }
    if ($phase -eq 'uninstalled') { break }
  }
  Start-Sleep -Seconds 1
}
[ordered]@{endedAt=[DateTime]::UtcNow.ToString('o');captures=$captures;lastState=$last} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runPath 'owner-batch-watcher-ended.json') -Encoding utf8
