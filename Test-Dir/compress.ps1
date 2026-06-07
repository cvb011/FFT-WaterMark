param(
    [Parameter(Mandatory=$true)]
    [string]$InputPath,

    [Parameter(Mandatory=$true)]
    [int]$N,

    [int]$Q = 12,

    [string]$OutDir = ""
)

if ($N -lt 1) {
    Write-Error "N 必须大于等于 1"
    exit 1
}

# 检查 ffmpeg 是否可用
$ffmpegCheck = Get-Command ffmpeg -ErrorAction SilentlyContinue
if (-not $ffmpegCheck) {
    Write-Error "未找到 ffmpeg，请确认 ffmpeg 已加入系统 PATH。"
    exit 1
}

# 检查输入文件
if (-not (Test-Path -LiteralPath $InputPath)) {
    Write-Error "输入文件不存在：$InputPath"
    exit 1
}

$inputFile = Get-Item -LiteralPath $InputPath
$baseName = [System.IO.Path]::GetFileNameWithoutExtension($inputFile.Name)
$inputDir = $inputFile.DirectoryName

if ($OutDir -eq "") {
    $OutDir = Join-Path $inputDir ($baseName + "_ffmpeg_jpeg_repeat")
}

if (-not (Test-Path -LiteralPath $OutDir)) {
    New-Item -ItemType Directory -Path $OutDir | Out-Null
}

$current = $inputFile.FullName

for ($i = 1; $i -le $N; $i++) {
    $idx = "{0:D3}" -f $i
    $output = Join-Path $OutDir ("{0}_q{1}_gen_{2}.jpg" -f $baseName, $Q, $idx)

    Write-Host "第 $i 次编码 -> $output"

    & ffmpeg `
        -hide_banner `
        -loglevel error `
        -y `
        -i "$current" `
        -frames:v 1 `
        -c:v mjpeg `
        "-q:v" $Q `
        -pix_fmt yuvj420p `
        "$output"

    if ($LASTEXITCODE -ne 0) {
        Write-Error "第 $i 次编码失败"
        exit 1
    }

    $current = $output
}

Write-Host ""
Write-Host "完成！"
Write-Host "连续 JPEG 编码次数：$N"
Write-Host "FFmpeg q:v：$Q"
Write-Host "最终文件：$current"
