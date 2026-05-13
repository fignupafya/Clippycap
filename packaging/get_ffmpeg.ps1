# Fetch a standalone (statically-linked) ffmpeg + ffprobe into <repo>\bin\.
#
# Used by build.ps1 so the installer (Clippycap-Setup.exe) can bundle ffmpeg, and handy for local
# development (the resolver checks <repo>\bin\ as the "@bundled" location). The PORTABLE build does
# NOT bundle ffmpeg -- it downloads this same build on demand into %APPDATA%\Clippycap\bin\.
#
# Run:  powershell -ExecutionPolicy Bypass -File packaging\get_ffmpeg.ps1   (force with -Force)
[CmdletBinding()] param([switch]$Force)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)          # -> repo root

# Already have a standalone build? (a static ffmpeg.exe is tens of MB; the "shared" build's stub is ~1 MB.)
if (-not $Force -and (Test-Path "bin\ffmpeg.exe") -and (Test-Path "bin\ffprobe.exe") -and
    (Get-Item "bin\ffmpeg.exe").Length -gt 30MB) {
    Write-Host "bin\ffmpeg.exe is already a standalone build -- skipping (use -Force to re-download)."
    exit 0
}

$url = "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip"
$zip = Join-Path $env:TEMP "clippycap-ffmpeg.zip"
$tmp = Join-Path $env:TEMP "clippycap-ffmpeg"

Write-Host "==> downloading ffmpeg (static win64 GPL build)"
$old = $ProgressPreference; $ProgressPreference = 'SilentlyContinue'
try { Invoke-WebRequest -Uri $url -OutFile $zip } finally { $ProgressPreference = $old }

Write-Host "==> extracting ffmpeg.exe + ffprobe.exe"
Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
Expand-Archive -Path $zip -DestinationPath $tmp
New-Item -ItemType Directory -Force -Path bin | Out-Null
Remove-Item -Force "bin\*.dll" -ErrorAction SilentlyContinue   # drop any leftover "shared"-build DLLs
Get-ChildItem -Path $tmp -Recurse -Include ffmpeg.exe, ffprobe.exe |
    ForEach-Object { Copy-Item $_.FullName -Destination (Join-Path "bin" $_.Name) -Force }
Remove-Item -Recurse -Force $tmp, $zip -ErrorAction SilentlyContinue

if (-not ((Test-Path "bin\ffmpeg.exe") -and (Test-Path "bin\ffprobe.exe"))) {
    throw "ffmpeg.exe / ffprobe.exe not found in the downloaded archive"
}
$mb = [math]::Round((Get-Item "bin\ffmpeg.exe").Length / 1MB, 0)
Write-Host "Done.  ->  bin\ffmpeg.exe (~$mb MB)  bin\ffprobe.exe   (BtbN static GPL build -- standalone, no DLLs)"
