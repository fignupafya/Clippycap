# Build Clippycap's distributables.  Run:  powershell -ExecutionPolicy Bypass -File build.ps1
#
# This script IS the recipe -- run it and you get the same .exe files that ship in a release, so you
# never have to trust a prebuilt binary. When it finishes, THIS folder (the repo root) holds:
#
#   Clippycap-Portable.exe   -- one self-contained .exe; nothing to install, just run it
#   Clippycap-Setup.exe      -- the Windows installer  (only if Inno Setup 6 is installed)
#
# ...and nothing else extra -- the temporary build directories (build\, dist\) are removed afterwards.
#
# Prerequisites:
#   - Python 3.13 with the project + its deps installed (a .venv at the repo root is used if present,
#     else the `python` on PATH).  [The desktop window uses pywebview, whose pythonnet dep has no
#     Python 3.14 wheels yet -- on 3.14 the app still works but falls back to a Chrome/Edge --app window.]
#   - Node.js + npm.
#   - Optional: Inno Setup 6 (https://jrsoftware.org/isdl.php) to also build the installer. When it's
#     present, this script first fetches a standalone ffmpeg/ffprobe into bin\ (once) so the installer
#     bundles them; the *portable* .exe never bundles ffmpeg -- it downloads it on demand. Building
#     the installer therefore needs internet (once); building only the portable .exe doesn't.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot          # this script lives at the repo root

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
Invoke-Step "ensuring PyInstaller + pywebview are installed" {
    & $py -m pip install -q "pyinstaller>=6.0" "pywebview>=5.1"
}
Invoke-Step "bundling with PyInstaller (portable .exe + one-folder build)" {
    & $py -m PyInstaller --noconfirm packaging\clippycap.spec
}

# installer (Inno Setup) -- optional; skipped with a warning if iscc.exe isn't found.
$iscc = $null
$cmd = Get-Command iscc.exe -ErrorAction SilentlyContinue
if ($cmd) { $iscc = $cmd.Source }
if (-not $iscc) {
    foreach ($p in @(
        "$env:LocalAppData\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )) {
        if (Test-Path $p) { $iscc = $p; break }
    }
}
if ($iscc) {
    Invoke-Step "ensuring a standalone ffmpeg is in bin\ (the installer bundles it)" {
        & "$PSScriptRoot\packaging\get_ffmpeg.ps1"
        Set-Location $PSScriptRoot   # get_ffmpeg.ps1 changes the location; put it back
    }
    Invoke-Step "building the Windows installer (Inno Setup)" {
        & $iscc /Qp packaging\installer.iss
    }
} else {
    Write-Host ""
    Write-Warning "Inno Setup not found (looked for iscc.exe on PATH and under Program Files) -- skipping Clippycap-Setup.exe."
    Write-Warning "Install Inno Setup 6 from https://jrsoftware.org/isdl.php and re-run this script to build the installer too."
}

# collect the produced .exe files into the repo root, then drop the temporary build directories.
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
