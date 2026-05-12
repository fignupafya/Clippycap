# Build Clippycap's distributables.  Run:  powershell -ExecutionPolicy Bypass -File packaging\build.ps1
#
# This script IS the recipe -- anyone can run it to reproduce the released .exe files from source
# (so there's no need to trust a prebuilt binary). When it finishes, the *repo root* holds:
#
#   Clippycap-Portable.exe   -- a single self-contained .exe; nothing to install, just run it
#   Clippycap-Setup.exe      -- the Windows installer  (only if Inno Setup 6 is installed)
#
# and it removes its own temporary build directories (build\, dist\) afterwards.
#
# Prerequisites: Python 3.12+ with the project + its deps installed (a .venv at the repo root is used
# if present, else the `python` on PATH); Node.js + npm; and, optionally, Inno Setup 6 for the installer.
# ffmpeg is NOT bundled -- the app downloads it on demand -- so you don't need it to build.

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)          # -> repo root

function Invoke-Step([string]$Name, [scriptblock]$Body) {
    Write-Host ""
    Write-Host "==> $Name" -ForegroundColor Cyan
    & $Body
    if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw "step failed ($Name): exit code $LASTEXITCODE" }
}

$py = ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

Invoke-Step "building the web UI (npm run build)" {
    npm --prefix web run build
}
Invoke-Step "ensuring PyInstaller is installed" {
    & $py -m pip install -q "pyinstaller>=6.0"
}
Invoke-Step "bundling with PyInstaller (portable .exe + one-folder build)" {
    & $py -m PyInstaller --noconfirm packaging\clippycap.spec
}

# installer (Inno Setup) -- optional; skipped with a warning if iscc.exe isn't found.
$iscc = $null
$cmd = Get-Command iscc.exe -ErrorAction SilentlyContinue
if ($cmd) { $iscc = $cmd.Source }
if (-not $iscc) {
    foreach ($p in @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe")) {
        if (Test-Path $p) { $iscc = $p; break }
    }
}
if ($iscc) {
    Invoke-Step "building the Windows installer (Inno Setup)" {
        & $iscc /Qp packaging\installer.iss
    }
} else {
    Write-Host ""
    Write-Warning "Inno Setup not found (looked for iscc.exe on PATH and under Program Files) -- skipping Clippycap-Setup.exe."
    Write-Warning "Install Inno Setup 6 from https://jrsoftware.org/isdl.php and re-run this script to build the installer too."
}

# move the produced .exe files into the repo root, then remove the temporary build dirs.
$produced = @()
foreach ($n in @("Clippycap-Portable.exe", "Clippycap-Setup.exe")) {
    if (Test-Path "dist\$n") {
        Move-Item -Force "dist\$n" "."
        $produced += $n
    }
}
foreach ($d in @("dist", "build")) {
    if (Test-Path $d) { Remove-Item -Recurse -Force $d }
}

if ($produced.Count -eq 0) { throw "no .exe files were produced -- see the PyInstaller output above" }

Write-Host ""
Write-Host "Done. In the repo root:" -ForegroundColor Green
foreach ($n in $produced) {
    $mb = [math]::Round((Get-Item $n).Length / 1MB, 1)
    Write-Host ("  {0,-24} {1} MB" -f $n, $mb)
}
if (-not (Test-Path "Clippycap-Setup.exe")) {
    Write-Host "  (Clippycap-Setup.exe: install Inno Setup 6, then re-run this script to build it too)" -ForegroundColor Yellow
}
