. (Join-Path $PSScriptRoot 'stage7-common.ps1')
$env:PYTHONPATH=(Join-Path $Stage7Repo 'artifacts/stage7/pydeps')+';'+$Stage7Root
Push-Location $Stage7Repo
try {
    & $Stage7Python -m unittest discover -s stages/07-agent/tests -v
    if ($LASTEXITCODE -ne 0) { throw 'Offline tests failed.' }
} finally { Pop-Location }
