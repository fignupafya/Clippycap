# Packaging Clippycap

Two deliverables, both single `.exe` files (a user only needs the `.exe`, nothing else):

| File | What it is |
|------|------------|
| **`Clippycap-Portable.exe`** | One self-contained executable (PyInstaller one-file). Nothing to install -- download and double-click. Unpacks to a temp dir on each launch, so the first start is a touch slower than the installed build. |
| **`Clippycap-Setup.exe`** | A Windows installer (Inno Setup) wrapping the one-folder build. Adds a Start-menu (and optional desktop) shortcut and an uninstaller. During setup, if FFmpeg isn't already on the machine, it offers to download it. |

This folder (`packaging/`) holds the *internals* of the build -- the PyInstaller spec, the Inno Setup
script, the on-demand-ffmpeg helper, the app icon. The one entry-point script lives at the **repo root**:
**`build.ps1`**.

## Build it yourself

```powershell
powershell -ExecutionPolicy Bypass -File build.ps1
```

That's the whole recipe -- run it and you get the same `.exe` files that ship in a release, so you never have to trust a prebuilt binary. It:

1. builds the web UI (`npm --prefix web run build` -> `web/dist/`),
2. installs PyInstaller into the active environment if needed,
3. runs `pyinstaller --noconfirm packaging\clippycap.spec`, which produces a one-file exe and a one-folder build (`Clippycap.exe` + `_internal\`) under a temporary `dist\`,
4. if [Inno Setup 6](https://jrsoftware.org/isdl.php) is installed (it looks for `iscc.exe` on `PATH` and under `Program Files`), runs `iscc packaging\installer.iss` to wrap the one-folder build into an installer. If Inno Setup isn't installed it just skips this step with a warning -- the portable `.exe` is still built.
5. **moves `Clippycap-Portable.exe` (and `Clippycap-Setup.exe`, if it was built) into the repo root, then deletes the temporary `dist\` and `build\` directories.**

So after a run, the repo root holds exactly `build.ps1` + `Clippycap-Portable.exe` + `Clippycap-Setup.exe` (plus the source folders) -- nothing else. The two `.exe`s are git-ignored, never committed.

Prerequisites: **Python 3.13** with the project + its dependencies installed (a `.venv` at the repo root is used if present, otherwise the `python` on `PATH`) -- the desktop window uses [pywebview](https://pywebview.flowrl.com/), whose `pythonnet` dependency has no Python 3.14 wheels yet; on 3.14 the app still runs but falls back to a Chrome/Edge `--app` window. Plus Node.js + npm, and -- only for the installer -- Inno Setup 6. **FFmpeg is *not* needed to build** (it isn't bundled; see below).

You can also run the steps by hand: `npm --prefix web run build`, then `pyinstaller --noconfirm packaging\clippycap.spec`, then (optionally) `iscc packaging\installer.iss`. `clippycap.spec` and `installer.iss` are both plain text and fully auditable.

## What's bundled (and what isn't)

Bundled into the executables: the `clippycap` Python package, the Python runtime, the dependencies (FastAPI / uvicorn / pydantic / blake3 / tomli_w / pywebview), `config/default.toml`, and `web/dist/` (the built SPA -- without it the backend serves a placeholder page).

**Not bundled: FFmpeg / ffprobe.** They're large (~150 MB of DLLs) and licence-encumbered, so instead the app fetches a small static build *on demand*:

- On first launch, if ffmpeg isn't found, Clippycap offers to download it. Decline and it never re-asks; you can still install it later from **Settings -> FFmpeg** (which also lets you point the app at an ffmpeg you installed yourself, anywhere).
- `Clippycap-Setup.exe` offers the same download as a ticked task on the "Select Additional Tasks" page.

In every case the downloaded `ffmpeg.exe` / `ffprobe.exe` go to `%APPDATA%\Clippycap\bin\` -- which is the first place the app's `"auto"` detection looks (it then checks the bundle dir, next to the exe, common install locations, and finally `PATH`).

## Where things live at runtime

- **Code / web UI / `default.toml`**: inside the bundle. When frozen, `default_install_dir()` resolves to PyInstaller's bundled-data dir (`sys._MEIPASS` -- a temp dir for the one-file build, the `_internal\` folder for the one-folder build).
- **User data**: `%APPDATA%\Clippycap\` -- the SQLite library, `local.toml`, thumbnails, tag images, the on-demand ffmpeg (`bin\`), plugins, and logs. This is shared by both builds and survives reinstalls/updates. (The installer's uninstaller leaves this folder alone -- it's your data.)
- **Logs / console output**: the `.exe` is built **windowed** (no console window); stdout/stderr (including any crash) go to `%APPDATA%\Clippycap\logs\clippycap.log`. For a console window while debugging, set `console=True` in `clippycap.spec` and rebuild.

## Files in here

- `clippycap.spec` -- PyInstaller spec. One analysis -> `Clippycap-Portable.exe` (one-file) **and** a one-folder build (for the installer). Bundles `config/default.toml` + `web/dist/`; **not** ffmpeg. Uses `packaging/clippycap.ico` for the exe/window icon if present.
- `clippycap_entry.py` -- the PyInstaller entry point (just calls `clippycap.shell.cli.main`).
- `installer.iss` -- Inno Setup 6 script: per-user install, Start-menu (and optional desktop) shortcut, uninstaller, an icon, and an optional "download FFmpeg" task. Outputs `Clippycap-Setup.exe`.
- `clippycap.ico` -- the app icon (exe, window, installer).
- `get_ffmpeg.ps1` -- dev helper: drops a static ffmpeg/ffprobe into `<repo>/bin/` (the packaged app downloads it on demand instead).

## Knobs

- **App icon**: `packaging/clippycap.ico` -- used by `clippycap.spec` (exe + window) and `installer.iss` (`SetupIconFile`). Replace it to rebrand.
- **Missing import at runtime** (an `ImportError` when the `.exe` runs): add the module name to `hiddenimports` in `clippycap.spec`.
- The one-file build is bigger and slower to start than the one-folder one (it has to unpack itself each time). If you only want the installer, drop the `EXE(... name="Clippycap-Portable" ...)` block from `clippycap.spec`; if you only want the portable `.exe`, drop the `COLLECT(...)` line and don't run `iscc`.
