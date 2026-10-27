param([Parameter(Mandatory=$true)][string]$RunDirectory,[ValidateSet('A','B')][string]$Machine='A',[ValidateRange(1,60)][int]$Minutes=45)
$ErrorActionPreference='Stop'
$env:PSModulePath = (Join-Path $PSHOME 'Modules') + [IO.Path]::PathSeparator + $env:PSModulePath
$root=[IO.Path]::GetFullPath($RunDirectory);[IO.Directory]::CreateDirectory($root)|Out-Null
$setup=Join-Path (Split-Path $root -Parent) 'PowerSystem-Desktop-0.2.0-x64-Setup.exe'
if(-not(Test-Path -LiteralPath $setup -PathType Leaf)){throw 'Frozen team installer is missing'}
if((Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash -ne '045f1aa62ba42428cbf43cffb07b05d6a44ee0545952ab59713880873a71ca23'){throw 'Frozen team installer SHA256 mismatch. Stop installation.'}
Write-Output 'Frozen HTTPS team installer SHA256 verified.'
$start=[DateTime]::UtcNow;$last='';$pending='';$since=$start;$captures=0
$eventFile=Join-Path $root ('watch-'+$Machine+'-'+$start.ToString('yyyyMMddTHHmmssZ')+'.jsonl')
Write-Output 'S03 read-only evidence watcher ready. Install/exit/upgrade/uninstall nodes are captured automatically.'
Write-Output 'Keep this window open; leave each node stable for at least 8 seconds. Ctrl+C ends monitoring.'
while([DateTime]::UtcNow -lt $start.AddMinutes($Minutes) -and -not(Test-Path -LiteralPath (Join-Path $root 'stop-watch.flag'))){
  $records=@(Get-ChildItem -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall' -ErrorAction SilentlyContinue|Get-ItemProperty|Where-Object {$_.PSChildName -eq '696e88de-db89-5bb4-91b5-b72e17354c33'})
  $procs=@(Get-Process -Name 'PowerSystem Desktop' -ErrorAction SilentlyContinue)
  $signature=(@($records.DisplayVersion)-join ',')+'/'+(@($procs.Id|Sort-Object)-join ',')+'/'+@($procs|Where-Object {$_.MainWindowHandle -ne 0}).Count
  if($signature -ne $pending){$pending=$signature;$since=[DateTime]::UtcNow}
  if($pending -ne $last -and ([DateTime]::UtcNow-$since).TotalSeconds -ge 2){
    $last=$pending;$phase=if($records.Count -eq 0 -and $procs.Count -eq 0){'uninstalled'}elseif($procs.Count -eq 0){'closed'}else{'running'}
    [ordered]@{at=[DateTime]::UtcNow.ToString('o');phase=$phase;versions=@($records|ForEach-Object {$_.DisplayVersion});processIds=@($procs|ForEach-Object {$_.Id});processCount=$procs.Count;windows=@($procs|Where-Object {$_.MainWindowHandle -ne 0}).Count}|ConvertTo-Json -Compress|Add-Content -LiteralPath $eventFile -Encoding utf8
    if($captures -lt 32){& (Join-Path $PSScriptRoot 'Collect-S03Evidence.ps1') -RunDirectory $root -Machine $Machine -Node $phase;$captures++}
  }
  Start-Sleep -Seconds 1
}
Write-Output 'S03 monitoring completed.'
