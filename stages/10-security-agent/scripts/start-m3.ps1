param([Parameter(Mandatory=$true)][string]$ConfigFile,[switch]$Stop,[switch]$Frontend,[switch]$Initialize)
$ErrorActionPreference='Stop'
$secRepo=Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
$secRun=Split-Path -Parent (Split-Path -Parent (Resolve-Path -LiteralPath $ConfigFile).Path)
$secLedger=Join-Path $secRun 'processes.json'
if((Split-Path -Leaf $secRun) -notlike 'sec-m3-*'){throw 'SEC-M3 batch required'}
if(Test-Path -LiteralPath $secLedger){
 $secPrevious=Get-Content -LiteralPath $secLedger -Raw -Encoding utf8|ConvertFrom-Json
 foreach($secEntry in $secPrevious.PSObject.Properties){
  $secOld=$secEntry.Value;$secProcess=Get-Process -Id $secOld.pid -ErrorAction SilentlyContinue
  if($secProcess -and $secProcess.StartTime.ToUniversalTime().Ticks -eq ([datetime]$secOld.started_at).ToUniversalTime().Ticks){Stop-Process -Id $secProcess.Id}
 }
}
if($Stop){
 foreach($secPort in @(18105,18097,8110,8111,8112,15179)){
  $secSocket=[System.Net.Sockets.TcpClient]::new()
  try {if($secSocket.ConnectAsync('127.0.0.1',$secPort).Wait(200) -and $secSocket.Connected){throw "Owned port $secPort remains reachable after stop"}} catch [System.AggregateException] {} finally {$secSocket.Dispose()}
 }
 return
}
foreach($secLine in Get-Content -LiteralPath $ConfigFile -Encoding utf8){if($secLine -match '^([A-Z][A-Z0-9_]*)=(.*)$'){Set-Item -Path "Env:$($Matches[1])" -Value $Matches[2]}}
$env:PYTHONIOENCODING='utf-8';$env:LANGCHAIN_TRACING_V2='false';$env:HF_HUB_DISABLE_TELEMETRY='1';$env:HF_HUB_OFFLINE='1'
$env:AGENT_ENABLED='true';$env:FEISHU_BUSINESS_ENABLED='false';$env:AI_ENABLED='false'
if($Initialize){
 Push-Location "$secRepo/stages/02-backend"
 try {
  & go build -o "$secRun/ma-local-api.exe" ./cmd/ma-local-api
  if($LASTEXITCODE -ne 0){throw 'Local proxy build failed'}
  & go run ./cmd/ma-m3-fixture
  if($LASTEXITCODE -ne 0){throw 'Dedicated fixture initialization failed'}
 } finally {Pop-Location}
}
foreach($secPort in @(18105,18097,8110,8111,8112,15179)){
 $secSocket=[System.Net.Sockets.TcpClient]::new()
 try {if($secSocket.ConnectAsync('127.0.0.1',$secPort).Wait(150) -and $secSocket.Connected){throw "Port $secPort occupied"}} catch [System.AggregateException] {} finally {$secSocket.Dispose()}
}
$secResult=@{}
function Start-SEC([string]$Name,[string]$File,[string[]]$Arguments,[string]$WorkDir){
 $secStamp=Get-Date -Format 'yyyyMMdd-HHmmss-fff'
 $secRemoved=@{}
 foreach($secVar in Get-ChildItem Env:){
  if($secVar.Name -notmatch '^(SEC_|OPS_|AGENT_|RAG_|BND_|MA_|POSTGRES_|REDIS_|TDENGINE_|JWT_SECRET$|HTTP_ADDR$|FEISHU_|AI_|VITE_)'){continue}
  $secKeep=switch($Name){
   'security' {$secVar.Name -match '^SEC_'}
   'ops' {$secVar.Name -match '^(OPS_|MA_|SEC_SERVICE_TOKEN_FILE$|SEC_REQUEST_KEY_FILE$|SEC_RESULT_KEY_FILE$|AGENT_SERVICE_TOKEN_FILE$)'}
   'adapter' {$secVar.Name -eq 'OPS_EXECUTOR_CONFIG_FILE'}
   'supervisor' {$secVar.Name -match '^(AGENT_|RAG_|BND_|OPS_URL$|OPS_SERVICE_TOKEN_FILE$|MA_ACTOR_KEY_FILE$|MA_ENVELOPE_KEY_FILE$|MA_DRILL_ACCEPTANCE_ONLY$)'}
   'proxy' {$secVar.Name -match '^(POSTGRES_|REDIS_|TDENGINE_|JWT_SECRET$|HTTP_ADDR$|AGENT_ENABLED$|AGENT_URL$|AGENT_CHAT_|AGENT_SERVICE_TOKEN_FILE$|OPS_URL$|OPS_SERVICE_TOKEN_FILE$|MA_ACTOR_KEY_FILE$|FEISHU_|AI_)'}
   'frontend' {$secVar.Name -eq 'VITE_API_TARGET'}
  }
  if(-not $secKeep){$secRemoved[$secVar.Name]=$secVar.Value;Remove-Item -LiteralPath "Env:$($secVar.Name)"}
 }
 try {$secStarted=Start-Process -FilePath $File -ArgumentList $Arguments -WorkingDirectory $WorkDir -WindowStyle Hidden -PassThru -RedirectStandardOutput "$secRun/$Name-$secStamp.log" -RedirectStandardError "$secRun/$Name-$secStamp.stderr.log"}
 finally {foreach($secPair in $secRemoved.GetEnumerator()){Set-Item -Path "Env:$($secPair.Key)" -Value $secPair.Value}}
 $secResult[$Name]=@{pid=$secStarted.Id;started_at=$secStarted.StartTime.ToUniversalTime().ToString('o')}
 $secResult|ConvertTo-Json|Set-Content -LiteralPath $secLedger -Encoding utf8
}
$secExecutorConfig=$env:OPS_EXECUTOR_CONFIG_FILE
Remove-Item Env:OPS_EXECUTOR_CONFIG_FILE -ErrorAction SilentlyContinue
Start-SEC 'proxy' "$secRun/ma-local-api.exe" @() "$secRepo/stages/02-backend"
$env:PYTHONPATH="$secRepo/stages/10-security-agent"
Start-SEC 'security' "$secRepo/artifacts/stage9-ops-agent/venv/Scripts/python.exe" @('-X','utf8','-m','uvicorn','security_agent.main:app','--host','127.0.0.1','--port','8110','--no-access-log') $secRepo
$env:PYTHONPATH="$secRepo/stages/09-ops-agent;$secRepo/stages/10-security-agent"
Start-SEC 'ops' "$secRepo/artifacts/stage9-ops-agent/venv/Scripts/python.exe" @('-X','utf8','-m','uvicorn','ops_agent.main:app','--host','127.0.0.1','--port','8111','--no-access-log') $secRepo
$env:OPS_EXECUTOR_CONFIG_FILE=$secExecutorConfig
Start-SEC 'adapter' "$secRepo/artifacts/stage9-ops-agent/venv/Scripts/python.exe" @('-X','utf8','-m','uvicorn','ops_executor.main:app','--host','127.0.0.1','--port','8112','--no-access-log') $secRepo
Remove-Item Env:OPS_EXECUTOR_CONFIG_FILE
$env:PYTHONPATH="$secRepo/stages/08-agent-rag"
Start-SEC 'supervisor' "$secRepo/artifacts/stage8-agent-rag/venv/Scripts/python.exe" @('-X','utf8','-m','uvicorn','rag_agent.main:app','--host','127.0.0.1','--port','18097','--no-access-log') $secRepo
if($Frontend){
 $env:VITE_API_TARGET='http://127.0.0.1:18105'
 $secNode=Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
 if(-not (Test-Path -LiteralPath $secNode)){$secNode=(Get-Command node).Source}
 Start-SEC 'frontend' $secNode @('node_modules/vite/bin/vite.js','--host','127.0.0.1','--port','15179','--strictPort') "$secRepo/stages/04-frontend"
}
Write-Output 'SEC-M3 owned local processes started; readiness must be checked before acceptance.'
