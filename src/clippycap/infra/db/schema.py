"""The SQLite schema and the forward-only migration list.

Migrations run in order at startup; ``PRAGMA user_version`` records how far we have got. Evolving
the schema later means *appending* a new ``(version, sql)`` entry -- never editing an old one.
"""

from __future__ import annotations

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

MIGRATIONS: tuple[tuple[int, str], ...] = ((1, _SCHEMA_V1),)
LATEST_VERSION: int = MIGRATIONS[-1][0]
