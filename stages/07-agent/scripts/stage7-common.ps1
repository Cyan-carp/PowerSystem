Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Stage7Root = Split-Path -Parent $PSScriptRoot
$Stage7Repo = Split-Path -Parent (Split-Path -Parent $Stage7Root)
$Stage7Runtime = Join-Path $Stage7Repo 'artifacts/stage7/runtime'
$Stage7Python = Join-Path $Stage7Repo '.venv/Scripts/python.exe'
function Set-Stage7Environment {
    param([string]$ConfigFile = (Join-Path $Stage7Root '.env'))
    foreach ($file in @((Join-Path $Stage7Repo '.env'), $ConfigFile)) {
        if (-not (Test-Path -LiteralPath $file)) { throw "Missing private configuration: $file" }
        foreach ($line in Get-Content -LiteralPath $file) {
            if ($line -match '^([A-Z][A-Z0-9_]*)=(.*)$') { Set-Item -Path "Env:$($Matches[1])" -Value $Matches[2] }
        }
    }
    $env:PYTHONPATH = (Join-Path $Stage7Repo 'artifacts/stage7/pydeps') + ';' + $Stage7Root
    $env:PYTHONIOENCODING = 'utf-8'
    foreach ($name in @('AGENT_SERVICE_TOKEN_FILE','AGENT_LLM_API_KEY_FILE','AGENT_MONITOR_TOKEN_FILE','AGENT_AUDIT_DIR','AGENT_SEARCH_KEY_FILE','AGENT_MISSES_DIR','AGENT_KNOWLEDGE_PATH')) {
        $value=[Environment]::GetEnvironmentVariable($name)
        if ($value -and -not [IO.Path]::IsPathRooted($value)) { Set-Item -Path "Env:$name" -Value (Join-Path $Stage7Repo $value) }
    }
    if (-not $env:AGENT_REDIS_URL) {
        $redisAddress = & $Stage7Python -m agent.cli redis-url
        if ($LASTEXITCODE -ne 0) { throw 'Could not configure Redis.' }
        $env:AGENT_REDIS_URL = $redisAddress
    }
}
function Start-Stage7Managed {
    param([string]$Name, [string]$Executable, [string[]]$Arguments, [string]$WorkingDirectory)
    New-Item -ItemType Directory -Path $Stage7Runtime -Force | Out-Null
    $file = Join-Path $Stage7Runtime "$Name-process.json"
    if (Test-Path -LiteralPath $file) {
        $state = Get-Content -LiteralPath $file -Raw | ConvertFrom-Json
        $old = Get-Process -Id $state.pid -ErrorAction SilentlyContinue
        if ($old -and [Math]::Abs(($old.StartTime.ToUniversalTime() - ([DateTime]$state.started_at).ToUniversalTime()).TotalSeconds) -lt 2) { throw "$Name already running" }
    }
    $launch = @{FilePath=$Executable;WorkingDirectory=$WorkingDirectory;PassThru=$true;WindowStyle='Hidden';RedirectStandardOutput=(Join-Path $Stage7Runtime "$Name.log");RedirectStandardError=(Join-Path $Stage7Runtime "$Name.stderr.log")}
    if ($Arguments.Count -gt 0) { $launch.ArgumentList=$Arguments }
    $process = Start-Process @launch
    @{pid=$process.Id;started_at=$process.StartTime.ToUniversalTime().ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath $file -Encoding utf8
    return $process
}
