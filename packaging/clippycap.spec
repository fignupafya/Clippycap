# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Clippycap. Build with:  pyinstaller --noconfirm packaging/clippycap.spec
# (run packaging/build.ps1 to also build the frontend first). Output: dist/Clippycap/Clippycap.exe.
#
# Prerequisites the build picks up if present:
#   web/dist/   -- the built Svelte SPA  (run `npm --prefix web run build`)
#   bin/        -- ffmpeg.exe + ffprobe.exe (+ their DLLs)  (run packaging/get_ffmpeg.ps1)
# Either may be absent: without web/dist the backend serves a placeholder page; without bin/ the
# app uses its no-ffmpeg fallback (client-side thumbnails, no trimming).
#
# To build a single .exe instead of a folder: replace the EXE(...) call with the all-in-one form
# (`EXE(pyz, a.scripts, a.binaries, a.datas, [], name="Clippycap", ...)`) and delete the COLLECT(...)
# line. (Slower startup -- it unpacks to a temp dir on every launch.)

from pathlib import Path

ROOT = Path(SPECPATH).parent          # SPECPATH = the dir holding this .spec (i.e. packaging/)

datas = [(str(ROOT / "config" / "default.toml"), "config")]
if (ROOT / "web" / "dist").is_dir():
    datas.append((str(ROOT / "web" / "dist"), "web/dist"))
if (ROOT / "bin").is_dir():
    datas.append((str(ROOT / "bin"), "bin"))

hiddenimports = [
    # uvicorn loads its loop / protocol implementations dynamically -- PyInstaller can't see those.
    "uvicorn.logging", "uvicorn.loops", "uvicorn.loops.auto",
    "uvicorn.protocols", "uvicorn.protocols.http", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets", "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan", "uvicorn.lifespan.on",
]

a = Analysis(
    [str(ROOT / "packaging" / "clippycap_entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Clippycap",
    debug=False,
    strip=False,
    upx=False,
    console=False,        # no console window; output goes to %APPDATA%/Clippycap/logs/clippycap.log
    # icon=str(ROOT / "packaging" / "clippycap.ico"),   # drop a clippycap.ico here for a custom icon
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Clippycap")
