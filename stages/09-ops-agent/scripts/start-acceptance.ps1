param([Parameter(Mandatory=$true)][string]$ConfigFile, [switch]$Frontend)
$ErrorActionPreference='Stop'
$taskRepo=Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
$taskRun=Split-Path -Parent (Split-Path -Parent (Resolve-Path -LiteralPath $ConfigFile).Path)
$taskLedger=Join-Path $taskRun 'processes.json'
$taskPrevious=if(Test-Path -LiteralPath $taskLedger){Get-Content -LiteralPath $taskLedger -Raw|ConvertFrom-Json}else{$null}
foreach($taskName in @('ops','supervisor','proxy','frontend')) {
 if($taskPrevious -and $taskPrevious.$taskName) {
  $taskOld=$taskPrevious.$taskName
  $taskProcess=Get-Process -Id $taskOld.pid -ErrorAction SilentlyContinue
  if($taskProcess -and $taskProcess.StartTime.ToUniversalTime().Ticks -eq ([datetime]$taskOld.started_at).ToUniversalTime().Ticks){Stop-Process -Id $taskProcess.Id}
 }
}
foreach($line in Get-Content -LiteralPath $ConfigFile -Encoding utf8){if($line -match '^([A-Z][A-Z0-9_]*)=(.*)$'){Set-Item -Path "Env:$($Matches[1])" -Value $Matches[2]}}
$env:PYTHONIOENCODING='utf-8';$env:LANGCHAIN_TRACING_V2='false';$env:HF_HUB_DISABLE_TELEMETRY='1'
$taskResult=@{}
function Start-Owned([string]$Name,[string]$File,[string[]]$Arguments,[string]$WorkDir) {
 $taskStarted=Start-Process -FilePath $File -ArgumentList $Arguments -WorkingDirectory $WorkDir -WindowStyle Hidden -PassThru -RedirectStandardOutput "$taskRun/$Name.log" -RedirectStandardError "$taskRun/$Name.stderr.log"
 $taskResult[$Name]=@{pid=$taskStarted.Id;started_at=$taskStarted.StartTime.ToUniversalTime().ToString('o')}
 $taskResult|ConvertTo-Json|Set-Content -LiteralPath $taskLedger -Encoding utf8
}
foreach($taskPort in @(18081,18093,8094)){if(Get-NetTCPConnection -LocalPort $taskPort -State Listen -ErrorAction SilentlyContinue){throw "Owned port $taskPort is still occupied"}}
Start-Owned 'proxy' "$taskRun/ma-local-api.exe" @() "$taskRepo/stages/02-backend"
$env:PYTHONPATH="$taskRepo/stages/08-agent-rag"
Start-Owned 'supervisor' "$taskRepo/artifacts/stage8-agent-rag/venv/Scripts/python.exe" @('-X','utf8','-m','uvicorn','rag_agent.main:app','--host','127.0.0.1','--port','18093') $taskRepo
$env:PYTHONPATH="$taskRepo/stages/09-ops-agent"
Start-Owned 'ops' "$taskRepo/artifacts/stage9-ops-agent/venv/Scripts/python.exe" @('-X','utf8','-m','uvicorn','ops_agent.main:app','--host','127.0.0.1','--port','8094') $taskRepo
if($Frontend){
 $env:VITE_API_TARGET='http://127.0.0.1:18081'
 $taskNode=(Get-Command node.exe).Source
 $taskBundledNode=Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
 if(Test-Path -LiteralPath $taskBundledNode){$taskNode=$taskBundledNode}
 Start-Owned 'frontend' $taskNode @('node_modules/vite/bin/vite.js','--host','127.0.0.1','--port','15173','--strictPort') "$taskRepo/stages/04-frontend"
}
foreach($taskHealth in @('http://127.0.0.1:18081/api/v1/ping','http://127.0.0.1:18093/health','http://127.0.0.1:8094/health')) {
 $taskReady=$false
 for($taskAttempt=0;$taskAttempt -lt 60;$taskAttempt++) {
  try {$null=Invoke-RestMethod -Uri $taskHealth -TimeoutSec 2;$taskReady=$true;break} catch {Start-Sleep -Seconds 1}
 }
 if(!$taskReady){throw "Local acceptance health timeout: $taskHealth"}
}
Write-Output 'Local acceptance processes ready. Ownership recorded; existing services preserved.'
