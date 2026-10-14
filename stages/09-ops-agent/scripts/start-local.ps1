param([Parameter(Mandatory=$true)][string]$ConfigFile)
$ErrorActionPreference='Stop'
$taskRepo=Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
foreach ($line in Get-Content -LiteralPath $ConfigFile -Encoding utf8) {
 if ($line -match '^([A-Z][A-Z0-9_]*)=(.*)$') { Set-Item -Path "Env:$($Matches[1])" -Value $Matches[2] }
}
$taskRun=Split-Path -Parent (Split-Path -Parent (Resolve-Path -LiteralPath $ConfigFile).Path)
$env:PYTHONIOENCODING='utf-8';$env:LANGCHAIN_TRACING_V2='false';$env:HF_HUB_DISABLE_TELEMETRY='1'
$env:PYTHONPATH=Join-Path $taskRepo 'stages/09-ops-agent'
$taskPython=Join-Path $taskRepo 'artifacts/stage9-ops-agent/venv/Scripts/python.exe'
if (Get-NetTCPConnection -LocalPort 8094 -State Listen -ErrorAction SilentlyContinue) {throw '8094 already occupied'}
$taskProcess=Start-Process -FilePath $taskPython -ArgumentList @('-X','utf8','-m','uvicorn','ops_agent.main:app','--host','127.0.0.1','--port','8094') -WorkingDirectory $taskRepo -WindowStyle Hidden -PassThru -RedirectStandardOutput "$taskRun/ops.log" -RedirectStandardError "$taskRun/ops.stderr.log"
@{pid=$taskProcess.Id;started_at=$taskProcess.StartTime.ToUniversalTime().ToString('o')}|ConvertTo-Json|Set-Content -LiteralPath "$taskRun/ops-process.json" -Encoding utf8
