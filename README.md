# Clippycap

A local, single-user, **extensible media library + annotation tool**. First use case: organising and
reviewing gameplay clips for self-improvement — find kills/mistakes, take notes (general + pinned to a
moment in the video), cross-reference clips, filter by your own tags. "video" is just the first
*media type plugin*; the core (assets, tags, notes, references, search) is media-type-agnostic, so
images / audio / `.dem` files / … can be added later without rewriting it.

Files are identified by their **content hash**, not their path — so renaming or moving a clip (even
across drives) never loses its tags or notes; a file that disappears is marked *missing*, not
deleted, and is restored automatically when it reappears.

The app opens as a **real frameless desktop window** (WebView2 via [pywebview]) with our own HTML
title bar — minimize / maximize / close, drag the brand or the empty strip — so it feels like a
native app, not a browser tab. If WebView2 is missing it falls back to a chromeless Chrome/Edge
`--app` window, then a normal browser tab.

[pywebview]: https://pywebview.flowrl.com/

## Distribution

Two single-file deliverables for Windows; everything you need is in one of them:

- **`Clippycap-Portable.exe`** (~20 MB) — one self-contained executable. Nothing to install, just run
  it. The first time, it offers to download a static FFmpeg build (~390 MB) into
  `%APPDATA%\Clippycap\bin\`; you can decline (the app still works with client-side thumbnails) and
  install it later from **Settings → FFmpeg**.
- **`Clippycap-Setup.exe`** (~104 MB) — a Windows installer (Inno Setup; per-user, no UAC). Bundles
  FFmpeg so the installed app works fully out of the box, offline; offers to install the Edge WebView2
  Runtime if it's missing. Adds a Start-menu shortcut and an uninstaller. Ships
  `THIRD_PARTY_NOTICES.txt` with the GPL attribution for FFmpeg.

User data (the SQLite library, thumbnails, tag images, logs, `local.toml`) always lives in
`%APPDATA%\Clippycap\` — shared by both builds, survives reinstalls.

## Build it yourself

```powershell
powershell -ExecutionPolicy Bypass -File build.ps1
```

That's the recipe — `build.ps1` (at the repo root) builds the web UI, runs PyInstaller (portable
`.exe` + one-folder build), runs Inno Setup if it's installed (and first downloads FFmpeg into
`bin\` for the installer to bundle), then moves the two `.exe` files into the repo root and removes
the temporary `dist\` / `build\`. After it finishes the root holds exactly `build.ps1` +
`Clippycap-Portable.exe` + `Clippycap-Setup.exe` (plus the source folders). Details and knobs in
[`packaging/README.md`](./packaging/README.md).

## Architecture

See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for the full design — the layered/hexagonal structure
(`core` → `app` → `infra` → `api` → `web`, plus a plugin layer), the extension points, the
scan/identity flows, the SQLite schema, packaging, the **decision log** (§14), and the **roadmap**
(§15).

Stack: **Python 3.13** + FastAPI (which also serves the built frontend); SQLite + FTS5;
[pywebview] / WebView2 for the window; Svelte 5 + Vite + TypeScript for the UI; ffmpeg/ffprobe via a
`FfmpegToolsHolder` so installs/path-changes take effect with no restart. (3.13 rather than 3.14
because pywebview's `pythonnet` dep has no 3.14 wheel yet; nothing in the code is 3.14-specific.)

## Set up (development)

```bat
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev,window]"   :: backend + tests/lint + pywebview
npm --prefix web install                                  :: frontend deps (once)
npm --prefix web run build                                :: emits web/dist/, the backend serves it at "/"
```

## Run

```bat
.venv\Scripts\python -m clippycap add-source "D:\path\to\your\clips"   :: register a folder to watch
.venv\Scripts\python -m clippycap scan                                 :: discover + hash media
.venv\Scripts\python -m clippycap                                      :: open the app window
```

Or just double-click **`Clippycap.bat`** in this folder. The interactive API lives at `/docs`.

Frontend dev loop: in one terminal `set CLIPPYCAP__SERVER__PORT=8765 && .venv\Scripts\python -m clippycap run --no-browser`,
in another `npm --prefix web run dev` (Vite proxies `/api`, `/media`, `/thumbnails` to the backend).

## Quality gates

```bat
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m ruff check src tests
.venv\Scripts\python -m mypy src/clippycap
```

## Status

End-to-end working: backend (config, domain core, plugin runtime, SQLite layer, scanner, ffmpeg
media layer with on-demand install, the `video` media type, all application services, the FastAPI
HTTP layer, the CLI / pywebview launcher), a full Svelte UI (the [mockup] is mostly live — rich
player, timeline with draggable IN/OUT and note markers, Tag Manager with image uploads, references
panel with @-mentions of clips and moments, saved views, Settings + FFmpeg tab, in-app
dialogs/toasts, hash-based URL routing), and a reproducible build → `Clippycap-Portable.exe` +
`Clippycap-Setup.exe` at the repo root. 65 pytest tests pass; ruff + mypy `--strict` clean. Open
items live in `ARCHITECTURE.md` §15 — mainly: JSON export/import, a sample plugin, splitting the
big `App.svelte`, more test coverage for the new flows.

[mockup]: ../Yakalamalar/yakalamalar-ui-preview.html
