. (Join-Path $PSScriptRoot 'stage2-common.ps1')
Push-Location $ProjectRoot
try {
    & (Join-Path $ProjectRoot 'stages\01-data-chain\scripts\preflight-stage1.ps1')
    Ensure-Stage2Secrets
    Set-Stage2Environment
    $dockerExe = Get-DockerExe
    & $dockerExe @Stage2Compose config --quiet
    if ($LASTEXITCODE -ne 0) { throw '阶段二 Compose 配置校验失败。' }
    & $dockerExe @Stage2Compose up -d --wait
    if ($LASTEXITCODE -ne 0) { throw '阶段二 PostgreSQL / Redis 启动失败。' }
    $env:GOCACHE = Join-Path $ProjectRoot 'artifacts\go-cache'
    $env:GOMODCACHE = Join-Path $ProjectRoot 'artifacts\go-mod'
    $binaryDir = Join-Path $ProjectRoot 'artifacts\bin'
    New-Item -ItemType Directory -Path $binaryDir -Force | Out-Null
    Push-Location $Stage2Root
    try {
        & go build -o (Join-Path $binaryDir 'stage2-api.exe') ./cmd/api
        if ($LASTEXITCODE -ne 0) { throw '阶段二 API 编译失败。' }
        & go build -o (Join-Path $binaryDir 'stage2-gateway.exe') ./cmd/gateway
        if ($LASTEXITCODE -ne 0) { throw '阶段二网关编译失败。' }
        & (Join-Path $binaryDir 'stage2-api.exe') -migrate-only
        if ($LASTEXITCODE -ne 0) { throw '阶段二数据库迁移失败。' }
    } finally { Pop-Location }
    Write-Host '阶段二预检、迁移及三台演示设备登记完成。'
} finally { Pop-Location }
