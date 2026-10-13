param([Parameter(Mandatory=$true)][string]$ConfigFile,[string]$Manifest)
. (Join-Path $PSScriptRoot 'common.ps1')
Set-RAGEnvironment $ConfigFile
$run=Split-Path -Parent (Split-Path -Parent (Resolve-Path -LiteralPath $ConfigFile).Path)
$arguments=@('-X','utf8',(Join-Path $PSScriptRoot 'knowledge.py'),'--report',"$run/knowledge-import.json")
if ($Manifest) {$arguments+=@('--manifest',$Manifest)}
& $RAGPython @arguments
if ($LASTEXITCODE -ne 0) {throw 'Knowledge import failed; active collection not replaced before validation.'}
