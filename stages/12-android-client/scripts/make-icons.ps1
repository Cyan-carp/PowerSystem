# 生成安卓启动图标 PNG（mdpi~xxxhdpi）。
# 图形与网页 favicon 同源：品牌蓝圆角方块 + 白色晶体菱形 + 半透明下折线。
# 只在 Windows PowerShell 5.1 下使用 System.Drawing 绘制，不引入图片依赖。

param(
    [string]$ResDir = (Join-Path (Split-Path -Parent $PSScriptRoot) 'app\res')
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

# 一个 32 单位的原始画布缩放到目标像素
function New-Icon {
    param([int]$Size, [string]$Path)

    $s = $Size / 32.0
    $bmp = New-Object System.Drawing.Bitmap $Size, $Size
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.Clear([System.Drawing.Color]::Transparent)

    # 圆角方块：2 单位外边距，圆角半径 7 单位
    $inset = 2 * $s
    $radius = 7 * $s
    $w = $Size - 2 * $inset
    $d = 2 * $radius
    $shape = New-Object System.Drawing.Drawing2D.GraphicsPath
    $shape.AddArc($inset, $inset, $d, $d, 180, 90)
    $shape.AddArc($inset + $w - $d, $inset, $d, $d, 270, 90)
    $shape.AddArc($inset + $w - $d, $inset + $w - $d, $d, $d, 0, 90)
    $shape.AddArc($inset, $inset + $w - $d, $d, $d, 90, 90)
    $shape.CloseFigure()

    $bgBrush = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(255, 37, 99, 235))
    $g.FillPath($bgBrush, $shape)

    # 晶体菱形（顶面）
    $diamond = @(
        (New-Object System.Drawing.PointF (16 * $s), (7 * $s)),
        (New-Object System.Drawing.PointF (26 * $s), (12.2 * $s)),
        (New-Object System.Drawing.PointF (16 * $s), (17.4 * $s)),
        (New-Object System.Drawing.PointF (6 * $s), (12.2 * $s))
    )
    $g.FillPolygon([System.Drawing.Brushes]::White, $diamond)

    # 下折线（表示云边分层）
    $chevron = @(
        (New-Object System.Drawing.PointF (6 * $s), (17.6 * $s)),
        (New-Object System.Drawing.PointF (16 * $s), (22.8 * $s)),
        (New-Object System.Drawing.PointF (26 * $s), (17.6 * $s))
    )
    $pen = New-Object System.Drawing.Pen ([System.Drawing.Color]::FromArgb(166, 255, 255, 255)), (2 * $s)
    $pen.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
    $pen.EndCap = [System.Drawing.Drawing2D.LineCap]::Round
    $pen.LineJoin = [System.Drawing.Drawing2D.LineJoin]::Round
    $g.DrawLines($pen, $chevron)

    $g.Dispose()
    $bmp.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)
    $bmp.Dispose()
    $pen.Dispose()
    $bgBrush.Dispose()
    $shape.Dispose()
}

$density = [ordered]@{
    'mipmap-mdpi'    = 48
    'mipmap-hdpi'    = 72
    'mipmap-xhdpi'   = 96
    'mipmap-xxhdpi'  = 144
    'mipmap-xxxhdpi' = 192
}

foreach ($name in $density.Keys) {
    $dir = Join-Path $ResDir $name
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    $target = Join-Path $dir 'ic_launcher.png'
    New-Icon -Size $density[$name] -Path $target
    Write-Host ("  图标 {0}  {1}px" -f $name, $density[$name])
}
