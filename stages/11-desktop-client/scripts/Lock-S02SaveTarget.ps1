$ErrorActionPreference = 'Stop'
$runPath = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../../artifacts/v5-desktop/s02-20261026-r1'))
$target = Join-Path $runPath 'S02-locked-target.csv'
$stopFile = Join-Path $runPath 'stop-save-target-lock.flag'
if (Test-Path -LiteralPath $target) { throw 'Fixture target already exists. Preserve it and use the existing lock evidence; do not overwrite it.' }
if (Test-Path -LiteralPath $stopFile) { throw 'Previous lock stop flag exists. Inspect before starting another run.' }
[IO.File]::WriteAllText($target, "fixture,expected`r`nS02-original-target,preserve`r`n", [Text.UTF8Encoding]::new($false))
# Permit ordinary read/write access (including the native save dialog checks),
# but withhold delete sharing so the final atomic replacement fails on Windows.
$stream = [IO.File]::Open($target, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
$started = [DateTime]::UtcNow
try {
  $manifest = [ordered]@{
    capturedAt=$started.ToString('o'); processId=$PID; target=$target;
    bytes=(Get-Item -LiteralPath $target).Length;
    sha256=(Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash;
    share='ReadWrite; no Delete'; scope='S02-owned fixture target only; no ACL, disk quota or system network changes'
  }
  $manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runPath 'save-target-lock-manifest.json') -Encoding utf8
  Write-Output 'S02 save target lock ready (read/write permitted; atomic replace blocked).'
  while (-not (Test-Path -LiteralPath $stopFile) -and [DateTime]::UtcNow -lt $started.AddMinutes(30)) { Start-Sleep -Seconds 1 }
} finally {
  $stream.Dispose()
  [ordered]@{releasedAt=[DateTime]::UtcNow.ToString('o');target=$target} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runPath 'save-target-lock-released.json') -Encoding utf8
}
