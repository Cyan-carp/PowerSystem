param(
  [Parameter(Mandatory=$true)][string]$RunDirectory,
  [ValidateSet('A','B')][string]$Machine = 'A',
  [string]$Node = 'snapshot'
)
$ErrorActionPreference = 'Stop'
# A caller running PowerShell 7 can pass its module path to Windows PowerShell.
# Prefer this host's bundled modules in this process only.
$env:PSModulePath = (Join-Path $PSHOME 'Modules') + [IO.Path]::PathSeparator + $env:PSModulePath
$outputRoot = [IO.Path]::GetFullPath($RunDirectory)
[IO.Directory]::CreateDirectory($outputRoot) | Out-Null
$exeName = 'PowerSystem Desktop.exe'
$records = @(Get-ChildItem -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall' -ErrorAction SilentlyContinue | Get-ItemProperty | Where-Object { $_.PSChildName -eq '696e88de-db89-5bb4-91b5-b72e17354c33' })
$installs = @($records | ForEach-Object {
  $location = $_.InstallLocation
  if (-not $location) {
    $icon = ([string]$_.DisplayIcon -replace ',\s*-?\d+$','').Trim('"')
    if ([IO.Path]::IsPathRooted($icon) -and [IO.Path]::GetFileName($icon) -eq $exeName) { $location = [IO.Path]::GetDirectoryName($icon) }
  }
  if (-not $location -or -not [IO.Path]::IsPathRooted($location)) { throw 'Cannot resolve validated installation directory' }
  [pscustomobject]@{version=$_.DisplayVersion;directory=$location;files=@(foreach($name in @($exeName,'resources/app.asar','resources/fallback.html')) {
    $file=Join-Path $location $name
    if(Test-Path -LiteralPath $file -PathType Leaf){[pscustomobject]@{name=$name;bytes=(Get-Item -LiteralPath $file).Length;sha256=(Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash}}
  })}
})
$processes = @(Get-Process -Name 'PowerSystem Desktop' -ErrorAction SilentlyContinue | ForEach-Object {[pscustomobject]@{id=$_.Id;path=$_.Path;window=($_.MainWindowHandle -ne 0)}})
$data = Join-Path $env:APPDATA 'PowerSystem Desktop'
$prefs = $null; $window = $null
if(Test-Path -LiteralPath (Join-Path $data 'preferences.json')) {
  $p=Get-Content -LiteralPath (Join-Path $data 'preferences.json') -Raw | ConvertFrom-Json
  $prefs=[pscustomobject]@{notificationsEnabled=$p.notificationsEnabled;launchAtLogin=$p.launchAtLogin;trayHintShown=$p.trayHintShown;keys=@($p.PSObject.Properties.Name)}
}
if(Test-Path -LiteralPath (Join-Path $data 'window-state.json')) {
  $w=Get-Content -LiteralPath (Join-Path $data 'window-state.json') -Raw | ConvertFrom-Json
  $window=[pscustomobject]@{bounds=$w.bounds;maximized=$w.maximized}
}
$audit=@();$log=Join-Path $data 'security-events.jsonl'
if(Test-Path -LiteralPath $log){$audit=@(Get-Content -LiteralPath $log -Tail 80 | ForEach-Object {try {$v=$_|ConvertFrom-Json;if($v.code -match '^[a-z]+(?:-[a-z0-9]+)+$'){[pscustomobject]@{at=$v.at;code=$v.code}}}catch{}})}
$os=Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version,BuildNumber,OSArchitecture
$files=@(if(Test-Path -LiteralPath $data){Get-ChildItem -LiteralPath $data -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object {[pscustomobject]@{name=$_.FullName.Substring($data.Length+1);bytes=$_.Length}}})
$setup=Join-Path (Split-Path $outputRoot -Parent) 'PowerSystem-Desktop-0.2.0-x64-Setup.exe'
$result=[ordered]@{at=[DateTime]::UtcNow.ToString('o');machine=$Machine;node=$Node;os=$os;installs=$installs;processes=$processes;preferences=$prefs;window=$window;audit=$audit;userDataFiles=$files;setupSHA256=$(if(Test-Path -LiteralPath $setup){(Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash}else{$null});limit='Read-only. No credentials or raw browser storage. Snapshot does not prove continuous exit, OS notification display, successful login, or file save.'}
$target=Join-Path $outputRoot ('evidence-'+$Machine+'-'+[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')+'.json')
$result | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $target -Encoding utf8
Write-Output $target
