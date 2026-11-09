# ============================================================================
# 直接用 Android SDK 工具链构建可安装 APK（不需要 Gradle，不联网拉依赖）
#
#   1. aapt2 compile/link  资源 + AndroidManifest  →  base.apk（无 dex）
#   2. javac               源码 → .class（-source 8）
#   3. d8                  .class → classes.dex（--min-api 24）
#   4. 打包 classes.dex → zipalign → apksigner 签名
#
# 用法（在仓库任意目录）：
#   powershell -ExecutionPolicy Bypass -File stages/12-android-client/scripts/build-apk.ps1
#   powershell ... -VersionName 1.0.1 -VersionCode 2
#
# 产物：stages/12-android-client/build/out/PowerSystem-Android-<版本>.apk
# 签名：build/keys/powersystem-mobile.jks（构建目录已 git 忽略；丢失后无法覆盖安装）
# ============================================================================

[CmdletBinding()]
param(
    [string]$VersionName = '1.0.0',
    [int]$VersionCode = 1,
    [switch]$KeepWork
)

$ErrorActionPreference = 'Stop'

# Remove-Item 走 PowerShell provider，逐项枚举，慢；且在部分宿主里参数绑定会被改写
# （报「找不到接受实际参数 -Recurse 的位置形式参数」）。直接调 .NET 删目录，快且稳。
function Remove-Dir([string]$path) {
    if ($path -and (Test-Path -LiteralPath $path)) {
        [System.IO.Directory]::Delete($path, $true)
    }
}

$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$appSource = Join-Path $root 'app'
$buildDir = Join-Path $root 'build'
# 中间产物放在纯 ASCII 的临时目录：aapt2 / zipalign 是原生程序，
# 仓库路径带中文时它们打不开目录。最终 APK 仍写回仓库 build\out。
$work = Join-Path $env:TEMP 'powersystem-apk-build'
$appDir = Join-Path $work 'app'
$outDir = Join-Path $buildDir 'out'
$keysDir = Join-Path $buildDir 'keys'

function Write-Step([string]$text) { Write-Host "==> $text" -ForegroundColor Cyan }

function Resolve-Sdk {
    $candidates = @($env:ANDROID_HOME, $env:ANDROID_SDK_ROOT, (Join-Path $env:LOCALAPPDATA 'Android\Sdk'), 'C:\Android\Sdk')
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path (Join-Path $candidate 'build-tools'))) { return (Resolve-Path $candidate).Path }
    }
    throw '找不到 Android SDK：请设置 ANDROID_HOME，或确保 %LOCALAPPDATA%\Android\Sdk 存在（Android Studio 默认位置）'
}

function Resolve-Jdk {
    $candidates = @($env:JAVA_HOME, 'C:\Program Files\Android\Android Studio\jbr')
    $candidates += (Get-ChildItem 'C:\Program Files\Java' -Directory -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path (Join-Path $candidate 'bin\javac.exe'))) { return (Resolve-Path $candidate).Path }
    }
    throw '找不到 JDK：请设置 JAVA_HOME，或安装 Android Studio（自带 jbr）'
}

# ---------- 工具链定位 ----------

$sdk = Resolve-Sdk
$jdk = Resolve-Jdk

$buildTools = Get-ChildItem (Join-Path $sdk 'build-tools') -Directory |
    Sort-Object { [version]$_.Name } -Descending | Select-Object -First 1
if (-not $buildTools) { throw "SDK 下没有 build-tools：$sdk" }
$bt = $buildTools.FullName

$platform = Get-ChildItem (Join-Path $sdk 'platforms') -Directory |
    Sort-Object { [int]($_.Name -replace '[^0-9]', '') } -Descending | Select-Object -First 1
if (-not $platform) { throw "SDK 下没有 platforms：$sdk" }
$androidJar = Join-Path $platform.FullName 'android.jar'
if (-not (Test-Path $androidJar)) { throw "缺少 android.jar：$androidJar" }

$env:JAVA_HOME = $jdk
$env:PATH = "$(Join-Path $jdk 'bin');$env:PATH"

Write-Step "SDK        $sdk"
Write-Step "build-tools $($buildTools.Name)"
Write-Step "platform    $($platform.Name)"
Write-Step "JDK         $jdk"

# ---------- 工作目录 ----------

Remove-Dir $work
# 早期版本把中间产物放在仓库 build\tmp，换到 ASCII 临时目录后这份遗留没人清
Remove-Dir (Join-Path $buildDir 'tmp')
New-Item -ItemType Directory -Force -Path $work, $outDir, $keysDir | Out-Null

Write-Step "复制源码到临时目录 $work"
Copy-Item $appSource -Destination $appDir -Recurse -Force

# ---------- 图标 ----------

Write-Step '生成启动图标'
& (Join-Path $PSScriptRoot 'make-icons.ps1') -ResDir (Join-Path $appDir 'res')

# ---------- 1. 资源 ----------

Write-Step 'aapt2 compile / link'
$resZip = Join-Path $work 'res.zip'
& (Join-Path $bt 'aapt2.exe') compile --dir (Join-Path $appDir 'res') -o $resZip
if ($LASTEXITCODE -ne 0) { throw 'aapt2 compile 失败' }

$baseApk = Join-Path $work 'base.apk'
& (Join-Path $bt 'aapt2.exe') link `
    -o $baseApk `
    -I $androidJar `
    --manifest (Join-Path $appDir 'AndroidManifest.xml') `
    -R $resZip `
    --min-sdk-version 24 `
    --target-sdk-version 34 `
    --version-code $VersionCode `
    --version-name $VersionName `
    --auto-add-overlay
if ($LASTEXITCODE -ne 0) { throw 'aapt2 link 失败' }

# ---------- 2. 编译 ----------

Write-Step 'javac'
$classes = Join-Path $work 'classes'
New-Item -ItemType Directory -Force -Path $classes | Out-Null
$javaFiles = @(Get-ChildItem (Join-Path $appDir 'src') -Recurse -Filter *.java | ForEach-Object { $_.FullName })
if ($javaFiles.Count -eq 0) { throw '没有找到 Java 源码' }
& (Join-Path $jdk 'bin\javac.exe') -encoding UTF-8 -source 8 -target 8 -nowarn -Xlint:none -classpath $androidJar -d $classes @javaFiles
if ($LASTEXITCODE -ne 0) { throw 'javac 失败' }

# ---------- 3. dex ----------

Write-Step 'd8'
$dexDir = Join-Path $work 'dex'
New-Item -ItemType Directory -Force -Path $dexDir | Out-Null
$classFiles = @(Get-ChildItem $classes -Recurse -Filter *.class | ForEach-Object { $_.FullName })
& (Join-Path $bt 'd8.bat') --lib $androidJar --min-api 24 --output $dexDir @classFiles
if ($LASTEXITCODE -ne 0) { throw 'd8 失败' }

Write-Step '打包 classes.dex'
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::Open($baseApk, [System.IO.Compression.ZipArchiveMode]::Update)
try {
    [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
        $zip, (Join-Path $dexDir 'classes.dex'), 'classes.dex',
        [System.IO.Compression.CompressionLevel]::Optimal)
} finally {
    $zip.Dispose()
}

# ---------- 4. 对齐与签名 ----------

Write-Step 'zipalign'
$aligned = Join-Path $work 'aligned.apk'
& (Join-Path $bt 'zipalign.exe') -f -p 4 $baseApk $aligned
if ($LASTEXITCODE -ne 0) { throw 'zipalign 失败' }

$keystore = Join-Path $keysDir 'powersystem-mobile.jks'
$alias = 'powersystem'
# 本工程只做内部安装包；正式对外分发前请换成自己的密钥并妥善保管
$keyPass = 'powersystem'
if (-not (Test-Path $keystore)) {
    Write-Step '生成签名密钥（仅首次）'
    & (Join-Path $jdk 'bin\keytool.exe') -genkeypair -v `
        -keystore $keystore -alias $alias -keyalg RSA -keysize 2048 -validity 10000 `
        -storepass $keyPass -keypass $keyPass `
        -dname 'CN=PowerSystem Mobile, OU=Ops, O=PowerSystem, C=CN'
    if ($LASTEXITCODE -ne 0) { throw 'keytool 失败' }
}

Write-Step 'apksigner'
$final = Join-Path $outDir "PowerSystem-Android-$VersionName.apk"
if (Test-Path $final) { Remove-Item $final -Force }
& (Join-Path $bt 'apksigner.bat') sign `
    --ks $keystore --ks-key-alias $alias `
    --ks-pass "pass:$keyPass" --key-pass "pass:$keyPass" `
    --v4-signing-enabled false `
    --out $final $aligned
if ($LASTEXITCODE -ne 0) { throw 'apksigner 签名失败' }

& (Join-Path $bt 'apksigner.bat') verify --print-certs $final
if ($LASTEXITCODE -ne 0) { throw 'apksigner 验签失败' }

# 收尾统一清干净：临时目录、历史遗留目录、以及旧版 apksigner 留下的 v4 签名文件。
# 这样 build\out 里只会有 APK，不会再出现需要手工删的残留。
if (-not $KeepWork) { Remove-Dir $work }
Remove-Dir (Join-Path $buildDir 'tmp')
Get-ChildItem -LiteralPath $outDir -Filter '*.idsig' -File -ErrorAction SilentlyContinue |
    ForEach-Object { [System.IO.File]::Delete($_.FullName) }

$sizeMb = [math]::Round((Get-Item $final).Length / 1MB, 2)
$sha = (Get-FileHash $final -Algorithm SHA256).Hash
Write-Host ''
Write-Host "APK   $final" -ForegroundColor Green
Write-Host "大小  $sizeMb MB"
Write-Host "SHA256 $sha"
