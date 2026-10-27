param(
  [Parameter(Mandatory=$true)]
  [ValidateSet('installed','running','closed','uninstalled')][string]$Phase,
  [string]$RunDirectory = (Join-Path $PSScriptRoot '../../../artifacts/v5-desktop/s02-20261026-r1'),
  [switch]$Isolated
)
$ErrorActionPreference = 'Stop'
$product = if ($Isolated) { '源网智联 S02 隔离验收' } else { '源网智联' }
$executable = if ($Isolated) { 'PowerSystem S02 Test' } else { 'PowerSystem Desktop' }
$dataName = if ($Isolated) { 'PowerSystem Desktop S02 Test' } else { 'PowerSystem Desktop' }
$runPath = [IO.Path]::GetFullPath($RunDirectory)
New-Item -ItemType Directory -Path $runPath -Force | Out-Null
$registrations = @(Get-ChildItem -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall' -ErrorAction SilentlyContinue |
  Get-ItemProperty | Where-Object { $_.DisplayName -eq $product } |
  ForEach-Object {
    $location = $_.InstallLocation
    if (-not $location) {
      $iconPath = ([string]$_.DisplayIcon -replace ',\s*-?\d+$','').Trim('"')
      if ([IO.Path]::IsPathRooted($iconPath) -and [IO.Path]::GetFileName($iconPath) -eq ($executable+'.exe')) { $location = [IO.Path]::GetDirectoryName($iconPath) }
    }
    [pscustomobject]@{ RegistryKeyName=$_.PSChildName; RegistryKeyPath=$_.PSPath; DisplayName=$_.DisplayName; DisplayVersion=$_.DisplayVersion; InstallLocation=$location }
  })
$machineRegistrations = @(Get-ChildItem -LiteralPath 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall' -ErrorAction SilentlyContinue |
  Get-ItemProperty | Where-Object { $_.DisplayName -eq $product } | Select-Object DisplayName,DisplayVersion)
$processes = @(Get-Process -Name $executable -ErrorAction SilentlyContinue | ForEach-Object {
  [pscustomobject]@{ id=$_.Id; path=$_.Path; hasWindow=($_.MainWindowHandle -ne 0) }
})
$links = @(
  (Join-Path ([Environment]::GetFolderPath('Desktop')) ($product+'.lnk')),
  (Join-Path $env:APPDATA ('Microsoft/Windows/Start Menu/Programs/'+$product+'.lnk'))
) | ForEach-Object { [pscustomobject]@{ path=$_; exists=(Test-Path -LiteralPath $_) } }
$userData = Join-Path $env:APPDATA $dataName
$files = @()
$jwtCandidates = 0
if (Test-Path -LiteralPath $userData) {
  $files = @(Get-ChildItem -LiteralPath $userData -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object {
    # Do not emit file contents, tokens, browser databases or raw logs.
    if ($_.Length -lt 33554432) {
      try {
        $bytes = [IO.File]::ReadAllBytes($_.FullName)
        foreach ($encoding in @([Text.Encoding]::UTF8,[Text.Encoding]::Unicode)) {
          if ($encoding.GetString($bytes) -match 'eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}') { $jwtCandidates++; break }
        }
      } catch { }
    }
    [pscustomobject]@{ path=$_.FullName.Substring($userData.Length+1); bytes=$_.Length }
  })
}
$state = $null
$stateFile = Join-Path $userData 'window-state.json'
if (Test-Path -LiteralPath $stateFile) {
  $stored = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
  $state = [pscustomobject]@{ bounds=$stored.bounds; maximized=($stored.maximized -eq $true) }
}
$audit = @()
$auditFile = Join-Path $userData 'security-events.jsonl'
if (Test-Path -LiteralPath $auditFile) {
  $audit = @(Get-Content -LiteralPath $auditFile -Tail 100 | ForEach-Object {
    try {
      $record = $_ | ConvertFrom-Json
      if ($record.code -match '^(ipc-denied|window-save-failed|startup-load-failed|fallback-load-failed|startup-loaded|second-instance-focused|certificate-denied|download-unavailable-s01|popup-denied|navigation-denied|redirect-denied|external-open-failed|external-click-denied|startup-fatal|download-saved|download-cancelled|download-failed|download-denied|download-partial-cleanup-failed|notification-session-reset|notification-submitted|notification-failed|notification-clicked|window-hidden|connection-resume|preference-rollback-failed|preference-sync-failed|preference-save-failed)$') {
        [pscustomobject]@{ at=$record.at; code=$record.code }
      }
    } catch { }
  })
}
$startups = @()
$runKey = Get-ItemProperty -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -ErrorAction SilentlyContinue
if ($runKey) { $startups = @($runKey.PSObject.Properties | Where-Object { $_.Name -notlike 'PS*' -and ($_.Name -match 'PowerSystem|源网智联' -or [string]$_.Value -match 'PowerSystem Desktop') } | ForEach-Object { $_.Name }) }
$setup = Join-Path $runPath $(if ($Isolated) { 'isolated-package/PowerSystem-S02-Isolated-0.2.0-x64-Setup.exe' } else { 'package/PowerSystem-Desktop-0.2.0-x64-Setup.exe' })
$setupHash = if (Test-Path -LiteralPath $setup) { (Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash } else { $null }
$installedFiles = @($registrations | ForEach-Object {
  if ($_.InstallLocation) {
    foreach ($relative in @(($executable+'.exe'),'resources/app.asar','resources/fallback.html')) {
      $target = Join-Path $_.InstallLocation $relative
      if (Test-Path -LiteralPath $target -PathType Leaf) {
        [pscustomobject]@{ path=$target; bytes=(Get-Item -LiteralPath $target).Length; sha256=(Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash }
      }
    }
  }
})
$result = [ordered]@{
  candidateKind=$(if($Isolated){'isolated'}else{'team'});
  phase=$Phase; capturedAt=(Get-Date).ToUniversalTime().ToString('o');
  os=[Environment]::OSVersion.VersionString; is64BitOS=[Environment]::Is64BitOperatingSystem;
  setupSHA256=$setupHash; installedFiles=$installedFiles; currentUserRegistrations=$registrations; machineRegistrations=$machineRegistrations;
  processes=$processes; shortcuts=$links; matchingStartupNames=$startups;
  userDataPath=$userData; windowState=$state; userDataFiles=$files;
  jwtCandidateFiles=$jwtCandidates; audit=$audit;
  preferences=$(if(Test-Path -LiteralPath (Join-Path $userData 'preferences.json')) { $prefs=Get-Content -LiteralPath (Join-Path $userData 'preferences.json') -Raw | ConvertFrom-Json; [pscustomobject]@{notificationsEnabled=($prefs.notificationsEnabled -eq $true);launchAtLogin=($prefs.launchAtLogin -eq $true);trayHintShown=($prefs.trayHintShown -eq $true);keys=@($prefs.PSObject.Properties.Name)} } else {$null});
  limit='Read-only snapshot. Pattern count is supplementary, not proof of absent credentials. No secrets or raw browser storage emitted.'
}
$output = Join-Path $runPath ('owner-evidence-'+$(if($Isolated){'isolated-'}else{'team-'})+$Phase+'-'+(Get-Date -Format 'yyyyMMdd-HHmmss')+'.json')
$result | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $output -Encoding utf8
Write-Output $output

