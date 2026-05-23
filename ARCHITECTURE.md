# Clippycap — Architecture

> Status: **planning / scaffolding**. No business logic implemented yet. This document is the source of truth for *what is built, why, and where*. It must be updated whenever the design changes.
>
> Companion docs: `config/default.toml` (the single source of all default behaviour), `docs/` (UI mockup, notes).

---

## 1. What Clippycap is

A **local, single-user, extensible media library + annotation tool**. Today it is used to organise and review **gameplay clips** (Team Fortress 2 recordings made by OBS, ~1 minute each) for self-improvement: find kills/mistakes, take notes, cross-reference clips. Tomorrow it should handle other media kinds (images, audio, demo files, …) **without rewriting the core** — "video" is just the first *media type plugin*.

Core capabilities:

- **Library**: point the app at one or more *source folders*; it discovers media files and tracks them by **content identity (hash)**, not by path — so renaming or moving a file does not lose anything.
- **Tagging**: a **flat, fully user-defined set of tags** (no built-in categories, no hierarchy). Each tag has a colour and an *icon* (from a bundled set) or a *custom uploaded image*. A media item can have any number of tags. Everything about tags is managed in the UI; nothing is hardcoded.
- **Notes**: per-item **general note** (markdown) plus **timestamped notes** pinned to a moment in the video (shown as markers on the timeline; clicking one seeks there). Timestamped notes can themselves carry tags.
- **References**: link a media item to other items, with an optional *relation type* (e.g. "better version of ⇄ worse version of", "same mistake", "see also" — the relation types are user-editable, seeded from config), a free-text label/note, and optional timestamps on either side. References are shown on both items (incoming & outgoing).
- **Review player**: a video player tuned for analysis — playback speed (0.1×–2×), frame-by-frame stepping, A–B loop, frame export, full keyboard control, timeline markers for timestamped notes.
- **Find**: filter by tags (AND / OR), full-text search over notes and titles, sort, *saved views* ("smart collections" = a saved filter).
- **Packaging**: ships as a **single double-click executable** (Python backend that also serves the built web UI, wrapped in a native window via `pywebview`, bundled into one `.exe` with PyInstaller). Data lives under the OS app-data directory.

---

## 2. Domain model (concepts)

Plain language first; the SQLite schema (Section 9) is a faithful projection of this.

| Concept | Meaning |
|---|---|
| **Asset** | One piece of media, identified by its **content hash** (not its path). Has a `media_type` ("video"), a user-editable display `title` (defaults from the file name), `size`, timestamps (`added_at`, `last_seen_at`, `last_opened_at`), and **media-type-specific metadata** stored as JSON (for video: `duration_ms`, `width`, `height`, `fps`, `codec`, `recorded_at` parsed from the file name, …). |
| **AssetPath** | A known on-disk location of an Asset (`path`, `volume_id`, `last_seen_at`, `present`). An Asset may have **several** (same file copied to two places). If *all* of an Asset's paths are `present = false`, the Asset is **missing** — kept, never deleted; it is *resurrected* automatically when the file reappears (e.g. an external drive is plugged back in). |
| **Tag** | A user-defined label: `name` (unique), `color`, **either** `icon` (reference into the bundled icon set) **or** `image_ref` (a small uploaded file stored in the app data dir), optional `description`, `sort_order` (also used as the `1‑9` quick-tag key order). Created/renamed/recoloured/deleted entirely from the UI. |
| **AssetTag** | The many-to-many link between an Asset and a Tag (`added_at`). |
| **Note** | Text attached to an Asset. `body` is markdown. `timestamp_ms` is `NULL` for the *general note* or a value for a *timestamped note*. Has `created_at` / `updated_at`. |
| **NoteTag** | Many-to-many link letting a *timestamped* note carry tags (e.g. a note at 0:34 tagged `mistake/ammo`). |
| **ReferenceType** | An optional, user-editable kind of relationship: `name`, optional `reverse_name` (if set, the relation is directed and shows differently on the two sides), `color`, `sort_order`. Seeded from `config/default.toml` on first run; fully editable afterwards. If the user deletes them all, references just use a free-text label. |
| **Reference** | A directed link `from_asset → to_asset` with optional `type_id`, free-text `label`, optional `from_timestamp_ms` / `to_timestamp_ms` ("compare 0:31 of A with 0:44 of B"), optional `note`, `created_at`. Surfaced on both Assets. |
| **Source** | A watched folder: `path`, `recursive`, `enabled`, `media_types` to scan for, `last_scanned_at`. |
| **SavedView** | A named, saved filter ("smart collection"): `filter` (JSON — tag AND/OR sets, text query, date range), `sort`, `sort_order`. Some views ("All", "Untagged", "Recently added", "Recently opened") are **derived/built-in**, not stored. |
| **HashCache** | `(path, size, mtime) → partial_hash, full_hash`. A pure performance cache so re-scans don't re-hash unchanged files. |

> **Why no built-in "review status" field (new / to-review / reviewed)?** The tag model is explicitly *flat and fully dynamic* ("everything is a tag"). A workflow status is just a tag (`to-review`, `reviewed`) plus a saved view. A *derived* "new" indicator (Asset never opened in the app — `last_opened_at IS NULL`) covers the "haven't looked at this yet" need without a stored enum. If a first-class status turns out to be needed, it can be added later without breaking anything. *(Decision log: §14.)*

---

## 3. Architecture at a glance

Hexagonal / "clean" layering + a plugin layer. Dependencies point **inward** (outer layers know inner ones; never the reverse). The domain core has **zero** infrastructure dependencies and is unit-testable in isolation.

```
                         ┌───────────────────────────────────────────────┐
                         │  shell/   (pywebview window  |  browser mode)  │   ← desktop "AppShell" adapter
                         └───────────────────────────────────────────────┘
                                              │ launches
                         ┌───────────────────────────────────────────────┐
   web/  (Svelte + Vite) │  api/     FastAPI routers  (+ media streaming) │   ← thin controllers
   served as static ───► └───────────────────────────────────────────────┘
   files by api/                            │ calls
                         ┌───────────────────────────────────────────────┐
                         │  app/     use-case services                   │
                         │   ScanLibrary · AssetQuery · Tag · Note ·      │
                         │   Reference · TagAsset · ManageSources …       │
                         └───────────────────────────────────────────────┘
                            │ depends on interfaces in core │ raises events
                         ┌───────────────────────┐   ┌──────────────────┐
                         │  core/   domain        │   │ plugins_runtime/ │
                         │  entities · value objs │   │  registries +    │
                         │  rules · INTERFACES:   │◄──┤  plugin discovery│
                         │   Repository*          │   │  + EventBus      │
                         │   IdentityStrategy     │   └──────────────────┘
                         │   MediaTypeProvider     │            ▲ register
                         │   MetadataExtractor      │           │
                         │   Thumbnailer            │   ┌──────────────────────────────┐
                         │   ExportProvider/Importer│   │ media_types/video/           │  built-in, but registered
                         └───────────────────────┘     │  VideoMediaType + ffmpeg/…   │  *like* a plugin
                                    ▲ implemented by    └──────────────────────────────┘
                         ┌───────────────────────────────────────────────┐   ┌──────────┐
                         │  infra/   db (SQLite repos, migrations, FTS5)  │   │ plugins/ │  3rd-party / user
                         │           scan (walker, hashing, HashCache)    │   │ (empty)  │  plugins live here
                         │           media (ffmpeg & browser adapters)    │   └──────────┘
                         │           config (loader + Pydantic schema)    │
                         └───────────────────────────────────────────────┘
```

**Rule of thumb:** if a piece of code talks to SQLite, the filesystem, ffmpeg, or HTTP, it lives in `infra/` or `api/`. If it expresses *what the app does* in domain terms, it lives in `core/` (rules) or `app/` (orchestration). Outer code depends on **interfaces** declared in `core/`; concrete implementations are wired together at startup (a small composition root in `api/` / `shell/`).

---

## 4. Layers in detail

### `core/` — domain
Pure Python. Entities & value objects (dataclasses), domain rules (e.g. *resolving a discovered file to an Asset by hash*, *applying a tag*, *creating a reference*), and **the abstract interfaces** the rest of the system implements:
`AssetRepository`, `TagRepository`, `NoteRepository`, `ReferenceRepository`, `SourceRepository`, `SavedViewRepository`, `HashCacheStore`; `IdentityStrategy`; `MediaTypeProvider`; `MetadataExtractor`; `Thumbnailer`; `ExportProvider` / `ImportProvider`.
*Why:* the most stable layer; lets us swap DB/tools/UI without touching business meaning; fully testable without I/O. *Alternative rejected:* logic in controllers/ORM models — untestable, brittle, leaks infrastructure into the domain. *Extends by:* adding a new entity or rule = new file in `core/`, nothing existing changes.

### `app/` — application / use cases
One class per use case (`ScanLibraryService`, `AssetQueryService`, `TagService`, `TagAssetService`, `NoteService`, `ReferenceService`, `SourceService`, `SavedViewService`, `ConfigService`, `ExportService`). They orchestrate domain objects + repository interfaces, and publish domain events on the `EventBus`. *Why:* "what the app does" in one place; API, future CLI, and tests all call the same services. *Extends by:* new use case = new service.

### `infra/` — infrastructure (adapters)
- `infra/db/` — SQLite-backed implementations of the repository interfaces; schema definition; a tiny forward-only **migration runner** (numbered SQL/py steps recorded in a `schema_migrations` table); FTS5 virtual table maintenance for search. *(Raw `sqlite3` + a thin repo layer is the baseline; if it ever needs more, the repo interfaces mean the swap is local.)*
- `infra/scan/` — recursive filesystem walker; the **hashing** primitives; `HashCacheStore`; "skip files modified in the last *N* seconds" (a file OBS is still writing); NTFS volume + file-id handling so the same file reached via two paths/symlinks/junctions isn't double-counted.
- `infra/media/` — `FfmpegMetadataExtractor` & `FfmpegThumbnailer` (call `ffprobe` / `ffmpeg`), plus `BrowserThumbnailer` (no-ffmpeg fallback that captures a frame client-side) — chosen per media type and per the `media.ffmpeg.enabled` config switch.
- `infra/config/` — the configuration loader (Section 8) and its Pydantic schema.

### `api/` — interface (HTTP)
FastAPI application assembled from **router modules** (`assets`, `tags`, `asset_tags`, `notes`, `references`, `reference_types`, `sources`, `saved_views`, `scan`, `config`, `media_stream`, `thumbnails`, `icons`, `plugins`). Controllers are thin: validate input (Pydantic), call an `app/` service, return a DTO. **`media_stream`** serves the actual video bytes with HTTP **Range** support (seeking / frame-stepping in the browser depends on this). A background task queue handles long jobs (scanning, thumbnail generation) and pushes progress to the UI via Server-Sent Events. The composition root here builds concrete repos/strategies from config and injects them into services. *Extends by:* a plugin can contribute its own router (mounted under a namespaced prefix).

### `web/` — presentation (Svelte + Vite, TypeScript)
Its own modular structure: `api-client/` (typed wrappers over the HTTP API), `store/` (app state), `views/` (Library view, Detail view, Settings), `components/` (cards, tag chips, tag manager modal, virtualised grid, …), `player/` (the analysis player + keyboard layer), and a **panel registry** — the four detail-view tabs (Tags / Notes / References / Info) and the sidebar sections are *registered*, not hardcoded; a plugin (or future feature) adds a tab by registering one. Built with `vite build` → static assets that `api/` serves; in development, Vite's dev server proxies to the API. *Why a framework (Svelte):* a clean component model makes the "registered panels" pattern and a maintainable UI cheap; small bundle, little boilerplate. *Alternatives considered:* vanilla + Web Components (zero framework, more hand-written UI plumbing); React/Vue (bigger ecosystem, more boilerplate).

### `plugins_runtime/` — extension machinery
The **registries** (`MediaTypeRegistry`, `IdentityStrategyRegistry`, `ThumbnailerRegistry`, `MetadataExtractorRegistry`, `ImportExportRegistry`, `ApiRouterRegistry`, `UiPanelRegistry`), the **`EventBus`** (publish/subscribe for domain events), and **plugin discovery** (scan the `plugins/` dir + the data-dir plugins dir at startup, import each, call its `register(context)` entry point with a small, stable `PluginContext` giving access to the registries, the event bus, config, and a logger). The built-in `media_types/video` package registers itself through the *same* mechanism — dogfooding.

### `shell/` — desktop AppShell (adapter)
Starts the FastAPI server on a local port and opens it in a native window via `pywebview` (Edge WebView2 on Windows, already present on Win10/11); a config switch falls back to "open in the default browser". This is a thin adapter — replacing it with Tauri/Electron/CLI later is a single-file change.

### `media_types/video/`
The first `MediaTypeProvider`: declares the recognised extensions, supplies the metadata extractor & thumbnailer to use, names the identity strategy, parses `recorded_at` out of OBS-style file names, and hints which frontend player component to use. Lives in the source tree (so it ships by default) but is wired in via the registry exactly like an external plugin would be.

---

## 5. Extension points — how to add things without editing existing code

| You want to… | Do this |
|---|---|
| Support a new media kind (images, audio, `.dem`, …) | Implement `MediaTypeProvider`; register it. It declares extensions/detection, metadata extractor, thumbnailer, identity strategy, and a frontend player/preview hint. The core, tagging, notes, references, search — all unchanged. |
| Change *how files are identified* | Implement `IdentityStrategy` (e.g. `PerceptualVideoHash`); register it; select it in config (globally or per media type). |
| Change *how thumbnails / metadata are produced* | Implement `Thumbnailer` / `MetadataExtractor`; register it for the relevant media type(s). |
| React to things happening (auto-tag, stats, backup, …) | Subscribe to `EventBus` events (`AssetAdded`, `AssetUpdated`, `AssetMissing`, `TagApplied`, `NoteCreated`, `ReferenceCreated`, `ScanStarted`, `ScanCompleted`, …) from a plugin. |
| Add API endpoints | A plugin contributes a FastAPI router via `ApiRouterRegistry`; mounted under a namespaced prefix. |
| Add a UI panel / detail tab / sidebar section | Register it via `UiPanelRegistry` (the built-in Tags/Notes/References/Info tabs use this too). |
| Add an import/export format | Implement `ExportProvider` / `ImportProvider` (CSV, sidecar `.json` next to each file, ELAN/EAF, …); register it. |
| Change defaults / behaviour | Edit `config/default.toml` (ships in the repo) or override in `local.toml` — see Section 8. |

A **plugin** is a Python package placed in `plugins/` (or the data-dir plugins folder) exposing `def register(context: PluginContext) -> None`. It can be enabled/disabled in config. There is no plugin API surface beyond `PluginContext` + the registry/interface contracts in `core/` and `plugins_runtime/`.

---

## 6. Key flows

### Scanning a library
A scan runs as a background job in **two phases**, so a large first scan stays usable throughout — the clips appear in the grid within seconds and the heavy work streams in afterwards.

**Phase 1 — discovery (`LibraryScanner`).** For each enabled `Source`: walk the folder (respecting `recursive`, ignored globs, hidden files) → for each file ask each `MediaTypeProvider.detect()` → if a type claims it: look up `HashCache` by `(path, size, mtime)`; if hit, reuse the hash, else compute it via the configured `IdentityStrategy` (Section 7) — which reads only the file's size + two end slices, not every byte — and cache it → find the Asset with that identity hash. **found** → record this `path` as `present`, clear `missing`. **not found** → create the Asset with only the *cheap* metadata (`MediaTypeProvider.quick_metadata()` — a video's `recorded_at` from its file name, no subprocess), flag it `metadata_pending`, emit `AssetAdded`. The scan **commits every `[scan].commit_batch_size` files** (not once at the end), so the grid fills in progressively.

**Phase 2 — enrichment (`MetadataEnricher`).** Every asset still `metadata_pending` is run through `MediaTypeProvider.extract_metadata()` (ffprobe, for video) to fill in duration / resolution / codec; the flag is cleared, committing per batch so durations fill the grid live. Enrichment always sweeps *all* pending assets, so an interrupted scan — or clips discovered before ffmpeg was installed — get finished the next time it runs (after any scan, at startup, or once ffmpeg becomes available). Thumbnails stay lazy (generated on first request).

A file modified within the last *N* seconds is skipped (OBS may still be writing it). After the walk, an `AssetPath` not seen this scan → `present = false`; an Asset whose paths are all absent → **missing** (emit `AssetMissing`); its tags/notes/references stay. `ScanStarted` / `ScanCompleted` bracket the run; per-phase progress is streamed to the UI, which live-refreshes the grid while a scan is active.

### Identity resolution on rename / move / copy / edit
- **rename or move** → next scan computes the hash at the new path, matches the existing Asset, just updates the path list. Nothing else changes. Drive-letter changes (`E:` → `F:`) are irrelevant — identity is content, not path.
- **a copy in a second folder** → same hash → same Asset, now with two `present` paths.
- **edited in place (trimmed/re-encoded)** → different content → different hash → looks like a *new* Asset; the old one becomes *missing*. The UI flags this and offers **"merge into another Asset"** (moves tags/notes/references over). A future `PerceptualVideoHash` plugin could propose such merges automatically.

### Tagging
In the Detail view: click a tag in the pool, or press its `1‑9` key, or type a name and press Enter (if it doesn't exist, it's created on the spot). `AssetTag` is upserted; `TagApplied` is emitted. Renaming/recolouring/deleting a tag in the Tag Manager reflects everywhere instantly (single source of truth; M:N link).

### Notes & references
General note: a markdown editor, autosaved. Timestamped note: "add note at current time" (also the `N` key — playback pauses), stored with `timestamp_ms`; appears as a timeline marker; clicking it seeks there; can carry tags. Reference: "add reference" → search for the other Asset → optionally pick a relation type, write a label/note, set timestamps on either side; shown on both Assets (outgoing & incoming).

---

## 7. Identity & hashing — details

**Principle:** identify media by **content**, never by path. A path is just "where a copy currently is".

**Default strategy — `blake3-composite` (`b3c:`):** the identity is `BLAKE3(exact byte size ‖ first N bytes ‖ last N bytes)` — N from `[identity].partial_hash_head_bytes` / `partial_hash_tail_bytes`. Only ~128 KB is read per file instead of every byte, so the first scan of a large library is many times faster on a spinning disk. The exact size is the first thing hashed, so two files share an identity only if their byte count **and** both end slices match — for real recordings (each end carries content-specific container structure) that is as collision-free as a whole-file hash. A file no larger than head + tail is hashed whole (the slices would otherwise overlap). With the `(path, size, mtime) → hash` cache, a re-scan re-hashes only new or changed files. *Why end-slices + size and not a full hash:* fully hashing an unchanged multi-GB library is wasteful I/O — this is the model digiKam has shipped for years. *Why not a path/size-only key:* too weak for *identity*.

**Full strategy — `blake3` (`b3:`):** a whole-file BLAKE3 hash, selectable via `[identity].strategy` for anyone who wants an absolute content hash. Changing the configured strategy **re-identifies the existing library** once, in the background (`IdentityUpgrader`), so the library never ends up half on one format and half on another.

**Other strategies (pluggable, future):**
- `PerceptualVideoHash` — keyframe pHash and/or audio fingerprint; survives re-encode/trim/format change; heavier; would power "is this the edited version of that?" suggestions and de-dup.
- Embedded-ID / sidecar — write a stable id into the file's metadata or a `file.ext.assetid` sidecar; survives even re-encoding (if re-embedded). Off by default (modifies/【shadows】 files).

**Edge cases handled:** offline/removable drives (offline ≠ deleted); byte-identical copies (one Asset, many paths); files being written by OBS (skip recently-modified); symlinks/junctions and the same file via two paths (NTFS volume + file-id de-dup); case-insensitive paths (normalised); huge files (partial-first); non-ASCII paths (UTF-8 throughout); zero-byte / unreadable files (skipped, flagged); a previously-missing Asset's hash reappearing (auto-resurrect, restoring its tags/notes/references).

---

## 8. Configuration system — "nothing hardcoded"

**Goal:** behaviour is *data*, not code. There is **no** code-level default-with-fallback (no `getenv("X", "default")`, no literal magic values scattered in functions). The single source of defaults is a real, editable file shipped in the repo.

**Layers (later overrides earlier):**
1. `config/default.toml` — ships in the repo; the canonical defaults; heavily commented; the only place a default value is defined.
2. `local.toml` — the user's overrides, in the data dir; on first run, a commented copy of `default.toml` is written here for the user to edit. *(This is a setup step, not a code fallback.)*
3. Environment variables (prefix `CLIPPYCAP__`, `__` for nesting) — for deployment/ops only.

The merged result is validated against a **Pydantic schema**. A missing or invalid key is a **hard, explicit startup error** — never a silent built-in default. The UI's Settings screen edits `local.toml` through the `config` API.

**Path tokens** (so platform-specific or install-relative paths aren't hardcoded as strings in code): `@appdata` → OS app-data dir (`%APPDATA%` on Windows, `~/.config` on Linux, `~/Library/Application Support` on macOS), `@data` → the resolved app data dir, `@install` → the install/bundle dir, `@bundled` → the bundled-tools dir (where the packaged ffmpeg lives). The loader expands these.

**What lives in config** (grows as features land): app identity & data dir; server host/port/open-browser; UI theme/accent/density/etc.; scan behaviour (recursive default, ignored globs, hidden-file handling, "skip modified within N seconds"); identity strategy + partial-hash byte count; per-media-type settings; `media.ffmpeg.enabled` + tool paths + thumbnail count/size; thumbnail cache settings; logging level; plugins dir + enabled/disabled lists; keybindings (player & app); seed reference types; default sort. **What does *not* live in config** (it's user data, edited in the UI, stored in SQLite): tags, the asset↔tag links, notes, references (and *current* reference types — config only *seeds* them once), sources, saved views.

---

## 9. Data storage

**SQLite** — one file (`@data/library.sqlite`), relational (tag filtering with AND/OR/NOT, the reference graph, "how many Assets have tag X"), with **FTS5** for note/title search; backup = copy the file; export = JSON (and, later, sidecar files / CSV / ELAN). Tables mirror Section 2: `assets`, `asset_paths`, `tags`, `asset_tags`, `notes`, `note_tags`, `reference_types`, `references`, `sources`, `saved_views`, `hash_cache`, `notes_fts` (FTS5), `schema_migrations`, `app_meta`. Migrations are forward-only, numbered, recorded in `schema_migrations`, run at startup. Uploaded tag images, generated thumbnails, and plugin data live as files under `@data/` (referenced from the DB), not as blobs in it.

*Why SQLite and not per-file JSON sidecars as the primary store:* global queries (filter/search/graph) need an index; many small files = lots of I/O and no cross-item queries. *Why not a server DB:* single-user, local, must "just work" from one `.exe` — a server DB is operational overhead with no benefit here. Sidecars remain valuable as an *export/portability* format, behind `ExportProvider`.

---

## 10. Frontend architecture (summary)

Svelte + Vite + TypeScript. `api-client/` mirrors the HTTP API with typed functions; `store/` holds UI + cached server state; `views/` = Library / Detail / Settings; `components/` = grid (virtualised — must handle thousands of items), media card, tag chip, tag pool, Tag Manager modal, reference picker, etc.; `player/` = the analysis player wrapping `<video>` with custom controls (speed via `playbackRate`, frame-step via `requestVideoFrameCallback` or `±1/fps` seeking, A–B loop in JS, frame export via a canvas, full keyboard map from config) and a timeline that renders timestamped-note markers; **`panels/` + a `UiPanelRegistry`** so detail-view tabs and sidebar sections are pluggable (the built-in four tabs register through it). The video element streams from `GET /media/{asset}/stream` (Range-enabled). UI strings are in English (the user picked all-English); an i18n seam can be added later if needed.

---

## 11. Packaging & runtime

- **Dev:** a Python venv (`pip install -e ".[dev]"`; `uv` if available) for the backend; `vite dev` for the web side (proxying API calls); FastAPI run with reload.
- **Build:** `vite build` → `web/dist`; PyInstaller (`packaging/clippycap.spec`) bundles the Python runtime + app + `web/dist` + a fetched ffmpeg/ffprobe + a copy of `config/default.toml` into one `.exe` (a `packaging/get_ffmpeg.*` script fetches the binaries).
- **Run:** double-click the `.exe` → `shell/` starts the FastAPI server on a free local port and opens a `pywebview` window pointed at it. **Data** (`library.sqlite`, `local.toml`, `thumbnails/`, `tag-images/`, `plugins/`, `logs/`) lives under `@appdata/Clippycap/`. **First run:** create the data dir, write a commented `local.toml`, create & migrate the DB, seed reference types from config, and offer to add the OS "Videos" folder (and any folders the user confirms) as the first Source.

---

## 12. Project structure

```
clippycap/
├─ pyproject.toml              # project metadata + Python deps (pip / uv in a venv)
├─ README.md                   # what it is, how to dev/build/run
├─ ARCHITECTURE.md             # this file — keep it current
├─ .gitignore
├─ config/
│  └─ default.toml             # the single source of all default behaviour (a real file, not code)
├─ src/clippycap/
│  ├─ core/                    # domain: entities, value objects, rules, INTERFACES
│  ├─ app/                     # use-case services
│  ├─ infra/
│  │  ├─ db/                   # SQLite repos, schema, migrations, FTS5
│  │  ├─ scan/                 # filesystem walker, hashing, HashCache
│  │  ├─ media/                # ffmpeg & browser metadata/thumbnail adapters
│  │  └─ config/               # config loader + Pydantic schema
│  ├─ api/                     # FastAPI app + router modules (+ media streaming, SSE)
│  ├─ plugins_runtime/         # registries, EventBus, plugin discovery, PluginContext
│  ├─ shell/                   # pywebview AppShell adapter (+ browser mode)
│  └─ media_types/
│     └─ video/                # VideoMediaType (ships built-in, wired via the registry)
├─ plugins/                    # 3rd-party / user plugins (empty to start)
├─ web/                        # Svelte + Vite frontend (build output served by api/)
│  └─ src/{api-client,store,views,components,panels,player}/
├─ tests/                      # core & app unit tests; infra integration tests
├─ packaging/                  # PyInstaller spec, ffmpeg fetch script, app icon
└─ docs/                       # UI mockup, design notes
```

(The interactive UI mockup is currently at `D:\Yakalamalar\yakalamalar-ui-preview.html` — kept there because it references the real clips by relative path; a copy/screenshots will land in `docs/`.)

---

## 13. Conventions

- **Language:** all code, identifiers, comments, and documentation in **English**. UI strings in English for now (i18n seam later if wanted).
- **Every design decision is recorded with: why this way · what the alternative was · how it extends.** New decisions go in §14.
- **Keep `ARCHITECTURE.md` current** — update it in the same change that alters the design.
- **No hardcoding** — see §8. No magic values, no code-level fallbacks; defaults live in `config/default.toml`.
- **Separation of concerns / DRY / clean code** — outer layers depend on `core/` interfaces; shared logic is factored into helpers; the domain stays I/O-free and tested.
- **Tests** — `core/` and `app/` carry unit tests; `infra/` has integration tests against a temp dir / temp SQLite; the scanner is tested against synthetic files for rename/move/missing/merge/copy.
- **Work style** — consult before assuming; explain plans with cause→effect and get approval; think about edge cases up front; don't tunnel-vision.

---

## 14. Decision log

| # | Decision | Why | Alternative(s) considered | How it extends |
|---|---|---|---|---|
| D1 | Local **web app**: Python/FastAPI backend that also serves a Svelte/Vite frontend | Cleanest separation; richest UI tech; lowest friction; the "be a desktop app" concern becomes *packaging*, not architecture | Tauri (needs Rust toolchain), Electron (heavy ~150 MB+), Python GUI toolkit (worse UI, harder to make modular) | Swap the `shell/` adapter (pywebview → Tauri/Electron/CLI); the API/app/core are untouched |
| D2 | Ship **two single `.exe` files** from one PyInstaller analysis: `Clippycap-Portable.exe` (one-file, just run it) **and** a one-folder build wrapped by an Inno-Setup `Clippycap-Setup.exe` (per-user, Start-menu/desktop shortcut, uninstaller, icon, optional "download FFmpeg"/"install Edge WebView2 Runtime" tasks). `build.ps1` (at the repo root) produces *both, into the repo root*, and removes the temp `dist/`/`build/`. FastAPI serves the built UI; the launcher opens a desktop window | User must just double-click; some want "no install", some want a real installer. Two `.exe`s cover both, the user only needs the `.exe`(s), and after a build the repo root holds exactly `build.ps1` + the two `.exe`s | Two processes the user manages (rejected: bad UX); browser tab as the *primary* window (kept only as the last fallback); one big folder only (kept as the installer's payload — faster cold start than one-file) | `build.ps1` is the public recipe (reproducible build); auto-update / code-signing later; the window is `shell/`-local (pywebview → Chrome/Edge `--app` → browser tab) |
| D3 | **Flat, fully dynamic tags** (name, colour, icon-or-image, description, order); multi-tag per Asset; managed entirely in the UI | The user's explicit choice — "just tags, fully dynamic, the user builds everything"; simplest mental model | Categories+values with single/multi-select; hierarchical tags (both rejected per the user) | Optional, *non-breaking* visual grouping ("tag groups") could be layered on later; nothing forces it |
| D4 | **No first-class "status" field**; use tags + saved views; derive "new" from `last_opened_at IS NULL` | Consistent with D3 ("everything is a tag"); avoids a redundant enum | A stored status enum with configurable values | Add a real status concept later if needed — additive |
| D5 | **General + timestamped notes**; timestamped notes can carry tags | Game review needs notes pinned to moments; tagging a moment ("0:34 — ammo mistake") is valuable | General-note-only (rejected — too coarse for analysis) | Note threads/replies, per-note attachments — additive |
| D6 | **References** = `from→to` + optional (user-editable) relation type + free-text label + optional timestamps on both sides + note; bidirectional | Supports "better version of", "same mistake", "compare 0:31 with 0:44" workflows; relation types stay user-defined (not hardcoded) | Plain links with no type; rigid built-in relation types | New relation behaviours (e.g. transitive groups, "playlists") — additive |
| D7 | **Content-hash identity** with a pluggable `IdentityStrategy`; v1 = `CompositeHash` (partial → full BLAKE3) + `(path,size,mtime)` cache | Files move/rename/copy across drives; identity must follow content; partial-first keeps scans fast, full-hash confirm keeps it correct | Path-based identity (rejected — fragile); full-hash-only (wasteful on big libraries); partial-hash-only (collision risk) | Add `PerceptualVideoHash` (re-encode/trim resistant), embedded-id/sidecar — register & select in config |
| D8 | "video" is a **`MediaTypeProvider`**, registered like a plugin; core/tagging/notes/refs are media-type-agnostic | The user wants other media kinds later without rewriting the core | A video-specific app (rejected — exactly what we're told to avoid) | New media types = new `MediaTypeProvider`s; everything else unchanged |
| D9 | ffmpeg/ffprobe: the **installer bundles** a standalone static build (BtbN win64 GPL → `{app}\bin\`); the **portable `.exe` downloads it on demand** (first-run prompt / Settings → FFmpeg → `%APPDATA%\Clippycap\bin\`); the resolved paths live in a mutable `FfmpegToolsHolder` so an install takes effect with no restart; `media.ffmpeg.enabled = false` → browser-based fallback; `THIRD_PARTY_NOTICES.txt` carries the GPL attribution + source links | An installed app should work fully out of the box, offline, no second download — that's worth ~85 MB of the ~104 MB installer (GPL ffmpeg). The portable `.exe` is "grab-and-go", so it stays ~20 MB and downloads ffmpeg on demand rather than ship a ~190 MB one-file exe that unpacks itself every launch. Either way ffmpeg is "have it / install it; if I say no, work without it" | Bundle in *both* (rejected — the portable's per-launch unpack of ~190 MB is bad); bundle in *neither* (rejected — the user wanted the installer to "just work"); hard dependency / no escape hatch (rejected) | Other extractors/thumbnailers per media type (e.g. Pillow for images); the holder pattern generalises to any swappable external tool |
| D14 | **`FfmpegToolsHolder` / `ConfigHolder`**: long-lived services (the thumbnailer, metadata extractor, video editor) read external-tool paths and `[editing]`/`[player]`/keybindings through a *mutable holder*, not a captured value | Settings edits (`PUT /api/config`) and an on-demand ffmpeg install must take effect on the *next* call, not the next launch — the user explicitly disliked "restart to apply" | Restart-to-apply (rejected per the user); a global mutable `Config` (rejected — the held value stays immutable; only the reference is swapped) | Any future "applies live" knob: add it to a `_Section`, read it via the holder |
| D15 | **Reproducible build script** (`build.ps1` at the repo root = npm build → PyInstaller spec → optional Inno Setup → move the `.exe`s to the repo root, clean `dist/`/`build/`) committed alongside the spec/`.iss` | The user: a wary user must be able to rebuild the released `.exe`s from source rather than trust a prebuilt binary; and the repo root should stay tidy | "Just download the `.exe`" only (rejected per the user); leaving outputs in `dist/` (rejected — the user wanted them at the root, nothing else extra); a CI-only build (fine, but the script is the source of truth either way) | Add code-signing, a Linux/macOS variant, a CI job — all wrap the same script |
| D16 | **Desktop window = pywebview (WebView2), frameless; the app's own `<header>` IS the title bar** — `shell/cli.py` `webview.create_window(html=splash, frameless=True, easy_drag=False, js_api=_WindowApi())` then `webview.start(gui="edgechromium")`; the SPA's `<header>` (logo, search, sort, a flexible `.pywebview-drag-region` strip, Scan/Tags/Settings) ends with a `<WindowControls>` (min/max/close → `window.pywebview.api.*`) shown only when `window.pywebview` exists — *no separate title-bar strip*. pywebview's drag handler drags from anything inside a `.pywebview-drag-region` element (no form-control exclusion), so the interactive bits are siblings — not children — of the drag regions. Window *size* (not position) → `[shell].window_width/height`; single-instance lock on `<data_dir>/.lock`; instant splash; falls back to a chromeless Chrome/Edge `--app` window → a browser tab if WebView2/pywebview is absent (the installer offers to install the Edge WebView2 Runtime). Forced Python down to **3.13** (pywebview's `pythonnet` has no 3.14 wheel; nothing in the code is 3.14-specific) | The `--app` window looked like a browser (Chrome's title bar, no app icon, no splash) — *and a separate custom title-bar strip above the app's header read as "browser chrome + a page"*, hence one unified bar. A real frameless window + an integrated title bar + an instant splash is the bulk of "looks like a native app", at low cost, one-process / one-language | A separate title-bar strip (rejected — looked like browser chrome); Electron sidecar (rejected for now — ~200 MB, Node toolchain, two-process orphan-backend class of bug); Tauri sidecar (rejected for now — Rust toolchain, can't compile-test here, two-process; revisit if auto-update/tray become must-haves); a hand-rolled ctypes WebView2 host (rejected — bug-prone); Qt WebEngine (rejected — bundles Chromium, huge) | Auto-update / tray later; or swap the `shell/` adapter to Tauri/Electron if the project outgrows pywebview — the FastAPI backend + SQLite + Svelte UI are untouched |
| D10 | **SQLite** (one file, FTS5) as the store; JSON export; sidecars only as an export format | Needs indexed queries (filter/search/graph); must work from one `.exe`; trivial backup | Per-file JSON sidecars as primary store (no cross-item queries, heavy I/O); a server DB (operational overhead, no benefit here) | Schema migrations; later a sync plugin; alternate `ExportProvider`s |
| D11 | **Config as data**, layered (`default.toml` → `local.toml` → env), Pydantic-validated, missing key = hard error, path `@tokens` | The user: "nothing hardcoded, not even a code-level `.env` fallback" | Code-level defaults/fallbacks (rejected per the user) | New settings = new keys in `default.toml` + schema; no code branching on "is it set" |
| D12 | **Svelte + Vite** for the frontend; UI panels via a `UiPanelRegistry` | Small bundle, little boilerplate, clean component model → cheap "registered panels"; the user picked it | Vanilla + Web Components (more plumbing); React/Vue (more boilerplate/bundle) | Plugins add detail tabs / sidebar sections via the registry |
| D13 | All code & docs in **English** | The user's explicit choice | Turkish docs / mixed (rejected per the user) | i18n seam for UI strings later if wanted |
| D17 | **Scan = a two-phase background job** — *discovery* (`LibraryScanner`: walk + `blake3-composite` identity + `quick_metadata`, committed every `[scan].commit_batch_size` files) then *enrichment* (`MetadataEnricher`: ffprobe duration/resolution, committed per batch); the UI live-refreshes the grid throughout. The default identity became **`blake3-composite`** (`BLAKE3(size ‖ head ‖ tail)`, ~128 KB read/file); whole-file `blake3` stays selectable. A strategy change re-identifies the library once via a background `IdentityUpgrader`; new assets carry a `metadata_pending` flag until enriched | A real user's first scan of ~2400 HDD clips was slow and showed *nothing* until it finished — one transaction, full-file hashing of ~53 GB, an inline ffprobe per file. Reading only the file ends, committing in batches, and deferring ffprobe makes the library appear in seconds and stay usable while the rest streams in — the modern photo/video-manager model. Refines D7 | Keep it one blocking pass (rejected — the reported bug); full-file hash as the default (rejected — the dominant cost on a spinning disk); a perceptual hash (heavier — still a future plugin); a SQL migration for the re-identify (rejected — a migration can't see the runtime-configured head/tail sizes; an app-level background job can) | `metadata_pending` + `MetadataEnricher` generalise to any media type's deep metadata; `IdentityUpgrader` re-runs for any future strategy change; `commit_batch_size` tunes the streaming granularity |

---

## 15. Roadmap (phased TODO)

> Tracked here; checked off as built. Detailed task list also kept in sync with the working plan.

- **Phase 0 — skeleton & contracts** *(done):* repo + structure + this doc · `config/default.toml` + Pydantic schema + layered loader + "missing key = error" + first-run `local.toml` · `core/` entities, value objects, interfaces, `EventBus` + tests · `plugins_runtime/` registries + plugin discovery.
- **Phase 1 — data & scanning** *(done):* SQLite schema + migrations + repos + FTS5 · **two-phase scan** — `LibraryScanner` discovery (walker + `blake3-composite`/`blake3` `IdentityStrategy` + `HashCache` + skip-recent + volume/file-id de-dup + batched commits) then `MetadataEnricher` (background duration/resolution) · `LibraryReconciler` (hashing-free rename/move re-sync) · `IdentityUpgrader` (one-time strategy-change re-identify) · `media_types/video` + the holder-backed ffmpeg adapters (thumbnailer / metadata extractor / video editor) + filename `recorded_at` parsing · `ScanService`, `AssetService` + integration tests (rename/move/missing/copy, composite hashing, discovery + enrichment).
- **Phase 2 — API** *(done):* routers (assets/tags/notes/references/reference-types/sources/saved-views/scan/jobs/config/**ffmpeg**/health) · `/media/{id}/stream` with HTTP Range · `/thumbnails/{id}` (serve-or-lazily-generate, 503-`ffmpeg_unavailable` when no ffmpeg) · `/api/ffmpeg/{install,path,auto,dismiss-prompt}` (on-demand install as a background job, set/reset a custom path, dismiss the first-run prompt) · `PUT /api/config` (hot-reload via `ConfigHolder`) · `ThreadJobQueue`.
- **Phase 3 — frontend** *(mostly done):* Vite + Svelte 5 SPA served by FastAPI · Library view (sidebar quick views + flat tag cloud + sources + saved views; asset grid with multi-select + bulk ops) · Detail view (rich analysis player + draggable timeline with IN/OUT and note markers + frame stepping + keyboard map) · Tags panel + per-note tags · References panel (two sections, ref-cards, free-text descriptions, manual + @-mention auto-references for moments) · Settings (Editing/Player/Keyboard/FFmpeg tabs, hot-reload via `ConfigHolder`) · Tag Manager modal (flat list, colour, icon/emoji or uploaded image, asset counts) · in-app dialogs + toasts (replacing native browser ones) · hash-based URL routing · **the app's `<header>` is the frameless pywebview window's title bar** (logo, search, sort, drag region, Scan/Tags/Settings, `<WindowControls>`). Open: split `App.svelte` into components, A-B loop & frame export, drag-reorder for tags, fix the suppressed a11y warnings.
- **Phase 4 — extensibility, packaging, polish** *(mostly done):* `build.ps1` at the repo root (npm build → PyInstaller portable one-file + one-folder build → Inno Setup wraps the one-folder build + bundles ffmpeg → moves both `.exe`s into the repo root, cleans up `dist/`+`build/`) · on-demand ffmpeg for the portable (download / use-existing / first-run prompt / Settings tab) · `THIRD_PARTY_NOTICES.txt` (GPL ffmpeg attribution) · pywebview frameless window with our HTML title bar, splash, single-instance lock, window-size persistence · the installer offers to install the Edge WebView2 Runtime if missing · app icon (`packaging/clippycap.ico`, exe + window + installer + favicon). Open: **JSON export/import** (`app/export.py`, `/api/export`+`/api/import`) · **a sample plugin** in `plugins/` proving the extension path · more pytest coverage for the newer flows (ffmpeg service end-to-end, references, @-mentions, the pywebview shell) · maybe an auto-update check.
- **Ongoing:** update `ARCHITECTURE.md` on every design change; keep `core/`/`app/` tested; refactor duplication into shared helpers.
