param([Parameter(Mandatory=$true)][string]$ConfigFile,[switch]$Frontend)
$ErrorActionPreference='Stop'
$maRepo=Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
$maRun=Split-Path -Parent (Split-Path -Parent (Resolve-Path -LiteralPath $ConfigFile).Path)
$maLedger=Join-Path $maRun 'processes.json'
$maPrevious=if(Test-Path -LiteralPath $maLedger){Get-Content -LiteralPath $maLedger -Raw|ConvertFrom-Json}else{$null}
foreach($maName in @('adapter','ops','supervisor','proxy','frontend')){
 if($maPrevious -and $maPrevious.$maName){
  $maOld=$maPrevious.$maName;$maProcess=Get-Process -Id $maOld.pid -ErrorAction SilentlyContinue
  if($maProcess -and $maProcess.StartTime.ToUniversalTime().Ticks -eq ([datetime]$maOld.started_at).ToUniversalTime().Ticks){Stop-Process -Id $maProcess.Id}
 }
}
foreach($maLine in Get-Content -LiteralPath $ConfigFile -Encoding utf8){if($maLine -match '^([A-Z][A-Z0-9_]*)=(.*)$'){Set-Item -Path "Env:$($Matches[1])" -Value $Matches[2]}}
$env:PYTHONIOENCODING='utf-8';$env:LANGCHAIN_TRACING_V2='false';$env:HF_HUB_DISABLE_TELEMETRY='1'
$maConfig=$env:OPS_EXECUTOR_CONFIG_FILE
Remove-Item Env:OPS_EXECUTOR_CONFIG_FILE -ErrorAction SilentlyContinue
$maResult=@{}
function Start-MA([string]$Name,[string]$File,[string[]]$Arguments,[string]$WorkDir){
 $maStarted=Start-Process -FilePath $File -ArgumentList $Arguments -WorkingDirectory $WorkDir -WindowStyle Hidden -PassThru -RedirectStandardOutput "$maRun/$Name.log" -RedirectStandardError "$maRun/$Name.stderr.log"
 $maResult[$Name]=@{pid=$maStarted.Id;started_at=$maStarted.StartTime.ToUniversalTime().ToString('o')}
 $maResult|ConvertTo-Json|Set-Content -LiteralPath $maLedger -Encoding utf8
}
foreach($maPort in @(18082,18094,8096,8095)){if(Get-NetTCPConnection -LocalPort $maPort -State Listen -ErrorAction SilentlyContinue){throw "MA-M2 port $maPort is occupied"}}
Start-MA 'proxy' "$maRun/ma-local-api.exe" @() "$maRepo/stages/02-backend"
$env:PYTHONPATH="$maRepo/stages/08-agent-rag"
Start-MA 'supervisor' "$maRepo/artifacts/stage8-agent-rag/venv/Scripts/python.exe" @('-X','utf8','-m','uvicorn','rag_agent.main:app','--host','127.0.0.1','--port','18094') $maRepo
$env:PYTHONPATH="$maRepo/stages/09-ops-agent"
Start-MA 'ops' "$maRepo/artifacts/stage9-ops-agent/venv/Scripts/python.exe" @('-X','utf8','-m','uvicorn','ops_agent.main:app','--host','127.0.0.1','--port','8096') $maRepo
$env:OPS_EXECUTOR_CONFIG_FILE=$maConfig
Start-MA 'adapter' "$maRepo/artifacts/stage9-ops-agent/venv/Scripts/python.exe" @('-X','utf8','-m','uvicorn','ops_executor.main:app','--host','127.0.0.1','--port','8095','--no-access-log') $maRepo
Remove-Item Env:OPS_EXECUTOR_CONFIG_FILE
if($Frontend){
 $env:VITE_API_TARGET='http://127.0.0.1:18082'
 $maNode=Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
 Start-MA 'frontend' $maNode @('node_modules/vite/bin/vite.js','--host','127.0.0.1','--port','15176','--strictPort') "$maRepo/stages/04-frontend"
}
foreach($maHealth in @('http://127.0.0.1:18082/api/v1/ping','http://127.0.0.1:18094/health','http://127.0.0.1:8096/health','http://127.0.0.1:8095/health')){
 $maReady=$false
 for($maAttempt=0;$maAttempt -lt 60;$maAttempt++){try{$null=Invoke-RestMethod -Uri $maHealth -TimeoutSec 2;$maReady=$true;break}catch{Start-Sleep -Seconds 1}}
 if(!$maReady){throw "MA-M2 health timeout: $maHealth"}
}
Write-Output 'MA-M2 owned loopback processes ready; earlier local and server services preserved.'
