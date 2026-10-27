param(
  [Parameter(Mandatory=$true)]
  [ValidateSet('installed','running','closed','uninstalled')][string]$Phase,
  [string]$RunDirectory = (Join-Path $PSScriptRoot '../../../artifacts/v5-desktop/s01-20261026-r1')
)
$ErrorActionPreference = 'Stop'
$runPath = [IO.Path]::GetFullPath($RunDirectory)
New-Item -ItemType Directory -Path $runPath -Force | Out-Null
$registrations = @(Get-ChildItem -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall' -ErrorAction SilentlyContinue |
  Get-ItemProperty | Where-Object { $_.DisplayName -eq '源网智联' } |
  ForEach-Object {
    $location = $_.InstallLocation
    if (-not $location) {
      $iconPath = ([string]$_.DisplayIcon -replace ',\s*-?\d+$','').Trim('"')
      if ([IO.Path]::IsPathRooted($iconPath) -and [IO.Path]::GetFileName($iconPath) -eq 'PowerSystem Desktop.exe') { $location = [IO.Path]::GetDirectoryName($iconPath) }
    }
    [pscustomobject]@{ DisplayName=$_.DisplayName; DisplayVersion=$_.DisplayVersion; InstallLocation=$location }
  })
$machineRegistrations = @(Get-ChildItem -LiteralPath 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall' -ErrorAction SilentlyContinue |
  Get-ItemProperty | Where-Object { $_.DisplayName -eq '源网智联' } | Select-Object DisplayName,DisplayVersion)
$processes = @(Get-Process -Name 'PowerSystem Desktop' -ErrorAction SilentlyContinue | ForEach-Object {
  [pscustomobject]@{ id=$_.Id; path=$_.Path; hasWindow=($_.MainWindowHandle -ne 0) }
})
$links = @(
  (Join-Path ([Environment]::GetFolderPath('Desktop')) '源网智联.lnk'),
  (Join-Path $env:APPDATA 'Microsoft/Windows/Start Menu/Programs/源网智联.lnk')
) | ForEach-Object { [pscustomobject]@{ path=$_; exists=(Test-Path -LiteralPath $_) } }
$userData = Join-Path $env:APPDATA 'PowerSystem Desktop'
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
      if ($record.code -match '^(ipc-denied|window-save-failed|startup-load-failed|fallback-load-failed|startup-loaded|second-instance-focused|certificate-denied|download-unavailable-s01|popup-denied|navigation-denied|redirect-denied|external-open-failed|external-click-denied|startup-fatal)$') {
        [pscustomobject]@{ at=$record.at; code=$record.code }
      }
    } catch { }
  })
}
$startups = @()
$runKey = Get-ItemProperty -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -ErrorAction SilentlyContinue
if ($runKey) { $startups = @($runKey.PSObject.Properties | Where-Object { $_.Name -notlike 'PS*' -and ($_.Name -match 'PowerSystem|源网智联' -or [string]$_.Value -match 'PowerSystem Desktop') } | ForEach-Object { $_.Name }) }
$setup = Join-Path $runPath 'package/PowerSystem-Desktop-0.1.0-x64-Setup.exe'
$setupHash = if (Test-Path -LiteralPath $setup) { (Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash } else { $null }
$installedFiles = @($registrations | ForEach-Object {
  if ($_.InstallLocation) {
    foreach ($relative in @('PowerSystem Desktop.exe','resources/app.asar','resources/fallback.html')) {
      $target = Join-Path $_.InstallLocation $relative
      if (Test-Path -LiteralPath $target -PathType Leaf) {
        [pscustomobject]@{ path=$target; bytes=(Get-Item -LiteralPath $target).Length; sha256=(Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash }
      }
    }
  }
})
$result = [ordered]@{
  phase=$Phase; capturedAt=(Get-Date).ToUniversalTime().ToString('o');
  os=[Environment]::OSVersion.VersionString; is64BitOS=[Environment]::Is64BitOperatingSystem;
  setupSHA256=$setupHash; installedFiles=$installedFiles; currentUserRegistrations=$registrations; machineRegistrations=$machineRegistrations;
  processes=$processes; shortcuts=$links; matchingStartupNames=$startups;
  userDataPath=$userData; windowState=$state; userDataFiles=$files;
  jwtCandidateFiles=$jwtCandidates; audit=$audit;
  limit='Read-only snapshot. Pattern count is supplementary, not proof of absent credentials. No secrets or raw browser storage emitted.'
}
$output = Join-Path $runPath ('owner-evidence-'+$Phase+'-'+(Get-Date -Format 'yyyyMMdd-HHmmss')+'.json')
$result | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $output -Encoding utf8
Write-Output $output
