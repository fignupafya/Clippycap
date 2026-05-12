# Packaging Clippycap into a standalone `.exe`

Goal: a build of Clippycap that a user can run **without installing Python** -- a folder containing
`Clippycap.exe` (double-click it).

## Build (Windows)

```powershell
powershell -ExecutionPolicy Bypass -File packaging\get_ffmpeg.ps1   # optional: fetch ffmpeg into bin\
powershell -ExecutionPolicy Bypass -File packaging\build.ps1        # builds the frontend + bundles
```

The result is `dist\Clippycap\` -- ship that whole folder; the user runs `Clippycap.exe` inside it.
(`build.ps1` runs `npm --prefix web run build` and then `pyinstaller --noconfirm packaging\clippycap.spec`.)

## What gets bundled

`packaging/clippycap.spec` pulls in: the `clippycap` package (from `src/`) + the Python runtime + the
deps (FastAPI / uvicorn / pydantic / blake3 / tomli_w), plus, if present, `web/dist/` (the built SPA),
`bin/` (ffmpeg.exe / ffprobe.exe / their DLLs), and `config/default.toml`. `web/dist/` and `bin/` are
both optional -- without the SPA the backend serves a placeholder page; without ffmpeg the app falls
back to client-side thumbnails and disables trimming.

When frozen, `default_install_dir()` resolves to PyInstaller's bundled-data dir (`sys._MEIPASS`), so
`@bundled` (= `@install/bin`) finds the bundled ffmpeg, and the SPA is served from there. User data
(the SQLite db, `local.toml`, thumbnails, tag images, logs) still lives in `%APPDATA%\Clippycap\`.

## Notes / knobs

- The `.exe` is built **windowed** (no console); stdout/stderr go to `%APPDATA%\Clippycap\logs\clippycap.log`.
  For a console window during debugging, set `console=True` in `clippycap.spec`.
- For a single `.exe` instead of a folder, see the comment at the top of `clippycap.spec`. (Slower
  startup -- it unpacks to a temp dir each launch.)
- Add a `packaging/clippycap.ico` and uncomment the `icon=` line in the spec for a custom icon.
- If PyInstaller misses an import at runtime (an `ImportError` when the `.exe` runs), add the module
  name to `hiddenimports` in `clippycap.spec`.
- An installer (Start-menu shortcut, uninstaller) is a future step -- e.g. wrap `dist\Clippycap\` with
  Inno Setup or NSIS.
