param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$stageRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$repoRoot = (Resolve-Path (Join-Path $stageRoot '..\..')).Path
$output = Join-Path $repoRoot 'artifacts\stage3'
$model = Join-Path $output 'model'
$packages = Join-Path $output 'pydeps'
if (-not (Test-Path (Join-Path $model 'model.json'))) { throw '先执行 build-model.ps1' }
if (-not (Test-Path $packages)) { throw '缺少 Python 依赖目录，先执行 build-model.ps1' }
$env:PYTHONPATH = "$packages;$stageRoot"
$env:STAGE3_MODEL_DIR = $model
$logs = Join-Path $output 'runtime'
New-Item -ItemType Directory -Path $logs -Force | Out-Null
$pidFile = Join-Path $logs 'ai-process.json'
if (Test-Path $pidFile) {
    $old = Get-Content -Raw $pidFile | ConvertFrom-Json
    $oldProcess = Get-Process -Id $old.pid -ErrorAction SilentlyContinue
    if ($oldProcess -and $old.started_at) {
        $started = ([DateTime]$old.started_at).ToUniversalTime()
        if ([math]::Abs(($oldProcess.StartTime.ToUniversalTime() - $started).TotalSeconds) -lt 60) {
            throw 'AI 服务已在运行'
        }
    }
}
$process = Start-Process -FilePath $Python -ArgumentList @('-m','uvicorn','prediction.app:app','--host','127.0.0.1','--port','8090') -WorkingDirectory $stageRoot -PassThru -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logs 'ai.log') -RedirectStandardError (Join-Path $logs 'ai.stderr.log')
@{pid=$process.Id;started_at=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath $pidFile -Encoding utf8
for ($i=0; $i -lt 20; $i++) {
    if ($process.HasExited) { throw "AI 服务提前退出，查看 $logs" }
    try {
        $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8090/health' -TimeoutSec 2
        if ($health.status -eq 'ok') {
            Write-Host "AI 服务就绪；模型版本 $($health.model_version)"
            return
        }
    } catch { Start-Sleep -Milliseconds 500 }
}
throw "AI 服务健康检查失败，查看 $logs"
