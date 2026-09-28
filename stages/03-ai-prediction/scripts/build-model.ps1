param(
    [string]$Python = 'python',
    [int]$Days = 21,
    [switch]$SkipInstall
)
$ErrorActionPreference = 'Stop'
$stageRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$repoRoot = (Resolve-Path (Join-Path $stageRoot '..\..')).Path
$output = Join-Path $repoRoot 'artifacts\stage3'
$packages = Join-Path $output 'pydeps'
New-Item -ItemType Directory -Path $packages -Force | Out-Null
if (-not $SkipInstall) {
    & $Python -m pip install --disable-pip-version-check --upgrade --target $packages -r (Join-Path $stageRoot 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw '阶段三依赖安装失败' }
}
$env:PYTHONPATH = "$packages;$stageRoot"
Push-Location $stageRoot
try {
    & $Python -m prediction.generate --out (Join-Path $output 'synthetic') --days $Days
    if ($LASTEXITCODE -ne 0) { throw '合成数据生成失败' }
    & $Python -m prediction.train --data (Join-Path $output 'synthetic') --out (Join-Path $output 'model')
    if ($LASTEXITCODE -ne 0) { throw '模型训练失败' }
} finally {
    Pop-Location
}
Write-Host "阶段三模型与评估报告：$(Join-Path $output 'model')"
