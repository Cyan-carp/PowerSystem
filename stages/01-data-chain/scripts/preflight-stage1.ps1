param([switch]$SkipBuild)
. (Join-Path $PSScriptRoot 'stage1-common.ps1')

Push-Location $ProjectRoot
try {
    $docker = Get-DockerExe
    $serverVersion = & $docker info --format '{{.ServerVersion}}' 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'Docker 引擎未运行。请先打开 Docker Desktop，等显示 Engine running 后重试。' }
    Write-Host "Docker Engine: $serverVersion"
    $secret = Get-LocalSecret
    & $docker @ComposeArgs config --quiet
    if ($LASTEXITCODE -ne 0) { throw 'compose.yaml 校验失败。' }
    & $docker @ComposeArgs up -d
    if ($LASTEXITCODE -ne 0) { throw 'EMQX / TDengine 容器启动失败。' }

    $deadline = (Get-Date).AddMinutes(2)
    $authenticated = $false
    while ((Get-Date) -lt $deadline) {
        try {
            $null = Invoke-TDSql -Sql 'SELECT SERVER_VERSION()' -Password $secret
            $authenticated = $true
            break
        } catch {
            Start-Sleep -Seconds 3
        }
    }
    if (-not $authenticated) {
        try {
            $null = Invoke-TDSql -Sql 'SELECT SERVER_VERSION()' -Password 'taosdata'
            $null = Invoke-TDSql -Sql "ALTER USER root PASS '$secret'" -Password 'taosdata'
            $null = Invoke-TDSql -Sql 'SELECT SERVER_VERSION()' -Password $secret
            $authenticated = $true
        } catch {
            throw 'TDengine 已启动，但 .env 密码无法登录；默认密码也不可用。请检查容器日志和已有数据卷。'
        }
    }
    $tcp = [Net.Sockets.TcpClient]::new()
    try {
        $connected = $tcp.ConnectAsync('127.0.0.1', 1883).Wait(30000)
        if (-not $connected) { throw 'EMQX MQTT 1883 端口未就绪。' }
    } finally { $tcp.Dispose() }
    Write-Host 'EMQX MQTT 与 TDengine REST 已就绪。'

    $venvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython)) {
        $python = Get-Command python -ErrorAction SilentlyContinue
        if (-not $python) { throw '未找到 Python。请安装 Python 3.12 或更新版本。' }
        & $python.Source -m venv (Join-Path $ProjectRoot '.venv')
        if ($LASTEXITCODE -ne 0) { throw '创建 Python 虚拟环境失败。' }
    }
    & $venvPython -c 'import paho.mqtt.client'
    if ($LASTEXITCODE -ne 0) {
        & $venvPython -m pip install -r (Join-Path $StageRoot 'simulator\requirements.txt')
        if ($LASTEXITCODE -ne 0) { throw '安装 paho-mqtt 失败。' }
    }
    if (-not $SkipBuild) {
        $env:GOCACHE = Join-Path $ProjectRoot 'artifacts\go-cache'
        $env:GOMODCACHE = Join-Path $ProjectRoot 'artifacts\go-mod'
        $binaryDir = Join-Path $ProjectRoot 'artifacts\bin'
        New-Item -ItemType Directory -Path $binaryDir -Force | Out-Null
        Push-Location (Join-Path $StageRoot 'gateway')
        try {
            & go build -o (Join-Path $binaryDir 'gateway.exe') ./cmd/gateway
            if ($LASTEXITCODE -ne 0) { throw 'Go 网关编译失败。' }
        } finally { Pop-Location }
    }
    Write-Host '预检通过。'
} finally { Pop-Location }
