param([Parameter(Mandatory=$true)][string]$RunDirectory)
$ErrorActionPreference = 'Stop'
$runPath = [IO.Path]::GetFullPath($RunDirectory)
$allowedRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../../artifacts/v5-desktop')) + [IO.Path]::DirectorySeparatorChar
if (-not $runPath.StartsWith($allowedRoot,[StringComparison]::OrdinalIgnoreCase)) { throw 'Unexpected run directory' }
$started = [DateTime]::UtcNow
$last = ''
$pending = ''
$stable = $started
$closedAt = $null
$events = Join-Path $runPath 't19-client-exit-monitor-events.jsonl'
if (Test-Path -LiteralPath $events) { throw 'Preserve earlier monitor evidence before starting another monitor.' }
[ordered]@{startedAt=$started.ToString('o');processId=$PID;scope='Only PowerSystem Desktop and PowerSystem S02 Test process presence; no window manipulation or server writes';expiresAt=$started.AddMinutes(30).ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runPath 't19-client-exit-monitor-manifest.json') -Encoding utf8
Write-Output 'S02 exact desktop exit-window monitor ready.'
while ([DateTime]::UtcNow -lt $started.AddMinutes(30)) {
  $processes = @(Get-Process -Name 'PowerSystem S02 Test','PowerSystem Desktop' -ErrorAction SilentlyContinue)
  $state = if ($processes.Count -eq 0) {'closed'} else {'running'}
  if ($state -ne $pending) { $pending=$state; $stable=[DateTime]::UtcNow }
  if ($state -ne $last -and ([DateTime]::UtcNow-$stable).TotalSeconds -ge 2) {
    $last=$state
    $at=[DateTime]::UtcNow.ToString('o')
    [ordered]@{at=$at;state=$state;processCount=$processes.Count;processIds=@($processes | Select-Object -ExpandProperty Id)} | ConvertTo-Json -Compress | Add-Content -LiteralPath $events -Encoding utf8
    & (Join-Path $PSScriptRoot 'Collect-S02Evidence.ps1') -Phase $state -Isolated -RunDirectory $runPath
    if ($state -eq 'closed') { $closedAt=$at }
    elseif ($closedAt) { break }
  }
  Start-Sleep -Seconds 1
}
[ordered]@{endedAt=[DateTime]::UtcNow.ToString('o');closedAt=$closedAt;lastState=$last} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runPath 't19-client-exit-monitor-ended.json') -Encoding utf8
