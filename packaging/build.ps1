# Build Clippycap.exe.  Run:  powershell -ExecutionPolicy Bypass -File packaging\build.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)          # -> repo root

$py = ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

Write-Host "==> building the frontend (npm run build)"
npm --prefix web run build

Write-Host "==> ensuring PyInstaller is installed"
& $py -m pip install -q "pyinstaller>=6.0"

Write-Host "==> bundling with PyInstaller"
& $py -m PyInstaller --noconfirm packaging\clippycap.spec

Write-Host ""
Write-Host "Done.  ->  dist\Clippycap\Clippycap.exe   (the whole dist\Clippycap\ folder is the app)"
