# Packaging Clippycap

Two deliverables, both single `.exe` files (a user only needs the `.exe`, nothing else):

| File | What it is |
|------|------------|
| **`Clippycap-Portable.exe`** (~20 MB) | One self-contained executable (PyInstaller one-file). Nothing to install -- download and double-click. Unpacks to a temp dir on each launch (~1 s). FFmpeg is *not* bundled; the app offers (once) to download a static build on first run, or you can install it later from **Settings → FFmpeg**. |
| **`Clippycap-Setup.exe`** (~104 MB) | A Windows installer (Inno Setup; per-user, no UAC) wrapping the one-folder PyInstaller build. **Bundles FFmpeg** (~390 MB on disk after install) so the installed app works fully out of the box, offline. Adds a Start-menu (and optional desktop) shortcut and an uninstaller. Offers to install the Edge WebView2 Runtime if it's missing. Ships `THIRD_PARTY_NOTICES.txt` (GPL attribution for ffmpeg). |

This folder (`packaging/`) holds the *internals* of the build -- the PyInstaller spec, the Inno Setup
script, the ffmpeg-fetch helper, the app icon. The one entry-point script lives at the **repo root**:
**`build.ps1`**.

## Build it yourself

```powershell
powershell -ExecutionPolicy Bypass -File build.ps1
```

That's the whole recipe -- run it and you get the same `.exe` files that ship in a release, so you never have to trust a prebuilt binary. It:

1. builds the web UI (`npm --prefix web run build` → `web/dist/`),
2. installs PyInstaller + pywebview into the active environment if needed,
3. runs `pyinstaller --noconfirm packaging\clippycap.spec`, producing a one-file `.exe` and a one-folder build under a temporary `dist\`,
4. if [Inno Setup 6](https://jrsoftware.org/isdl.php) is installed (it looks for `iscc.exe` on `PATH`, under `Program Files`, and under `%LocalAppData%\Programs\`):
   a. runs `packaging\get_ffmpeg.ps1` to drop a standalone `ffmpeg.exe` + `ffprobe.exe` into `<repo>\bin\` (skip-if-present; ~80 MB download, once), so the installer can bundle them,
   b. runs `iscc packaging\installer.iss` to wrap the one-folder build + ffmpeg + `THIRD_PARTY_NOTICES.txt` into `Clippycap-Setup.exe`. If Inno Setup isn't installed, this step is skipped with a warning -- the portable `.exe` is still built.
5. **moves `Clippycap-Portable.exe` (and `Clippycap-Setup.exe`, if it was built) into the repo root, then deletes the temporary `dist\` and `build\` directories.**

So after a run, the repo root holds exactly `build.ps1` + `Clippycap-Portable.exe` + `Clippycap-Setup.exe` (plus the source folders) -- nothing else. The two `.exe`s are git-ignored, never committed.

Prerequisites: **Python 3.13** with the project + its `[window]` extra (pywebview) installed in a `.venv` at the repo root (`pip install -e ".[dev,window,build]"`), Node.js + npm, and -- only for the installer -- Inno Setup 6. *Building the installer needs internet the first time* (to fetch ffmpeg into `bin\`); subsequent runs reuse it. The portable-only build (no Inno) doesn't need internet or ffmpeg at build time.

(Python 3.13 not 3.14: pywebview's `pythonnet` dep has no 3.14 wheels yet. Nothing in the code is 3.14-specific; on 3.14 everything else works, the pywebview window just falls back to a Chrome/Edge `--app` window.)

You can also run the steps by hand: `npm --prefix web run build`, `pyinstaller --noconfirm packaging\clippycap.spec`, `powershell packaging\get_ffmpeg.ps1`, `iscc packaging\installer.iss`. `clippycap.spec`, `installer.iss`, and the two `.ps1` scripts are plain text and fully auditable.

## What's where at runtime

- **The portable `.exe`'s code / web UI / `default.toml`**: inside the bundle (PyInstaller one-file unpacks to `sys._MEIPASS` -- a temp dir -- each launch).
- **The installed app**: at `{app}` (= `%LocalAppData%\Programs\Clippycap\` by default, since the installer is per-user). The PyInstaller one-folder payload lives in `{app}\_internal\`, ffmpeg in `{app}\bin\`, the launcher `{app}\Clippycap.exe`.
- **User data** (shared by both builds, survives reinstalls): `%APPDATA%\Clippycap\` -- the SQLite library, `local.toml`, thumbnails, tag images, the on-demand-installed ffmpeg (`bin\`), plugins, logs. The installer's uninstaller leaves this folder alone -- it's your data.
- **Logs / console output**: the `.exe` is built **windowed** (no console window); stdout/stderr (including any crash) go to `%APPDATA%\Clippycap\logs\clippycap.log`. For a console window while debugging, set `console=True` in `clippycap.spec` and rebuild.

## How the app finds ffmpeg

The resolver's `"auto"` mode (in `infra/media/ffmpeg.py`) probes, in order:

1. `<data_dir>/bin/` — where the *portable*'s on-demand install lands (`%APPDATA%\Clippycap\bin\`).
2. `<install_dir>/bin/` — the bundle's bin dir (used in dev: `<repo>/bin/`).
3. `<exe_dir>/bin/` (when frozen) — **where the installer puts the bundled ffmpeg** (`{app}\bin\`).
4. Common Windows install locations (`C:\ffmpeg\bin`, choco, scoop, winget links, ...).
5. `PATH`.

So an installed app finds the bundled ffmpeg in (3); a portable app that downloaded ffmpeg finds it in (1); a dev finds the `bin\` they populated by hand or via `get_ffmpeg.ps1` in (2); and someone with ffmpeg elsewhere can either set the path explicitly in **Settings → FFmpeg** (which goes into `local.toml` and wins over `"auto"`) or just rely on (4)/(5).

## Files in here

- `clippycap.spec` -- PyInstaller spec. One Analysis → `Clippycap-Portable.exe` (one-file) **and** a one-folder build (for the installer). Bundles `config/default.toml` + `web/dist/`; **not** ffmpeg (the installer adds it separately). Uses `packaging/clippycap.ico` for the exe/window icon.
- `clippycap_entry.py` -- the PyInstaller entry point (just calls `clippycap.shell.cli.main`).
- `installer.iss` -- Inno Setup 6 script: per-user install, Start-menu + optional desktop shortcut, uninstaller, icon. Bundles `ffmpeg.exe`+`ffprobe.exe` from `<repo>\bin\` → `{app}\bin\`, and `THIRD_PARTY_NOTICES.txt`. Optional ticked task: install the Edge WebView2 Runtime if it's missing (downloads Microsoft's Evergreen Bootstrapper). Outputs `Clippycap-Setup.exe`.
- `clippycap.ico` -- the app icon (exe + window + installer).
- `get_ffmpeg.ps1` -- downloads BtbN's static GPL win64 ffmpeg build → `<repo>\bin\` (skip-if-present; cleans up stray "shared"-build DLLs). Called by `build.ps1` before iscc; also handy for dev.

## Knobs

- **App icon**: `packaging/clippycap.ico` -- used by `clippycap.spec` (exe + window) and `installer.iss` (`SetupIconFile`). Replace it to rebrand.
- **Missing import at runtime** (an `ImportError` when the `.exe` runs): add the module name to `hiddenimports` in `clippycap.spec`.
- **Console build for debugging**: set `console=True` in `clippycap.spec`.
- **Just the portable, no installer**: drop the `COLLECT(...)` line from `clippycap.spec`; build.ps1 will skip iscc.
- **Just the installer, no portable**: drop the second `EXE(... name="Clippycap-Portable" ...)` block from `clippycap.spec`.
- **Smaller installer (no ffmpeg)**: remove the two `Source: "..\bin\ffmpeg*"` lines from `installer.iss` and add back a "Download FFmpeg" task in `[Tasks]` + the matching `[Code]` (see the git history of `installer.iss` for the old version). Installer drops to ~18 MB; users get the same first-run prompt as the portable.
