# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Clippycap. From one analysis it builds BOTH:
#   dist/Clippycap-Portable.exe   -- a single self-contained .exe (one-file; unpacks to a temp dir on
#                                    each launch). Nothing to install: download it and double-click.
#   dist/Clippycap/               -- a one-folder build (Clippycap.exe + _internal/). Faster startup;
#                                    this is what packaging/installer.iss wraps into Clippycap-Setup.exe.
#
# Build:  pyinstaller --noconfirm packaging/clippycap.spec   (packaging/build.ps1 builds the web UI
# first, then runs this, then -- if Inno Setup is installed -- compiles the installer).
#
# Bundled data: config/default.toml (always) and web/dist/ (the built Svelte SPA, if present -- without
# it the backend serves a placeholder page).  ffmpeg is NOT bundled: it's large (~150 MB) and licence-
# encumbered, so the app downloads a static build on demand (Settings > FFmpeg / the first-run prompt),
# or the installer offers to. It always lands in %APPDATA%\Clippycap\bin\.
#
# A custom icon: drop a packaging/clippycap.ico here (it is picked up automatically below).

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
]

_icon = ROOT / "packaging" / "clippycap.ico"
icon = str(_icon) if _icon.is_file() else None

a = Analysis(
    [str(ROOT / "packaging" / "clippycap_entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=[], datas=datas, hiddenimports=hiddenimports,
    hookspath=[], excludes=["tkinter"], noarchive=False,
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
