param([switch]$IncludeBackend)
. (Join-Path $PSScriptRoot 'stage7-common.ps1')
$names = @('agent')
if ($IncludeBackend) { $names += @('api','gateway','fixture','frontend') }
foreach ($name in $names) {
    $file = Join-Path $Stage7Runtime "$name-process.json"
    if (-not (Test-Path -LiteralPath $file)) { continue }
    $state = Get-Content -LiteralPath $file -Raw | ConvertFrom-Json
    $process = Get-Process -Id $state.pid -ErrorAction SilentlyContinue
    if ($process -and [Math]::Abs(($process.StartTime.ToUniversalTime() - ([DateTime]$state.started_at).ToUniversalTime()).TotalSeconds) -lt 2) {
        # Windows venv python.exe is a launcher; stop its owned child too.
        foreach ($child in @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $($process.Id)")) {
            $childProcess=Get-Process -Id $child.ProcessId -ErrorAction SilentlyContinue
            if ($childProcess -and $childProcess.StartTime -ge $process.StartTime) { Stop-Process -Id $childProcess.Id }
        }
        Stop-Process -Id $process.Id
        Wait-Process -Id $process.Id -Timeout 10 -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $file
}
