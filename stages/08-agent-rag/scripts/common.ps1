Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$RAGRoot=Split-Path -Parent $PSScriptRoot
$RAGRepo=Split-Path -Parent (Split-Path -Parent $RAGRoot)
$RAGPython=Join-Path $RAGRepo 'artifacts/stage8-agent-rag/venv/Scripts/python.exe'
function Set-RAGEnvironment([string]$ConfigFile) {
 foreach ($line in Get-Content -LiteralPath $ConfigFile -Encoding utf8) {
  if ($line -match '^([A-Z][A-Z0-9_]*)=(.*)$') {Set-Item -Path "Env:$($Matches[1])" -Value $Matches[2]}
 }
 $env:PYTHONPATH=$RAGRoot
 $env:PYTHONIOENCODING='utf-8'
 $env:TOKENIZERS_PARALLELISM='false'
 $env:HF_HUB_DISABLE_TELEMETRY='1'
 $env:LANGCHAIN_TRACING_V2='false'
 $env:OMP_NUM_THREADS='2'
 foreach ($name in @('AGENT_SERVICE_TOKEN_FILE','AGENT_LLM_API_KEY_FILE','AGENT_SEARCH_KEY_FILE','RAG_EMBEDDING_PATH','RAG_AUDIT_DIR')) {
  $value=[Environment]::GetEnvironmentVariable($name)
  if ($value -and -not [IO.Path]::IsPathRooted($value)) {Set-Item -Path "Env:$name" -Value (Join-Path $RAGRepo $value)}
 }
}
