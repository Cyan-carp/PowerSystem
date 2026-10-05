param([string]$ConfigFile, [switch]$Fixture)
. (Join-Path $PSScriptRoot 'stage7-common.ps1')
Push-Location $Stage7Repo
$record=$null
$dir=$null
try {
    if ($ConfigFile) { Set-Stage7Environment -ConfigFile $ConfigFile } else { Set-Stage7Environment }
    $run='stage7_'+(Get-Date -Format 'yyyyMMdd_HHmmss')+'_'+([Guid]::NewGuid().ToString('N').Substring(0,4))
    $dir=Join-Path $Stage7Repo "artifacts/stage7-智能体/$run"
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
    $record=@{run_id=$run;at=[DateTime]::UtcNow.ToString('o');fixture=[bool]$Fixture;offline_passed=$false;model_probe_passed=$false;integration_passed=$false;real_model_validated=$false;milestone_passed=$false;failed_stage='offline'}
    & $Stage7Python -m unittest discover -s stages/07-agent/tests -v *> (Join-Path $dir 'offline-tests.log')
    if ($LASTEXITCODE -ne 0) { throw "Offline tests failed; $dir" }
    $record.offline_passed=$true
    $record.failed_stage='model_probe'
    & $Stage7Python -m agent.cli probe *> (Join-Path $dir 'model-probe.log')
    if ($LASTEXITCODE -ne 0) { throw "Model capability preflight failed; $dir" }
    $record.model_probe_passed=$true
    $record.failed_stage='integration'
    $arguments=@((Join-Path $Stage7Root 'scripts/smoke-stage7.py'),'--output',$dir)
    if ($Fixture) { $arguments += '--fixture' }
    & $Stage7Python @arguments *> (Join-Path $dir 'integration.log')
    $passed=$LASTEXITCODE -eq 0
    $record.integration_passed=$passed
    $record.real_model_validated=($passed -and -not $Fixture)
    if (-not $passed) { throw "Integration failed; $dir" }
    $record.failed_stage=$null
    Write-Host "Tests recorded in $dir; milestone requires separate review of real-model and regression evidence."
} finally {
    if ($record -and $dir) { $record | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $dir 'run.json') -Encoding utf8 }
    Pop-Location
}
