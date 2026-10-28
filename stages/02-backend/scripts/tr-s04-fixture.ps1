param([ValidateSet('start','stop','inventory')][string]$Action='inventory',[Parameter(Mandatory=$true)][string]$EvidenceRoot)
$ErrorActionPreference='Stop'
$trRepo=[IO.Path]::GetFullPath("$PSScriptRoot/../../..")
$trEvidence=[IO.Path]::GetFullPath($EvidenceRoot)
$trAllowed=Join-Path $trRepo 'artifacts/telemetry-reliability/'
if(!$trEvidence.StartsWith($trAllowed,[StringComparison]::OrdinalIgnoreCase)){throw 'Fixture evidence must stay under repository artifacts'}
$trDocker=(Get-Command docker.exe -ErrorAction SilentlyContinue).Source
if(!$trDocker){$trDocker=Join-Path $env:LOCALAPPDATA 'Programs/DockerDesktop/resources/bin/docker.exe'}
$trNames=@('tr-s04-pg','tr-s04-redis','tr-s04-td','tr-s04-mqtt')
$trReceipt=Join-Path $trEvidence 'fixture-receipt.json'
New-Item -ItemType Directory -Force -Path $trEvidence|Out-Null
if($Action -eq 'inventory'){
 $trRecords=@(foreach($trName in $trNames){$trObject=& $trDocker inspect $trName|ConvertFrom-Json;if($LASTEXITCODE){throw 'Dedicated fixture missing'};[pscustomobject]@{name=$trName;id=$trObject.Id;image=$trObject.Image;created=$trObject.Created;nano_cpus=$trObject.HostConfig.NanoCpus;memory=$trObject.HostConfig.Memory;ports=$trObject.NetworkSettings.Ports}})
 if(Test-Path -LiteralPath $trReceipt){$trPrior=Get-Content -Raw $trReceipt|ConvertFrom-Json;foreach($trRecord in $trRecords){if(($trPrior|Where-Object name -eq $trRecord.name).id -ne $trRecord.id){throw 'Fixture identity changed'}}}
 $trRecords|ConvertTo-Json -Depth 8|Set-Content -Encoding utf8 $trReceipt
 exit 0
}
if(Test-Path -LiteralPath $trReceipt){
 $trPrior=Get-Content -Raw $trReceipt|ConvertFrom-Json
 foreach($trName in $trNames){$trID=& $trDocker inspect --format '{{.Id}}' $trName;if($LASTEXITCODE -or $trID -ne ($trPrior|Where-Object name -eq $trName).id){throw 'Refuse unrelated or replaced fixture'};& $trDocker $Action $trName;if($LASTEXITCODE){throw 'Fixture lifecycle failed'}}
 exit 0
}
if($Action -eq 'stop'){throw 'No ownership receipt; refusing stop'}
foreach($trName in $trNames){& $trDocker inspect $trName 2>$null|Out-Null;if($LASTEXITCODE -eq 0){throw 'Existing name without receipt; inspect ownership first'}}
& $trDocker network create tr-s04-isolated
if($LASTEXITCODE){throw 'Network exists or create failed; inspect first'}
& $trDocker run -d --name tr-s04-pg --network tr-s04-isolated --cpus 2 --memory 1g -p 127.0.0.1:15442:5432 -e POSTGRES_USER=tr_s04 -e POSTGRES_DB=tr_s04_test -e POSTGRES_HOST_AUTH_METHOD=trust postgres:17.6
if($LASTEXITCODE){throw 'PG create failed'}
& $trDocker run -d --name tr-s04-redis --network tr-s04-isolated --cpus 1 --memory 256m -p 127.0.0.1:16392:6379 redis:7.4.5-alpine redis-server --save '' --appendonly no
if($LASTEXITCODE){throw 'Redis create failed'}
& $trDocker run -d --name tr-s04-td --hostname tr-s04-td --network tr-s04-isolated --cpus 2 --memory 2g -p 127.0.0.1:16042:6041 tdengine/tsdb:3.4.2.8
if($LASTEXITCODE){throw 'TD create failed'}
& $trDocker run -d --name tr-s04-mqtt --network tr-s04-isolated --cpus 1 --memory 1g -p 127.0.0.1:11884:1883 emqx:5.8.8
if($LASTEXITCODE){throw 'MQTT create failed'}
& $PSCommandPath -Action inventory -EvidenceRoot $trEvidence
