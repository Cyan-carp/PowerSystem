param([Parameter(Mandatory=$true)][string]$ConfigFile, [switch]$Stop)
$ErrorActionPreference='Stop'
$secRoot=(Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
$secFile=(Resolve-Path -LiteralPath $ConfigFile).Path
$secRun=Split-Path (Split-Path $secFile -Parent) -Parent
$secAllowed=[IO.Path]::GetFullPath((Join-Path $secRoot 'artifacts/stage10-security-agent'))+[IO.Path]::DirectorySeparatorChar
if(-not $secRun.StartsWith($secAllowed,[StringComparison]::OrdinalIgnoreCase)){throw 'invalid_sec_run'}
$secRecord=Join-Path $secRun 'process.json'
if($Stop){
    if(Test-Path -LiteralPath $secRecord){
        $secProc=Get-Content -LiteralPath $secRecord -Encoding utf8 | ConvertFrom-Json
        $secSaved=@($secProc.processes)
        if(-not $secProc.processes){
            # Compatibility with the first-run launcher-only record.
            $secSaved=@(@{pid=$secProc.pid;started_at=$secProc.started_at})
            $secLauncher=Get-Process -Id $secProc.pid -ErrorAction SilentlyContinue
            $secLaunchTime=[DateTimeOffset]$secProc.started_at
            if($secLauncher -and $secLauncher.StartTime.ToUniversalTime().Ticks -eq $secLaunchTime.UtcDateTime.Ticks){
                foreach($secChild in Get-CimInstance Win32_Process -Filter "ParentProcessId=$($secProc.pid)"){
                    if($secChild.CommandLine -match 'security_agent.main:app.*--host 127\.0\.0\.1.*--port 8100' -and $secChild.CreationDate.ToUniversalTime() -ge $secLaunchTime.UtcDateTime){
                        $secSaved+=@{pid=$secChild.ProcessId;started_at=$secChild.CreationDate.ToUniversalTime().ToString('o')}
                    }
                }
            }
        }
        [array]::Reverse($secSaved)
        foreach($secEntry in $secSaved){
            $secRunning=Get-Process -Id $secEntry.pid -ErrorAction SilentlyContinue
            $secExpected=[DateTimeOffset]$secEntry.started_at
            if($secRunning -and $secRunning.StartTime.ToUniversalTime().Ticks -eq $secExpected.UtcDateTime.Ticks){Stop-Process -Id $secEntry.pid -ErrorAction SilentlyContinue}
        }
    }
    try{Invoke-WebRequest http://127.0.0.1:8100/health -TimeoutSec 2 | Out-Null;throw 'security_service_still_reachable'}catch{
        if($_.Exception.Message -eq 'security_service_still_reachable'){throw}
    }
    return
}
$secSocket=[Net.Sockets.TcpClient]::new()
try{$secSocket.Connect('127.0.0.1',8100);throw 'security_port_in_use'}catch{if($_.Exception.Message -eq 'security_port_in_use'){throw}}finally{$secSocket.Dispose()}
foreach($secLine in Get-Content -LiteralPath $secFile -Encoding utf8){if($secLine -match '^([A-Z][A-Z0-9_]*)=(.*)$'){Set-Item -Path "Env:$($Matches[1])" -Value $Matches[2]}}
$secPython=Join-Path $secRoot 'artifacts/stage9-ops-agent/venv/Scripts/python.exe'
$env:PYTHONPATH=Join-Path $secRoot 'stages/10-security-agent'
$secStamp=Get-Date -Format 'yyyyMMdd-HHmmss-ffff'
$secStarted=Start-Process -FilePath $secPython -ArgumentList '-X','utf8','-m','uvicorn','security_agent.main:app','--host','127.0.0.1','--port','8100','--workers','1','--no-access-log' -WorkingDirectory (Join-Path $secRoot 'stages/10-security-agent') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $secRun "service-$secStamp.log") -RedirectStandardError (Join-Path $secRun "service-$secStamp.err")
@{pid=$secStarted.Id;started_at=$secStarted.StartTime.ToUniversalTime().ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath $secRecord -Encoding utf8
for($secAttempt=0;$secAttempt -lt 30;$secAttempt++){
    try{
        $secHealth=Invoke-RestMethod http://127.0.0.1:8100/health -TimeoutSec 1
        if($secHealth.engine -eq 'security-agent'){
            $secSaved=@(@{pid=$secStarted.Id;started_at=$secStarted.StartTime.ToUniversalTime().ToString('o')})
            foreach($secChild in Get-CimInstance Win32_Process -Filter "ParentProcessId=$($secStarted.Id)"){
                if($secChild.CommandLine -match 'security_agent.main:app.*--host 127\.0\.0\.1.*--port 8100'){
                    $secSaved+=@{pid=$secChild.ProcessId;started_at=$secChild.CreationDate.ToUniversalTime().ToString('o')}
                }
            }
            @{processes=$secSaved} | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $secRecord -Encoding utf8
            Write-Output 'Security service ready on loopback 8100';return
        }
    }catch{}
    Start-Sleep -Milliseconds 300
}
throw 'security_service_not_ready'
