# Fetch a static ffmpeg/ffprobe into bin/ -- handy for local development (the resolver checks
# <repo>/bin via the "@bundled" location). The packaged app does NOT bundle ffmpeg; it downloads
# this same build on demand into %APPDATA%\Clippycap\bin\ instead -- see packaging/README.md.
# Run:  powershell -ExecutionPolicy Bypass -File packaging\get_ffmpeg.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)          # -> repo root

$url = "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip"
$zip = Join-Path $env:TEMP "clippycap-ffmpeg.zip"
$tmp = Join-Path $env:TEMP "clippycap-ffmpeg"

Write-Host "==> downloading ffmpeg ($url)"
Invoke-WebRequest -Uri $url -OutFile $zip

Write-Host "==> extracting"
Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
Expand-Archive -Path $zip -DestinationPath $tmp
New-Item -ItemType Directory -Force -Path bin | Out-Null
Get-ChildItem -Path $tmp -Recurse -Include ffmpeg.exe, ffprobe.exe |
    ForEach-Object { Copy-Item $_.FullName -Destination (Join-Path bin $_.Name) -Force }
Remove-Item -Recurse -Force $tmp, $zip -ErrorAction SilentlyContinue

Write-Host "Done.  ->  bin\ffmpeg.exe  bin\ffprobe.exe   (BtbN's static GPL build -- no DLLs needed)"
