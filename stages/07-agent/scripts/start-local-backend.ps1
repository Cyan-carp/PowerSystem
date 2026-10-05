param([string]$ConfigFile)
. (Join-Path $PSScriptRoot 'stage7-common.ps1')
Push-Location $Stage7Repo
try {
    if ($ConfigFile) { Set-Stage7Environment -ConfigFile $ConfigFile } else { Set-Stage7Environment }
    $listener = Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue
    if ($listener) { throw 'Port 8080 already owned by another process. Stop its owning stage first.' }
    $env:GOCACHE=Join-Path $Stage7Repo 'artifacts/go-cache'
    $env:GOMODCACHE=Join-Path $Stage7Repo 'artifacts/go-mod'
    $env:GOTELEMETRY='off'
    $env:STAGE2_DATA_DIR=Join-Path $Stage7Repo 'artifacts/stage2/runtime'
    $bin=Join-Path $Stage7Repo 'artifacts/stage7-智能体/bin'
    New-Item -ItemType Directory -Path $bin -Force | Out-Null
    Push-Location (Join-Path $Stage7Repo 'stages/02-backend')
    try {
        foreach ($name in @('api','gateway')) {
            $exe=Join-Path $bin "$name.exe"
            & go build -o $exe "./cmd/$name"
            if ($LASTEXITCODE -ne 0) { throw "$name build failed" }
            $null=Start-Stage7Managed -Name $name -Executable $exe -Arguments @() -WorkingDirectory (Get-Location).Path
        }
    } finally { Pop-Location }
    for ($i=0;$i -lt 30;$i++) {
        try { $ping=Invoke-RestMethod 'http://127.0.0.1:8080/api/v1/ping' -TimeoutSec 1; if ($ping.code -eq 0) { Write-Host 'Local Go API and gateway ready.'; return } } catch {}
        Start-Sleep -Milliseconds 500
    }
    throw 'Backend health check timed out; inspect private stage7 logs.'
} finally { Pop-Location }
