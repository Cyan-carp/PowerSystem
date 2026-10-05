param([switch]$ProbeModel, [string]$ConfigFile)
. (Join-Path $PSScriptRoot 'stage7-common.ps1')
Push-Location $Stage7Repo
try {
    if ($ConfigFile) { Set-Stage7Environment -ConfigFile $ConfigFile } else { Set-Stage7Environment }
    & $Stage7Python -m agent.cli validate
    if ($LASTEXITCODE -ne 0) { throw 'Agent configuration invalid.' }
    foreach ($port in @(8080,6379)) {
        $client = New-Object Net.Sockets.TcpClient
        try { $client.Connect('127.0.0.1',$port) } finally { $client.Dispose() }
    }
    if ($ProbeModel) {
        & $Stage7Python -m agent.cli probe
        if ($LASTEXITCODE -ne 0) { throw 'Model capability preflight failed.' }
    }
} finally { Pop-Location }
