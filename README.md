# Clippycap

A local, single-user, **extensible media library + annotation tool**. First use case: organising and
reviewing gameplay clips for self-improvement — find kills/mistakes, take notes (general + pinned to a
moment in the video), cross-reference clips, filter by your own tags. "video" is just the first
*media type plugin*; the core (assets, tags, notes, references, search) is media-type-agnostic, so
images / audio / `.dem` files / … can be added later without rewriting it.

Files are identified by their **content hash**, not their path — so renaming or moving a clip (even
across drives) never loses its tags or notes; a file that disappears is marked *missing*, not
deleted, and is restored automatically when it reappears.

## Architecture

See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for the full design — the layered/hexagonal structure
(`core` → `app` → `infra` → `api` → `web`, plus a plugin layer), the extension points, the scan/
identity flows, the SQLite schema, packaging, the decision log, and the roadmap. Working
conventions are in [`CLAUDE.md`](./CLAUDE.md). The intended full UI is mocked up at
`../Yakalamalar/yakalamalar-ui-preview.html` (relative to a typical layout).

Backend: Python 3.12+ / FastAPI (which also serves the built frontend); SQLite + FTS5;
ffmpeg/ffprobe auto-detected (with a no-ffmpeg, client-side-thumbnail fallback). Frontend: Svelte 5
+ Vite + TypeScript. Shipped as a single double-click executable (PyInstaller).

## Set up (development)

```bat
py -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"        :: backend + test/lint tools
npm --prefix web install                               :: frontend deps (once)
npm --prefix web run build                             :: emits web/dist/, which the backend serves at "/"
```

## Run

```bat
.venv\Scripts\python -m clippycap add-source "D:\path\to\your\clips"   :: register a folder to watch
.venv\Scripts\python -m clippycap scan                                 :: discover + hash media
.venv\Scripts\python -m clippycap                                      :: start the server, open the UI
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

The whole backend (config, domain core, plugin runtime, SQLite layer, scanner, ffmpeg media layer,
the `video` media type, the application services + composition root, the FastAPI HTTP layer, the CLI)
is implemented and tested; the frontend is a minimal-but-functional Svelte app that builds. Still to
do: the single-`.exe` packaging, the full polished UI from the mockup, a Settings screen + `PUT
/api/config`, JSON export/import, a sample plugin. See `ARCHITECTURE.md` §15 and `../Yakalamalar/CLAUDE.md`.
