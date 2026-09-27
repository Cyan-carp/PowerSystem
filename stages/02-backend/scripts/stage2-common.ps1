Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Stage2Root = Split-Path -Parent $PSScriptRoot
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $Stage2Root)
. (Join-Path $ProjectRoot 'stages\01-data-chain\scripts\stage1-common.ps1')
$Stage2Root = Split-Path -Parent $PSScriptRoot
$Stage2Compose = @('compose','--project-name','powersystem','--env-file',(Join-Path $ProjectRoot '.env'),'--file',(Join-Path $ProjectRoot 'stages\01-data-chain\compose.yaml'),'--file',(Join-Path $Stage2Root 'compose.yaml'))

function New-RandomSecret {
    $bytes = New-Object byte[] 24
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return (-join ($bytes | ForEach-Object { $_.ToString('x2') }))
}
function Ensure-Stage2Secrets {
    $envPath = Join-Path $ProjectRoot '.env'
    foreach ($name in @('POSTGRES_PASSWORD','REDIS_PASSWORD','JWT_SECRET')) {
        $existing = Get-Content -LiteralPath $envPath | Where-Object { $_ -match "^$name=" } | Select-Object -Last 1
        if (-not $existing) {
            Add-Content -LiteralPath $envPath -Value "$name=$(New-RandomSecret)" -Encoding utf8
        } elseif (($existing -split '=', 2)[1] -eq '') {
            $lines = Get-Content -LiteralPath $envPath | ForEach-Object { if ($_ -match "^$name=") { "$name=$(New-RandomSecret)" } else { $_ } }
            Set-Content -LiteralPath $envPath -Value $lines -Encoding utf8
        }
    }
}
function Set-Stage2Environment {
    $envPath = Join-Path $ProjectRoot '.env'
    foreach ($line in Get-Content -LiteralPath $envPath) {
        if ($line -match '^([A-Z][A-Z0-9_]*)=(.*)$') { Set-Item -Path "Env:$($Matches[1])" -Value $Matches[2] }
    }
}
function Stage2-RunDirectory { return (Join-Path $ProjectRoot 'artifacts\stage2\runtime') }
