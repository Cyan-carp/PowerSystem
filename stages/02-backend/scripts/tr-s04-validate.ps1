param(
 [Parameter(Mandatory=$true)][string]$Run,
 [Parameter(Mandatory=$true)][string]$EvidenceRoot,
 [Parameter(Mandatory=$true)][string]$OldApiBinary,
 [string]$Tests='TestTRS04'
)
$ErrorActionPreference='Stop'
$trRepo=[IO.Path]::GetFullPath("$PSScriptRoot/../../..")
$trBackend=Join-Path $trRepo 'stages/02-backend'
$trEvidence=[IO.Path]::GetFullPath($EvidenceRoot)
$trAllowed=Join-Path $trRepo 'artifacts/telemetry-reliability/'
if(!$trEvidence.StartsWith($trAllowed,[StringComparison]::OrdinalIgnoreCase)){throw 'Evidence must stay under repository artifacts/telemetry-reliability'}
if($Run -notmatch '^[a-z0-9-]+$'){throw 'Invalid run name'}
if((Test-Path -LiteralPath "$trEvidence/$Run-manifest.json") -or (Test-Path -LiteralPath "$trEvidence/$Run-build.txt")){throw 'Run already exists; retain evidence and use another name'}
New-Item -ItemType Directory -Force -Path $trEvidence|Out-Null
$env:GOCACHE=Join-Path $trEvidence 'go-cache'
$env:TR_S04_FLEET=Join-Path $trRepo 'stages/01-data-chain/simulator/fleet.json'
$env:GOMAXPROCS='4'
$env:TR_S04_TEST_DSN='postgres://tr_s04@127.0.0.1:15442/tr_s04_test?sslmode=disable'
$env:TR_S04_API_BIN=Join-Path $trEvidence "api-$Run.exe"
$env:TR_S04_OLD_API_BIN=(Resolve-Path -LiteralPath $OldApiBinary).Path
$env:TR_S04_EVIDENCE=$trEvidence
$env:TR_S04_RUN=$Run
$trGateway=Join-Path $trEvidence "gateway-$Run.exe"
$trTests=Join-Path $trEvidence "tr-s04-$Run.test.exe"
$trFiles=Get-ChildItem "$trBackend/internal","$trBackend/cmd","$trBackend/deploy","$trBackend/scripts" -Recurse -File|Where-Object{$_.Extension -in '.go','.sql','.sh','.ps1','.py'}
$trFiles+=Get-Item "$trBackend/go.mod","$trBackend/go.sum","$trBackend/README.md",$env:TR_S04_FLEET
$trSource=@($trFiles|Sort-Object FullName|ForEach-Object{[pscustomobject]@{path=$_.FullName;sha256=(Get-FileHash -LiteralPath $_.FullName).Hash.ToLower()}})
$trSource|ConvertTo-Json|Set-Content -Encoding utf8 "$trEvidence/$Run-source-before-build.json"
go version|Set-Content "$trEvidence/$Run-build.txt"
go -C $trBackend build -o $env:TR_S04_API_BIN ./cmd/api 2>&1|Tee-Object -Append "$trEvidence/$Run-build.txt"
if($LASTEXITCODE -ne 0){exit $LASTEXITCODE}
go -C $trBackend build -o $trGateway ./cmd/gateway 2>&1|Tee-Object -Append "$trEvidence/$Run-build.txt"
if($LASTEXITCODE -ne 0){exit $LASTEXITCODE}
go -C $trBackend test -c -tags 'tr_s04 tr_s02' -o $trTests ./internal/api 2>&1|Tee-Object -Append "$trEvidence/$Run-build.txt"
if($LASTEXITCODE -ne 0){exit $LASTEXITCODE}
$trBuildChanged=@($trSource|Where-Object{(Get-FileHash -LiteralPath $_.path).Hash.ToLower() -ne $_.sha256})
if($trBuildChanged.Count){throw 'Source changed during build; candidate rejected'}
$trManifest=@($trSource)
foreach($trBinary in @($env:TR_S04_API_BIN,$trGateway,$trTests,$env:TR_S04_OLD_API_BIN)){
 $trManifest+=[pscustomobject]@{path=$trBinary;sha256=(Get-FileHash -LiteralPath $trBinary).Hash.ToLower()}
}
$trManifest|ConvertTo-Json|Set-Content -Encoding utf8 "$trEvidence/$Run-manifest.json"
$trHash=(Get-FileHash "$trEvidence/$Run-manifest.json").Hash.ToLower()
"BEGIN manifest sha256=$trHash"|Set-Content -Encoding utf8 "$trEvidence/$Run.txt"
Push-Location "$trBackend/internal/api"
try{& $trTests ("-test.run=$Tests") '-test.v=true' '-test.count=1' '-test.timeout=480s' 2>&1|Tee-Object -Append "$trEvidence/$Run.txt";$trExit=$LASTEXITCODE}finally{Pop-Location}
$trChanged=@($trManifest|Where-Object{(Get-FileHash -LiteralPath $_.path).Hash.ToLower() -ne $_.sha256})
"END manifest sha256=$trHash changes=$($trChanged.Count) test_exit=$trExit"|Tee-Object -Append "$trEvidence/$Run.txt"
if($trChanged.Count){exit 2}
exit $trExit
