# Packaging Clippycap

Two deliverables, both single `.exe` files (a user only needs the `.exe`, nothing else):

| File | What it is |
|------|------------|
| **`Clippycap-Portable.exe`** | One self-contained executable (PyInstaller one-file). Nothing to install -- download and double-click. Unpacks to a temp dir on each launch, so the first start is a touch slower than the installed build. |
| **`Clippycap-Setup.exe`** | A Windows installer (Inno Setup) wrapping the one-folder build. Adds a Start-menu (and optional desktop) shortcut and an uninstaller. During setup, if FFmpeg isn't already on the machine, it offers to download it. |

## Build it yourself

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

That's the whole recipe -- run it and you get the same `.exe` files that ship in a release, so you never have to trust a prebuilt binary. It:

1. builds the web UI (`npm --prefix web run build` -> `web/dist/`),
2. installs PyInstaller into the active environment if needed,
3. runs `pyinstaller --noconfirm packaging\clippycap.spec`, which produces a one-file exe and a one-folder build (`Clippycap.exe` + `_internal\`) under `dist\`,
4. if [Inno Setup 6](https://jrsoftware.org/isdl.php) is installed (it looks for `iscc.exe` on `PATH` and under `Program Files`), runs `iscc packaging\installer.iss` to wrap the one-folder build into an installer. If Inno Setup isn't installed it just skips this step with a warning -- the portable `.exe` is still built.
5. moves the finished **`Clippycap-Portable.exe`** (and **`Clippycap-Setup.exe`**, if it was built) into the **repo root**, then deletes the temporary `dist\` and `build\` directories.

So after a run you're left with `Clippycap-Portable.exe` (and `Clippycap-Setup.exe`) sitting in the repo root and nothing else -- those are what you ship; they're git-ignored, never committed.

Prerequisites: Python 3.12+ with the project + its dependencies installed (a `.venv` at the repo root is used if present, otherwise the `python` on `PATH`), Node.js + npm, and -- only for the installer -- Inno Setup 6. **FFmpeg is *not* needed to build** (it isn't bundled; see below).

You can also run the steps by hand: `npm --prefix web run build`, then `pyinstaller --noconfirm packaging\clippycap.spec`, then (optionally) `iscc packaging\installer.iss`. `clippycap.spec` and `installer.iss` are both plain text and fully auditable.

## What's bundled (and what isn't)

Bundled into the executables: the `clippycap` Python package, the Python runtime, the dependencies (FastAPI / uvicorn / pydantic / blake3 / tomli_w), `config/default.toml`, and `web/dist/` (the built SPA -- without it the backend serves a placeholder page).

**Not bundled: FFmpeg / ffprobe.** They're large (~150 MB of DLLs) and licence-encumbered, so instead the app fetches a small static build *on demand*:

- On first launch, if ffmpeg isn't found, Clippycap offers to download it. Decline and it never re-asks; you can still install it later from **Settings -> FFmpeg** (which also lets you point the app at an ffmpeg you installed yourself, anywhere).
- `Clippycap-Setup.exe` offers the same download as a ticked task on the "Select Additional Tasks" page.

In every case the downloaded `ffmpeg.exe` / `ffprobe.exe` go to `%APPDATA%\Clippycap\bin\` -- which is the first place the app's `"auto"` detection looks (it then checks the bundle dir, next to the exe, common install locations, and finally `PATH`).

## Where things live at runtime

- **Code / web UI / `default.toml`**: inside the bundle. When frozen, `default_install_dir()` resolves to PyInstaller's bundled-data dir (`sys._MEIPASS` -- a temp dir for the one-file build, the `_internal\` folder for the one-folder build).
- **User data**: `%APPDATA%\Clippycap\` -- the SQLite library, `local.toml`, thumbnails, tag images, the on-demand ffmpeg (`bin\`), plugins, and logs. This is shared by both builds and survives reinstalls/updates. (The installer's uninstaller leaves this folder alone -- it's your data.)
- **Logs / console output**: the `.exe` is built **windowed** (no console window); stdout/stderr (including any crash) go to `%APPDATA%\Clippycap\logs\clippycap.log`. For a console window while debugging, set `console=True` in `clippycap.spec` and rebuild.

## Knobs

- **Custom icon**: drop `packaging\clippycap.ico` -- `clippycap.spec` picks it up automatically. (For the installer too, also uncomment `SetupIconFile=clippycap.ico` in `installer.iss`.)
- **Missing import at runtime** (an `ImportError` when the `.exe` runs): add the module name to `hiddenimports` in `clippycap.spec`.
- The one-file build is bigger and slower to start than the one-folder one (it has to unpack itself each time). If you only want the installer, you can drop the `EXE(... name="Clippycap-Portable" ...)` block from `clippycap.spec`; if you only want the portable `.exe`, drop the `COLLECT(...)` line and don't run `iscc`.
