# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Clippycap. From one analysis it builds BOTH:
#   dist/Clippycap-Portable.exe   -- a single self-contained .exe (one-file; unpacks to a temp dir on
#                                    each launch). Nothing to install: download it and double-click.
#   dist/Clippycap/               -- a one-folder build (Clippycap.exe + _internal/). Faster startup;
#                                    this is what packaging/installer.iss wraps into Clippycap-Setup.exe.
#
# Build:  pyinstaller --noconfirm packaging/clippycap.spec   (or just run build.ps1 at the repo root,
# which builds the web UI first, runs this, then -- if Inno Setup is installed -- compiles the installer,
# and finally moves Clippycap-Portable.exe / Clippycap-Setup.exe to the repo root).
#
# Bundled data: config/default.toml (always) and web/dist/ (the built Svelte SPA, if present -- without
# it the backend serves a placeholder page).  ffmpeg is NOT bundled: it's large (~150 MB) and licence-
# encumbered, so the app downloads a static build on demand (Settings > FFmpeg / the first-run prompt),
# or the installer offers to. It always lands in %APPDATA%\Clippycap\bin\.
#
# The window/exe icon is packaging/clippycap.ico (picked up automatically below if present).

from pathlib import Path

ROOT = Path(SPECPATH).parent          # SPECPATH = the dir holding this .spec (packaging/)

datas = [(str(ROOT / "config" / "default.toml"), "config")]
if (ROOT / "web" / "dist").is_dir():
    datas.append((str(ROOT / "web" / "dist"), "web/dist"))

hiddenimports = [
    # uvicorn loads its loop / protocol implementations dynamically -- PyInstaller can't see those.
    "uvicorn.logging", "uvicorn.loops", "uvicorn.loops.auto",
    "uvicorn.protocols", "uvicorn.protocols.http", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets", "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan", "uvicorn.lifespan.on",
    # pywebview picks its window backend by name at runtime; we force the WebView2 one.
    "webview.platforms.edgechromium",
    # pythonnet's .NET-Framework loader (clr_loader) -- chosen dynamically by clr.
    "clr_loader", "clr_loader.netfx",
]

# Keep the bundle lean: pywebview's PyInstaller hook would otherwise collect every window backend,
# pulling in Qt / GTK / CEF. We only use edgechromium (and a no-ffmpeg/--app fallback elsewhere).
excludes = ["tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "cefpython3", "gi", "qtpy"]

_icon = ROOT / "packaging" / "clippycap.ico"
icon = str(_icon) if _icon.is_file() else None

a = Analysis(
    [str(ROOT / "packaging" / "clippycap_entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=[], datas=datas, hiddenimports=hiddenimports,
    hookspath=[], excludes=excludes, noarchive=False,
)
pyz = PYZ(a.pure)

# (1) one-folder build -> dist/Clippycap/   (used by the installer; faster cold start)
exe_dir = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True, name="Clippycap",
    debug=False, strip=False, upx=False, console=False, icon=icon,
)
coll = COLLECT(exe_dir, a.binaries, a.datas, strip=False, upx=False, name="Clippycap")

# (2) one-file build  -> dist/Clippycap-Portable.exe   (a single download-and-run file)
exe_onefile = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="Clippycap-Portable",
    debug=False, strip=False, upx=False, console=False, icon=icon,
)
