param([Parameter(Mandatory=$true)][string]$ConfigFile)
. (Join-Path $PSScriptRoot 'common.ps1')
Set-RAGEnvironment $ConfigFile
$run=Split-Path -Parent (Split-Path -Parent (Resolve-Path -LiteralPath $ConfigFile).Path)
if (Get-NetTCPConnection -LocalPort 8093 -State Listen -ErrorAction SilentlyContinue) {throw 'Agent RAG port8093 already occupied'}
$proc=Start-Process -FilePath $RAGPython -ArgumentList @('-X','utf8','-m','uvicorn','rag_agent.main:app','--host','127.0.0.1','--port','8093') -WorkingDirectory $RAGRepo -WindowStyle Hidden -PassThru -RedirectStandardOutput "$run/agent.log" -RedirectStandardError "$run/agent.stderr.log"
@{pid=$proc.Id;started_at=$proc.StartTime.ToUniversalTime().ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath "$run/agent-process.json" -Encoding utf8
Write-Output 'Agent starting; local Embedding warmup may take a minute on the first run.'
for ($attempt=0; $attempt -lt 90; $attempt++) {
 if ($proc.HasExited) {throw 'Agent exited during startup; inspect private logs.'}
 try {
  $health=Invoke-RestMethod 'http://127.0.0.1:8093/health' -TimeoutSec 1
  if ($health.engine -eq 'agent-rag') {Write-Output "Agent ready on8093; embedding_ready=$($health.embedding_ready). Real-model probe is separate."; return}
 } catch {}
 Start-Sleep -Seconds 2
}
throw 'Agent startup health timed out; inspect private logs before sending chat requests.'
