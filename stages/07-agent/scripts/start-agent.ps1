param([string]$ConfigFile)
. (Join-Path $PSScriptRoot 'stage7-common.ps1')
Push-Location $Stage7Repo
try {
    if ($ConfigFile) { Set-Stage7Environment -ConfigFile $ConfigFile } else { Set-Stage7Environment }
    & $Stage7Python -m agent.cli validate
    if ($LASTEXITCODE -ne 0) { throw 'Invalid configuration.' }
    $process = Start-Stage7Managed -Name agent -Executable $Stage7Python -Arguments @('-m','uvicorn','agent.main:app','--host','127.0.0.1','--port','8092','--no-access-log') -WorkingDirectory $Stage7Repo
    for ($i=0;$i -lt 30;$i++) {
        if ($process.HasExited) { throw 'Agent exited; check private runtime logs.' }
        try { $health=Invoke-RestMethod 'http://127.0.0.1:8092/health' -TimeoutSec 1; if ($health.status -eq 'ok') { Write-Host 'Agent ready on loopback 8092; model capability is verified separately.'; return } } catch {}
        Start-Sleep -Milliseconds 500
    }
    throw 'Agent health check timed out.'
} finally { Pop-Location }
