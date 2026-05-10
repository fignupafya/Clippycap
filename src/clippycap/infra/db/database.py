"""The SQLite :class:`~clippycap.core.ports.Database` and :class:`~clippycap.core.ports.UnitOfWork`.

Each :meth:`SqliteDatabase.transaction` opens a fresh connection (foreign keys on, WAL journal),
wraps every repository on it, commits on a clean exit and rolls back on an exception.
:meth:`SqliteDatabase.initialise` creates / migrates the schema (idempotent).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from clippycap.core.ports import (
    AssetRepository,
    HashCacheStore,
    MetaStore,
    NoteRepository,
    ReferenceRepository,
    ReferenceTypeRepository,
    SavedViewRepository,
    SourceRepository,
    TagRepository,
)
from clippycap.infra.db import repositories as repos
from clippycap.infra.db.schema import MIGRATIONS


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


class SqliteUnitOfWork:
    """All repositories sharing one :class:`sqlite3.Connection` / transaction."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.assets: AssetRepository = repos.SqliteAssetRepository(conn)
        self.tags: TagRepository = repos.SqliteTagRepository(conn)
        self.notes: NoteRepository = repos.SqliteNoteRepository(conn)
        self.reference_types: ReferenceTypeRepository = repos.SqliteReferenceTypeRepository(conn)
        self.references: ReferenceRepository = repos.SqliteReferenceRepository(conn)
        self.sources: SourceRepository = repos.SqliteSourceRepository(conn)
        self.saved_views: SavedViewRepository = repos.SqliteSavedViewRepository(conn)
        self.hash_cache: HashCacheStore = repos.SqliteHashCacheStore(conn)
        self.meta: MetaStore = repos.SqliteMetaStore(conn)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()


class SqliteDatabase:
    """A file-backed SQLite database. Construct with a path; call :meth:`initialise` once at startup."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def initialise(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = _connect(self._path)
        try:
            current = int(conn.execute("PRAGMA user_version").fetchone()[0])
            for version, sql in MIGRATIONS:
                if version > current:
                    conn.executescript(sql)
                    conn.execute(f"PRAGMA user_version = {version}")
            conn.commit()
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[SqliteUnitOfWork]:
        conn = _connect(self._path)
        try:
            yield SqliteUnitOfWork(conn)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
