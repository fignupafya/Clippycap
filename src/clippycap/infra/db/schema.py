"""The SQLite schema and the forward-only migration list.

Migrations run in order at startup; ``PRAGMA user_version`` records how far we have got. Evolving
the schema later means *appending* a new ``(version, step)`` entry -- never editing an old one. A
step is either a SQL string (run with ``executescript``) or a callable ``(connection) -> None`` for
data migrations that need real logic (timezone conversion, parsing, ...). Migration callables are
kept *self-contained* -- they must not import app code that could change underneath them.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import PureWindowsPath

_SCHEMA_V1 = """
CREATE TABLE app_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE assets (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    identity_hash  TEXT NOT NULL UNIQUE,
    media_type     TEXT NOT NULL,
    title          TEXT NOT NULL,
    size_bytes     INTEGER NOT NULL,
    metadata_json  TEXT NOT NULL DEFAULT '{}',
    added_at       TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    last_opened_at TEXT,
    missing        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_assets_media_type ON assets(media_type);
CREATE INDEX idx_assets_missing    ON assets(missing);

CREATE TABLE asset_paths (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id      INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    path          TEXT NOT NULL UNIQUE,
    volume_id     TEXT,
    present       INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL
);
CREATE INDEX idx_asset_paths_asset   ON asset_paths(asset_id);
CREATE INDEX idx_asset_paths_present ON asset_paths(present);

CREATE TABLE tags (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    color       TEXT NOT NULL,
    icon        TEXT,
    image_ref   TEXT,
    description TEXT NOT NULL DEFAULT '',
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_tags_sort ON tags(sort_order, name);

CREATE TABLE asset_tags (
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    tag_id   INTEGER NOT NULL REFERENCES tags(id)   ON DELETE CASCADE,
    added_at TEXT NOT NULL,
    PRIMARY KEY (asset_id, tag_id)
);
CREATE INDEX idx_asset_tags_tag ON asset_tags(tag_id);

CREATE TABLE notes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id     INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    body         TEXT NOT NULL DEFAULT '',
    timestamp_ms INTEGER,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX idx_notes_asset ON notes(asset_id, timestamp_ms);
CREATE UNIQUE INDEX idx_notes_one_general ON notes(asset_id) WHERE timestamp_ms IS NULL;

CREATE TABLE note_tags (
    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    tag_id  INTEGER NOT NULL REFERENCES tags(id)  ON DELETE CASCADE,
    PRIMARY KEY (note_id, tag_id)
);

CREATE TABLE reference_types (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    reverse_name TEXT,
    color        TEXT NOT NULL,
    sort_order   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE asset_references (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    from_asset_id     INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    to_asset_id       INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    type_id           INTEGER REFERENCES reference_types(id) ON DELETE SET NULL,
    label             TEXT NOT NULL DEFAULT '',
    from_timestamp_ms INTEGER,
    to_timestamp_ms   INTEGER,
    note              TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL
);
CREATE INDEX idx_asset_references_from ON asset_references(from_asset_id);
CREATE INDEX idx_asset_references_to   ON asset_references(to_asset_id);

CREATE TABLE sources (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    path             TEXT NOT NULL UNIQUE,
    recursive        INTEGER NOT NULL DEFAULT 1,
    enabled          INTEGER NOT NULL DEFAULT 1,
    media_types_json TEXT NOT NULL DEFAULT '[]',
    last_scanned_at  TEXT
);

CREATE TABLE saved_views (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    filter_json TEXT NOT NULL,
    sort_key    TEXT NOT NULL,
    sort_order  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE hash_cache (
    path          TEXT PRIMARY KEY,
    size          INTEGER NOT NULL,
    mtime_ns      INTEGER NOT NULL,
    identity_hash TEXT NOT NULL
);

CREATE VIRTUAL TABLE search_index USING fts5(kind UNINDEXED, ref_id UNINDEXED, text);

CREATE TRIGGER trg_assets_search_ai AFTER INSERT ON assets BEGIN
    INSERT INTO search_index(kind, ref_id, text) VALUES ('asset', new.id, new.title);
END;
CREATE TRIGGER trg_assets_search_au AFTER UPDATE OF title ON assets BEGIN
    DELETE FROM search_index WHERE kind = 'asset' AND ref_id = old.id;
    INSERT INTO search_index(kind, ref_id, text) VALUES ('asset', new.id, new.title);
END;
CREATE TRIGGER trg_assets_search_ad AFTER DELETE ON assets BEGIN
    DELETE FROM search_index WHERE kind = 'asset' AND ref_id = old.id;
END;
CREATE TRIGGER trg_notes_search_ai AFTER INSERT ON notes BEGIN
    INSERT INTO search_index(kind, ref_id, text) VALUES ('note', new.id, new.body);
END;
CREATE TRIGGER trg_notes_search_au AFTER UPDATE OF body ON notes BEGIN
    DELETE FROM search_index WHERE kind = 'note' AND ref_id = old.id;
    INSERT INTO search_index(kind, ref_id, text) VALUES ('note', new.id, new.body);
END;
CREATE TRIGGER trg_notes_search_ad AFTER DELETE ON notes BEGIN
    DELETE FROM search_index WHERE kind = 'note' AND ref_id = old.id;
END;
"""

# v2: a timestamped note may span an interval (end_timestamp_ms), not just a single moment.
_MIGRATION_V2 = "ALTER TABLE notes ADD COLUMN end_timestamp_ms INTEGER;"

# v3: index the recorded_at sort expression -- the default library sort orders by it, and the grid
# is paginated, so this keeps ORDER BY ... LIMIT cheap as the library grows.
_MIGRATION_V3 = (
    "CREATE INDEX IF NOT EXISTS idx_assets_recorded "
    "ON assets(json_extract(metadata_json, '$.recorded_at'));"
)


def _canonical_recorded_at(raw: str) -> str | None:
    """Reformat a stored recorded_at to canonical naive-local ``YYYY-MM-DDTHH:MM:SS``; ``None`` if
    it cannot be parsed. Self-contained (see the module docstring) -- a frozen copy of the rule."""
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if parsed.tzinfo is not None:                       # UTC / offset -> machine-local wall clock
        try:
            parsed = parsed.astimezone().replace(tzinfo=None)
        except (OSError, OverflowError, ValueError):
            return None                                 # pre-epoch sentinel the OS can't convert
    return parsed.strftime("%Y-%m-%dT%H:%M:%S")


def _migration_v4_normalize_recorded_at(conn: sqlite3.Connection) -> None:
    """Rewrite every asset's ``metadata_json.recorded_at`` to one canonical naive-local format.

    Older builds stored three inconsistent shapes -- naive-local parsed from an OBS file name, UTC
    ``...Z`` from ffprobe, UTC ``...+00:00`` from the file mtime -- so the string-based date sort was
    wrong wherever the shapes were mixed (a library of only OBS-named clips was unaffected). This
    fixes existing rows in place; new scans already write the canonical form."""
    rows = conn.execute("SELECT id, metadata_json FROM assets").fetchall()
    for row in rows:
        try:
            meta = json.loads(row["metadata_json"])
        except (ValueError, TypeError):
            continue
        if not isinstance(meta, dict) or not isinstance(meta.get("recorded_at"), str):
            continue
        raw = meta["recorded_at"]
        canonical = _canonical_recorded_at(raw)
        if canonical == raw:
            continue
        if canonical is None:
            meta.pop("recorded_at", None)               # unparseable -> drop, don't sort on junk
        else:
            meta["recorded_at"] = canonical
        conn.execute("UPDATE assets SET metadata_json = ? WHERE id = ?", (json.dumps(meta), row["id"]))


# v5: index hash_cache by (size, mtime_ns) so the background reconciler can match a renamed file to
# its old entry without re-hashing -- see infra/scan/reconciler.py.
_MIGRATION_V5 = "CREATE INDEX IF NOT EXISTS idx_hash_cache_sig ON hash_cache(size, mtime_ns);"


def _migration_v6_titles_from_filenames(conn: sqlite3.Connection) -> None:
    """Re-derive every asset's title from its file name. Older builds set the title to the recording
    timestamp; the title is now simply the file's own name (the recording time is shown separately
    from ``recorded_at``). Picks the asset's present path, else its first-seen one."""
    rows = conn.execute(
        "SELECT a.id AS id, ("
        " SELECT p.path FROM asset_paths p WHERE p.asset_id = a.id "
        " ORDER BY p.present DESC, p.id ASC LIMIT 1) AS path "
        "FROM assets a"
    ).fetchall()
    for row in rows:
        if not row["path"]:
            continue
        stem = PureWindowsPath(row["path"]).stem
        if stem:
            conn.execute("UPDATE assets SET title = ? WHERE id = ?", (stem, row["id"]))


# v7: the two-phase scan. Discovery records an asset fast; a background enrichment pass then reads
# its duration / resolution into metadata_json and clears `metadata_pending`. Existing assets
# already have their metadata, so the column defaults to 0 (not pending). The partial index keeps
# "find the assets still awaiting enrichment" instant however large the library grows.
_MIGRATION_V7 = (
    "ALTER TABLE assets ADD COLUMN metadata_pending INTEGER NOT NULL DEFAULT 0;\n"
    "CREATE INDEX idx_assets_metadata_pending ON assets(metadata_pending) WHERE metadata_pending = 1;"
)


# v8: user-defined tag groups (categories) + per-tag/per-group "has its own page" opt-in + a
# per-tag free-form notes body. All additive and OFF by default, so every existing tag keeps
# behaving exactly as before: group_id NULL = uncategorised, has_page 0 = no page, notes '' = empty.
# (No groups are seeded -- categories are entirely user-created; nothing is hardcoded.) Deleting a
# group sets its tags' group_id back to NULL (ON DELETE SET NULL), so tags are never lost with it.
_MIGRATION_V8 = (
    "CREATE TABLE tag_groups ("
    " id         INTEGER PRIMARY KEY AUTOINCREMENT,"
    " name       TEXT NOT NULL UNIQUE,"
    " color      TEXT NOT NULL DEFAULT '',"
    " sort_order INTEGER NOT NULL DEFAULT 0,"
    " has_page   INTEGER NOT NULL DEFAULT 0"
    ");\n"
    "ALTER TABLE tags ADD COLUMN group_id INTEGER REFERENCES tag_groups(id) ON DELETE SET NULL;\n"
    "ALTER TABLE tags ADD COLUMN has_page INTEGER NOT NULL DEFAULT 0;\n"
    "ALTER TABLE tags ADD COLUMN notes TEXT NOT NULL DEFAULT '';\n"
    "CREATE INDEX idx_tags_group ON tags(group_id);"
)


# v9: categories become navigable, editable, nestable. `parent_id` lets a category sit under another
# (e.g. a per-player category under a "Players" umbrella) -- ON DELETE SET NULL so deleting a parent
# just promotes its children to top-level rather than destroying them. `notes` gives a category its
# own editable page body (the same free-form write-up tags already have). Both additive + empty by
# default, so existing flat categories are unaffected.
_MIGRATION_V9 = (
    "ALTER TABLE tag_groups ADD COLUMN parent_id INTEGER REFERENCES tag_groups(id) ON DELETE SET NULL;\n"
    "ALTER TABLE tag_groups ADD COLUMN notes TEXT NOT NULL DEFAULT '';\n"
    "CREATE INDEX idx_tag_groups_parent ON tag_groups(parent_id);"
)


# v10: DIRECT clip<->category membership. A clip can belong to a category WITHOUT carrying a tag in
# it (the previous, tag-derived membership still applies on top). Cascades on both sides so deleting
# a clip or a category cleans up its rows.
_MIGRATION_V10 = (
    "CREATE TABLE asset_categories ("
    " asset_id    INTEGER NOT NULL REFERENCES assets(id)     ON DELETE CASCADE,"
    " category_id INTEGER NOT NULL REFERENCES tag_groups(id) ON DELETE CASCADE,"
    " added_at    TEXT NOT NULL,"
    " PRIMARY KEY (asset_id, category_id)"
    ");\n"
    "CREATE INDEX idx_asset_categories_cat ON asset_categories(category_id);"
)


# v11: LINKERS -- user-defined rules that auto-attach companion files (demos, scripts, transcripts,
# RAWs, ...) to assets. A linker's whole rule (scopes / field extraction / transforms / match
# predicates / resolution policy / open-with actions) is a versioned JSON blob (`definition_json`),
# so the rule language can evolve without a migration per tweak and linkers export/import as JSON.
# `asset_attachments` holds the resolved links; `attachment_overrides` are the manual pin/exclude
# tombstones that survive every re-run; `linker_file_cache` makes incremental runs cheap. All cascade
# on asset/linker deletion. Entirely additive -- nothing here touches existing tables.
_MIGRATION_V11 = """
CREATE TABLE linkers (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL UNIQUE,
    description    TEXT NOT NULL DEFAULT '',
    color          TEXT NOT NULL DEFAULT '',
    enabled        INTEGER NOT NULL DEFAULT 0,
    sort_order     INTEGER NOT NULL DEFAULT 0,
    schema_version INTEGER NOT NULL DEFAULT 1,
    definition_json TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE asset_attachments (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id         INTEGER NOT NULL REFERENCES assets(id)   ON DELETE CASCADE,
    linker_id        INTEGER NOT NULL REFERENCES linkers(id)  ON DELETE CASCADE,
    path             TEXT NOT NULL,
    label            TEXT NOT NULL DEFAULT '',
    ext              TEXT NOT NULL DEFAULT '',
    score            REAL NOT NULL DEFAULT 0,
    matched_json     TEXT NOT NULL DEFAULT '{}',
    status           TEXT NOT NULL DEFAULT 'linked',   -- linked | missing
    origin           TEXT NOT NULL DEFAULT 'auto',     -- auto | manual (a pinned file)
    size             INTEGER,
    mtime_ns         INTEGER,
    created_at       TEXT NOT NULL,
    last_verified_at TEXT,
    UNIQUE (asset_id, linker_id, path)
);
CREATE INDEX idx_attachments_asset  ON asset_attachments(asset_id);
CREATE INDEX idx_attachments_linker ON asset_attachments(linker_id);

CREATE TABLE attachment_overrides (
    asset_id   INTEGER NOT NULL REFERENCES assets(id)  ON DELETE CASCADE,
    linker_id  INTEGER NOT NULL REFERENCES linkers(id) ON DELETE CASCADE,
    path       TEXT NOT NULL,
    decision   TEXT NOT NULL,                          -- pin | exclude
    created_at TEXT NOT NULL,
    PRIMARY KEY (asset_id, linker_id, path)
);

CREATE TABLE linker_file_cache (
    linker_id   INTEGER NOT NULL REFERENCES linkers(id) ON DELETE CASCADE,
    path        TEXT NOT NULL,
    size        INTEGER NOT NULL,
    mtime_ns    INTEGER NOT NULL,
    fields_json TEXT NOT NULL,
    PRIMARY KEY (linker_id, path)
);
"""


# A migration step is SQL (run with executescript) or a callable for data migrations needing logic.
MigrationStep = str | Callable[[sqlite3.Connection], None]

MIGRATIONS: tuple[tuple[int, MigrationStep], ...] = (
    (1, _SCHEMA_V1),
    (2, _MIGRATION_V2),
    (3, _MIGRATION_V3),
    (4, _migration_v4_normalize_recorded_at),
    (5, _MIGRATION_V5),
    (6, _migration_v6_titles_from_filenames),
    (7, _MIGRATION_V7),
    (8, _MIGRATION_V8),
    (9, _MIGRATION_V9),
    (10, _MIGRATION_V10),
    (11, _MIGRATION_V11),
)
LATEST_VERSION: int = MIGRATIONS[-1][0]
