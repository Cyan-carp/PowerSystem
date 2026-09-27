Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$StageRoot = Split-Path -Parent $PSScriptRoot
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $StageRoot)
$ComposeArgs = @('compose', '--project-name', 'powersystem', '--env-file', (Join-Path $ProjectRoot '.env'), '--file', (Join-Path $StageRoot 'compose.yaml'))

function Get-DockerExe {
    $found = Get-Command docker -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    $candidate = Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\resources\bin\docker.exe'
    if (Test-Path -LiteralPath $candidate) { return $candidate }
    throw 'Docker CLI 未找到。请启动或修复 Docker Desktop 后重试。'
}

function Get-PythonExe {
    $candidate = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $candidate) { return $candidate }
    throw 'Python 虚拟环境不存在，请先运行 stages/01-data-chain/scripts/preflight-stage1.ps1。'
}

function Get-LocalSecret {
    $envPath = Join-Path $ProjectRoot '.env'
    if (-not (Test-Path -LiteralPath $envPath)) {
        $bytes = New-Object byte[] 12
        $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
        try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
        $hex = -join ($bytes | ForEach-Object { $_.ToString('x2') })
        Set-Content -LiteralPath $envPath -Value "TDENGINE_ROOT_PASSWORD=Aa7!$hex" -Encoding utf8
    }
    $line = Get-Content -LiteralPath $envPath | Where-Object { $_ -match '^TDENGINE_ROOT_PASSWORD=' } | Select-Object -Last 1
    if (-not $line) { throw '.env 缺少 TDENGINE_ROOT_PASSWORD。' }
    $value = ($line -split '=', 2)[1]
    if ($value -eq 'change-this-local-password' -or $value.Length -lt 8) { throw '.env 中的 TDengine 密码尚未正确设置。' }
    if ($value -notmatch '^[A-Za-z0-9!@#%._-]+$') { throw '.env 密码只能使用英文字母、数字和 ! @ # % . _ -。' }
    return $value
}

function Invoke-TDSql {
    param([Parameter(Mandatory)][string]$Sql, [Parameter(Mandatory)][string]$Password)
    $pair = "root:$Password"
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($pair))
    $result = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:6041/rest/sql' -Headers @{ Authorization = "Basic $encoded" } -ContentType 'text/plain' -Body $Sql -TimeoutSec 15
    if ($result.code -ne 0) { throw "TDengine SQL 失败：$($result.code) $($result.desc)" }
    return $result
}

function Initialize-TDDatabase {
    param([Parameter(Mandatory)][string]$Database, [Parameter(Mandatory)][string]$Password)
    if ($Database -notmatch '^[a-z][a-z0-9_]{0,63}$') { throw '数据库名含有非法字符。' }
    $template = Get-Content -LiteralPath (Join-Path $StageRoot 'deploy\tdengine\init.sql') -Raw
    $statements = ($template -replace '(?m)^--.*$', '' -replace '\{\{DATABASE\}\}', $Database) -split ';'
    foreach ($statement in $statements) {
        if ($statement.Trim()) { $null = Invoke-TDSql -Sql $statement.Trim() -Password $Password }
    }
}

function Write-RunEvent {
    param([string]$RunDir, [string]$Event, [hashtable]$Fields = @{})
    $record = [ordered]@{ at = [DateTime]::UtcNow.ToString('o'); event = $Event }
    foreach ($key in $Fields.Keys) { $record[$key] = $Fields[$key] }
    Add-Content -LiteralPath (Join-Path $RunDir 'events.jsonl') -Value ($record | ConvertTo-Json -Compress -Depth 5) -Encoding utf8
}

function Get-QueueStatus {
    param([string]$RunDir)
    $python = Get-PythonExe
    $output = & $python (Join-Path $StageRoot 'scripts\queue-status.py') $RunDir
    if ($LASTEXITCODE -ne 0) { throw '无法读取 SQLite 队列计数。' }
    return ($output | ConvertFrom-Json)
}
