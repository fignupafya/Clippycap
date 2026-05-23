"""SQLite implementations of the persistence ports.

Helpers (time/JSON conversion, row mapping, the sort-key map) come first; then one repository
class per port. Each class takes a :class:`sqlite3.Connection` and runs parametrised SQL.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from clippycap.core.entities import (
    Asset,
    AssetPath,
    Note,
    Reference,
    ReferenceType,
    SavedView,
    Source,
    Tag,
)
from clippycap.core.errors import ConflictError, InvalidInputError
from clippycap.core.query import AssetFilter

# --------------------------------------------------------------------------- helpers


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _from_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _iso_or_none(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _fts_query(text: str) -> str:
    """A user search string -> a safe FTS5 query (each whitespace word as a quoted literal, AND-ed)."""
    return " AND ".join('"' + word.replace('"', '""') + '"' for word in text.split())


def _is_under(path: str, root: str) -> bool:
    return path == root or path.startswith(root + "\\") or path.startswith(root + "/")


# ORDER BY clauses, one per sort key. Assets whose recorded_at / duration is unknown (NULL) always
# sort to the END: SQLite's DESC already puts NULLs last, while ASC needs an explicit "(expr IS NULL)"
# leading term to force the same. `a.added_at` / `a.id` are stable tie-breakers.
_REC = "json_extract(a.metadata_json, '$.recorded_at')"
_DUR = "CAST(json_extract(a.metadata_json, '$.duration_ms') AS INTEGER)"
_SORT_CLAUSES: dict[str, str] = {
    "recorded_desc":    f"{_REC} DESC, a.added_at DESC",
    "recorded_asc":     f"({_REC} IS NULL), {_REC} ASC, a.added_at ASC",
    "added_desc":       "a.added_at DESC, a.id DESC",
    "added_asc":        "a.added_at ASC, a.id ASC",
    "duration_desc":    f"{_DUR} DESC, a.added_at DESC",
    "duration_asc":     f"({_DUR} IS NULL), {_DUR} ASC, a.added_at ASC",
    "title_asc":        "a.title COLLATE NOCASE ASC, a.id ASC",
    "tag_count_desc":   "(SELECT COUNT(*) FROM asset_tags z WHERE z.asset_id = a.id) DESC, a.added_at DESC",
    "last_opened_desc": "a.last_opened_at DESC, a.added_at DESC",
}


def _asset(r: sqlite3.Row) -> Asset:
    return Asset(
        identity_hash=r["identity_hash"],
        media_type=r["media_type"],
        title=r["title"],
        size_bytes=r["size_bytes"],
        metadata=json.loads(r["metadata_json"]),
        metadata_pending=bool(r["metadata_pending"]),
        added_at=_from_iso(r["added_at"]),
        last_seen_at=_from_iso(r["last_seen_at"]),
        last_opened_at=_from_iso(r["last_opened_at"]),
        id=r["id"],
    )


def _asset_path(r: sqlite3.Row) -> AssetPath:
    return AssetPath(
        asset_id=r["asset_id"],
        path=r["path"],
        volume_id=r["volume_id"],
        present=bool(r["present"]),
        first_seen_at=_from_iso(r["first_seen_at"]),
        last_seen_at=_from_iso(r["last_seen_at"]),
        id=r["id"],
    )


def _tag(r: sqlite3.Row) -> Tag:
    return Tag(
        name=r["name"],
        color=r["color"],
        icon=r["icon"],
        image_ref=r["image_ref"],
        description=r["description"],
        sort_order=r["sort_order"],
        created_at=_from_iso(r["created_at"]),
        id=r["id"],
    )


def _note(r: sqlite3.Row) -> Note:
    return Note(
        asset_id=r["asset_id"],
        body=r["body"],
        timestamp_ms=r["timestamp_ms"],
        end_timestamp_ms=r["end_timestamp_ms"],
        created_at=_from_iso(r["created_at"]),
        updated_at=_from_iso(r["updated_at"]),
        id=r["id"],
    )


def _reference_type(r: sqlite3.Row) -> ReferenceType:
    return ReferenceType(
        name=r["name"], color=r["color"], reverse_name=r["reverse_name"], sort_order=r["sort_order"], id=r["id"]
    )


def _reference(r: sqlite3.Row) -> Reference:
    return Reference(
        from_asset_id=r["from_asset_id"],
        to_asset_id=r["to_asset_id"],
        type_id=r["type_id"],
        label=r["label"],
        from_timestamp_ms=r["from_timestamp_ms"],
        to_timestamp_ms=r["to_timestamp_ms"],
        note=r["note"],
        created_at=_from_iso(r["created_at"]),
        id=r["id"],
    )


def _source(r: sqlite3.Row) -> Source:
    return Source(
        path=r["path"],
        recursive=bool(r["recursive"]),
        enabled=bool(r["enabled"]),
        media_types=list(json.loads(r["media_types_json"])),
        last_scanned_at=_from_iso(r["last_scanned_at"]),
        id=r["id"],
    )


def _saved_view(r: sqlite3.Row) -> SavedView:
    return SavedView(
        name=r["name"], filter_json=r["filter_json"], sort_key=r["sort_key"], sort_order=r["sort_order"], id=r["id"]
    )


class _Repo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._c = conn


# --------------------------------------------------------------------------- assets


class SqliteAssetRepository(_Repo):
    def add(self, asset: Asset) -> Asset:
        added = _now()
        try:
            cur = self._c.execute(
                "INSERT INTO assets(identity_hash, media_type, title, size_bytes, metadata_json, "
                "added_at, last_seen_at, last_opened_at, missing, metadata_pending) "
                "VALUES (?,?,?,?,?,?,?,NULL,0,?)",
                (asset.identity_hash, asset.media_type, asset.title, asset.size_bytes,
                 json.dumps(asset.metadata), added, added, 1 if asset.metadata_pending else 0),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"an asset with identity {asset.identity_hash!r} already exists") from exc
        asset.id = cur.lastrowid
        asset.added_at = _from_iso(added)
        asset.last_seen_at = _from_iso(added)
        return asset

    def get(self, asset_id: int) -> Asset | None:
        row = self._c.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        return _asset(row) if row else None

    def get_by_hash(self, identity_hash: str) -> Asset | None:
        row = self._c.execute("SELECT * FROM assets WHERE identity_hash = ?", (identity_hash,)).fetchone()
        return _asset(row) if row else None

    def update(self, asset: Asset) -> None:
        try:
            self._c.execute(
                "UPDATE assets SET identity_hash=?, media_type=?, title=?, size_bytes=?, metadata_json=?, "
                "last_opened_at=?, metadata_pending=? WHERE id=?",
                (asset.identity_hash, asset.media_type, asset.title, asset.size_bytes,
                 json.dumps(asset.metadata), _iso_or_none(asset.last_opened_at),
                 1 if asset.metadata_pending else 0, asset.id),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"another asset already has identity {asset.identity_hash!r}") from exc

    def delete(self, asset_id: int) -> None:
        self._c.execute("DELETE FROM assets WHERE id = ?", (asset_id,))

    def touch_seen(self, asset_id: int) -> None:
        self._c.execute("UPDATE assets SET last_seen_at = ? WHERE id = ?", (_now(), asset_id))

    def touch_opened(self, asset_id: int) -> None:
        self._c.execute("UPDATE assets SET last_opened_at = ? WHERE id = ?", (_now(), asset_id))

    def set_missing(self, asset_id: int, missing: bool) -> None:
        self._c.execute("UPDATE assets SET missing = ? WHERE id = ?", (1 if missing else 0, asset_id))

    def search(
        self, *, filter: AssetFilter, sort_key: str, offset: int, limit: int
    ) -> tuple[list[Asset], int]:
        order = _SORT_CLAUSES.get(sort_key)
        if order is None:
            raise InvalidInputError(f"unknown sort key {sort_key!r}")
        where: list[str] = []
        params: list[Any] = []
        f = filter
        if f.media_type is not None:
            where.append("a.media_type = ?")
            params.append(f.media_type)
        for tag_id in f.tags_all:
            where.append("EXISTS (SELECT 1 FROM asset_tags x WHERE x.asset_id = a.id AND x.tag_id = ?)")
            params.append(tag_id)
        if f.tags_any:
            slots = ",".join("?" * len(f.tags_any))
            where.append(f"EXISTS (SELECT 1 FROM asset_tags x WHERE x.asset_id = a.id AND x.tag_id IN ({slots}))")
            params.extend(f.tags_any)
        for tag_id in f.tags_none:
            where.append("NOT EXISTS (SELECT 1 FROM asset_tags x WHERE x.asset_id = a.id AND x.tag_id = ?)")
            params.append(tag_id)
        if f.untagged_only:
            where.append("NOT EXISTS (SELECT 1 FROM asset_tags x WHERE x.asset_id = a.id)")
        if f.only_missing:
            where.append("a.missing = 1")
        if f.never_opened:
            where.append("a.last_opened_at IS NULL")
        if f.added_after is not None:
            where.append("a.added_at >= ?")
            params.append(f.added_after.isoformat())
        if f.recorded_after is not None:
            where.append("json_extract(a.metadata_json, '$.recorded_at') >= ?")
            params.append(f.recorded_after.isoformat())
        if f.recorded_before is not None:
            where.append("json_extract(a.metadata_json, '$.recorded_at') <= ?")
            params.append(f.recorded_before.isoformat())
        if f.text:
            fts = _fts_query(f.text)
            if fts:
                where.append(
                    "a.id IN (SELECT CASE WHEN kind = 'asset' THEN ref_id "
                    "ELSE (SELECT n.asset_id FROM notes n WHERE n.id = ref_id) END "
                    "FROM search_index WHERE search_index MATCH ?)"
                )
                params.append(fts)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total = int(self._c.execute(f"SELECT COUNT(*) AS n FROM assets a{clause}", params).fetchone()["n"])
        rows = self._c.execute(
            f"SELECT a.* FROM assets a{clause} ORDER BY {order} LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return [_asset(r) for r in rows], total

    def pending_metadata_ids(self) -> list[int]:
        rows = self._c.execute(
            "SELECT a.id FROM assets a WHERE a.metadata_pending = 1 AND EXISTS "
            "(SELECT 1 FROM asset_paths p WHERE p.asset_id = a.id AND p.present = 1) ORDER BY a.id"
        ).fetchall()
        return [r["id"] for r in rows]

    def asset_ids_with_foreign_identity(self, media_type: str, current_prefix: str) -> list[int]:
        # substr(hash, 1, N) is the hash's first N chars -- compared against the strategy's prefix,
        # so no LIKE wildcard escaping is needed however the prefix is spelled.
        rows = self._c.execute(
            "SELECT id FROM assets WHERE media_type = ? AND substr(identity_hash, 1, ?) <> ?",
            (media_type, len(current_prefix), current_prefix),
        ).fetchall()
        return [r["id"] for r in rows]

    # paths --------------------------------------------------------------

    def add_path(self, path: AssetPath) -> AssetPath:
        now = _now()
        cur = self._c.execute(
            "INSERT INTO asset_paths(asset_id, path, volume_id, present, first_seen_at, last_seen_at) "
            "VALUES (?,?,?,?,?,?)",
            (path.asset_id, path.path, path.volume_id, 1 if path.present else 0, now, now),
        )
        path.id = cur.lastrowid
        path.first_seen_at = _from_iso(now)
        path.last_seen_at = _from_iso(now)
        return path

    def get_paths(self, asset_id: int) -> list[AssetPath]:
        rows = self._c.execute("SELECT * FROM asset_paths WHERE asset_id = ? ORDER BY id", (asset_id,)).fetchall()
        return [_asset_path(r) for r in rows]

    def all_paths(self) -> list[AssetPath]:
        return [_asset_path(r) for r in self._c.execute("SELECT * FROM asset_paths ORDER BY id").fetchall()]

    def find_by_path(self, path: str) -> Asset | None:
        row = self._c.execute(
            "SELECT a.* FROM assets a JOIN asset_paths p ON p.asset_id = a.id WHERE p.path = ?", (path,)
        ).fetchone()
        return _asset(row) if row else None

    def rename_path(self, old_path: str, new_path: str) -> None:
        try:
            self._c.execute("UPDATE asset_paths SET path = ? WHERE path = ?", (new_path, old_path))
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"a file is already registered at {new_path!r}") from exc

    def set_path_present(self, path_id: int, present: bool) -> None:
        self._c.execute(
            "UPDATE asset_paths SET present = ? WHERE id = ?", (1 if present else 0, path_id)
        )

    def upsert_path(self, asset_id: int, path: str, volume_id: str | None) -> None:
        now = _now()
        existing = self._c.execute("SELECT id FROM asset_paths WHERE path = ?", (path,)).fetchone()
        if existing is None:
            self._c.execute(
                "INSERT INTO asset_paths(asset_id, path, volume_id, present, first_seen_at, last_seen_at) "
                "VALUES (?,?,?,1,?,?)",
                (asset_id, path, volume_id, now, now),
            )
        else:
            self._c.execute(
                "UPDATE asset_paths SET asset_id=?, volume_id=?, present=1, last_seen_at=? WHERE id=?",
                (asset_id, volume_id, now, existing["id"]),
            )

    def reconcile_paths_under(self, root: str, seen_paths: Iterable[str]) -> list[int]:
        seen = set(seen_paths)
        rows = self._c.execute("SELECT id, asset_id, path FROM asset_paths WHERE present = 1").fetchall()
        affected: set[int] = set()
        for r in rows:
            if _is_under(r["path"], root) and r["path"] not in seen:
                self._c.execute("UPDATE asset_paths SET present = 0 WHERE id = ?", (r["id"],))
                affected.add(r["asset_id"])
        return list(affected)

    def all_paths_absent(self, asset_id: int) -> bool:
        n = self._c.execute(
            "SELECT COUNT(*) AS n FROM asset_paths WHERE asset_id = ? AND present = 1", (asset_id,)
        ).fetchone()["n"]
        return int(n) == 0


# --------------------------------------------------------------------------- tags


class SqliteTagRepository(_Repo):
    def add(self, tag: Tag) -> Tag:
        created = _now()
        try:
            cur = self._c.execute(
                "INSERT INTO tags(name, color, icon, image_ref, description, sort_order, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (tag.name, tag.color, tag.icon, tag.image_ref, tag.description, tag.sort_order, created),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"a tag named {tag.name!r} already exists") from exc
        tag.id = cur.lastrowid
        tag.created_at = _from_iso(created)
        return tag

    def get(self, tag_id: int) -> Tag | None:
        row = self._c.execute("SELECT * FROM tags WHERE id = ?", (tag_id,)).fetchone()
        return _tag(row) if row else None

    def get_by_name(self, name: str) -> Tag | None:
        row = self._c.execute("SELECT * FROM tags WHERE name = ?", (name,)).fetchone()
        return _tag(row) if row else None

    def list_all(self) -> list[Tag]:
        rows = self._c.execute("SELECT * FROM tags ORDER BY sort_order, name COLLATE NOCASE").fetchall()
        return [_tag(r) for r in rows]

    def update(self, tag: Tag) -> None:
        try:
            self._c.execute(
                "UPDATE tags SET name=?, color=?, icon=?, image_ref=?, description=?, sort_order=? WHERE id=?",
                (tag.name, tag.color, tag.icon, tag.image_ref, tag.description, tag.sort_order, tag.id),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"a tag named {tag.name!r} already exists") from exc

    def delete(self, tag_id: int) -> None:
        self._c.execute("DELETE FROM tags WHERE id = ?", (tag_id,))

    def reorder(self, ordered_ids: Sequence[int]) -> None:
        for index, tag_id in enumerate(ordered_ids):
            self._c.execute("UPDATE tags SET sort_order = ? WHERE id = ?", (index, tag_id))

    def asset_count(self, tag_id: int) -> int:
        return int(self._c.execute("SELECT COUNT(*) AS n FROM asset_tags WHERE tag_id = ?", (tag_id,)).fetchone()["n"])

    def apply(self, asset_id: int, tag_id: int) -> bool:
        cur = self._c.execute(
            "INSERT OR IGNORE INTO asset_tags(asset_id, tag_id, added_at) VALUES (?,?,?)",
            (asset_id, tag_id, _now()),
        )
        return cur.rowcount > 0

    def unapply(self, asset_id: int, tag_id: int) -> bool:
        cur = self._c.execute("DELETE FROM asset_tags WHERE asset_id=? AND tag_id=?", (asset_id, tag_id))
        return cur.rowcount > 0

    def tag_ids_for_asset(self, asset_id: int) -> list[int]:
        rows = self._c.execute(
            "SELECT t.id FROM tags t JOIN asset_tags at ON at.tag_id = t.id WHERE at.asset_id = ? "
            "ORDER BY t.sort_order, t.name COLLATE NOCASE",
            (asset_id,),
        ).fetchall()
        return [r["id"] for r in rows]

    def tag_ids_for_assets(self, asset_ids: Sequence[int]) -> dict[int, list[int]]:
        result: dict[int, list[int]] = {aid: [] for aid in asset_ids}
        if not asset_ids:
            return result
        slots = ",".join("?" * len(asset_ids))
        rows = self._c.execute(
            f"SELECT at.asset_id AS aid, t.id AS tid FROM asset_tags at JOIN tags t ON t.id = at.tag_id "
            f"WHERE at.asset_id IN ({slots}) ORDER BY t.sort_order, t.name COLLATE NOCASE",
            list(asset_ids),
        ).fetchall()
        for r in rows:
            result[r["aid"]].append(r["tid"])
        return result


# --------------------------------------------------------------------------- notes


class SqliteNoteRepository(_Repo):
    def add(self, note: Note) -> Note:
        now = _now()
        try:
            cur = self._c.execute(
                "INSERT INTO notes(asset_id, body, timestamp_ms, end_timestamp_ms, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (note.asset_id, note.body, note.timestamp_ms, note.end_timestamp_ms, now, now),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("this asset already has a general note") from exc
        note.id = cur.lastrowid
        note.created_at = _from_iso(now)
        note.updated_at = _from_iso(now)
        return note

    def get(self, note_id: int) -> Note | None:
        row = self._c.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        return _note(row) if row else None

    def update(self, note: Note) -> None:
        now = _now()
        self._c.execute("UPDATE notes SET body = ?, updated_at = ? WHERE id = ?", (note.body, now, note.id))
        note.updated_at = _from_iso(now)

    def retime(self, note_id: int, timestamp_ms: int | None, end_timestamp_ms: int | None) -> None:
        self._c.execute(
            "UPDATE notes SET timestamp_ms = ?, end_timestamp_ms = ?, updated_at = ? WHERE id = ?",
            (timestamp_ms, end_timestamp_ms, _now(), note_id),
        )

    def delete(self, note_id: int) -> None:
        self._c.execute("DELETE FROM notes WHERE id = ?", (note_id,))

    def list_for_asset(self, asset_id: int) -> list[Note]:
        rows = self._c.execute(
            "SELECT * FROM notes WHERE asset_id = ? ORDER BY (timestamp_ms IS NOT NULL), timestamp_ms, id",
            (asset_id,),
        ).fetchall()
        return [_note(r) for r in rows]

    def general_note(self, asset_id: int) -> Note | None:
        row = self._c.execute(
            "SELECT * FROM notes WHERE asset_id = ? AND timestamp_ms IS NULL", (asset_id,)
        ).fetchone()
        return _note(row) if row else None

    def count_for_asset(self, asset_id: int) -> int:
        return int(self._c.execute("SELECT COUNT(*) AS n FROM notes WHERE asset_id = ?", (asset_id,)).fetchone()["n"])

    def counts_for_assets(self, asset_ids: Sequence[int]) -> dict[int, int]:
        result: dict[int, int] = {aid: 0 for aid in asset_ids}
        if not asset_ids:
            return result
        slots = ",".join("?" * len(asset_ids))
        rows = self._c.execute(
            f"SELECT asset_id, COUNT(*) AS n FROM notes WHERE asset_id IN ({slots}) GROUP BY asset_id",
            list(asset_ids),
        ).fetchall()
        for r in rows:
            result[r["asset_id"]] = int(r["n"])
        return result

    def set_tags(self, note_id: int, tag_ids: Sequence[int]) -> None:
        self._c.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
        self._c.executemany("INSERT INTO note_tags(note_id, tag_id) VALUES (?,?)", [(note_id, t) for t in tag_ids])

    def tag_ids_for_note(self, note_id: int) -> list[int]:
        rows = self._c.execute(
            "SELECT t.id FROM tags t JOIN note_tags nt ON nt.tag_id = t.id WHERE nt.note_id = ? "
            "ORDER BY t.sort_order, t.name COLLATE NOCASE",
            (note_id,),
        ).fetchall()
        return [r["id"] for r in rows]


# --------------------------------------------------------------------------- reference types


class SqliteReferenceTypeRepository(_Repo):
    def add(self, rt: ReferenceType) -> ReferenceType:
        try:
            cur = self._c.execute(
                "INSERT INTO reference_types(name, reverse_name, color, sort_order) VALUES (?,?,?,?)",
                (rt.name, rt.reverse_name, rt.color, rt.sort_order),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"a reference type named {rt.name!r} already exists") from exc
        rt.id = cur.lastrowid
        return rt

    def get(self, type_id: int) -> ReferenceType | None:
        row = self._c.execute("SELECT * FROM reference_types WHERE id = ?", (type_id,)).fetchone()
        return _reference_type(row) if row else None

    def get_by_name(self, name: str) -> ReferenceType | None:
        row = self._c.execute("SELECT * FROM reference_types WHERE name = ?", (name,)).fetchone()
        return _reference_type(row) if row else None

    def list_all(self) -> list[ReferenceType]:
        rows = self._c.execute("SELECT * FROM reference_types ORDER BY sort_order, name COLLATE NOCASE").fetchall()
        return [_reference_type(r) for r in rows]

    def update(self, rt: ReferenceType) -> None:
        try:
            self._c.execute(
                "UPDATE reference_types SET name=?, reverse_name=?, color=?, sort_order=? WHERE id=?",
                (rt.name, rt.reverse_name, rt.color, rt.sort_order, rt.id),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"a reference type named {rt.name!r} already exists") from exc

    def delete(self, type_id: int) -> None:
        self._c.execute("DELETE FROM reference_types WHERE id = ?", (type_id,))

    def reorder(self, ordered_ids: Sequence[int]) -> None:
        for index, type_id in enumerate(ordered_ids):
            self._c.execute("UPDATE reference_types SET sort_order = ? WHERE id = ?", (index, type_id))


# --------------------------------------------------------------------------- references


class SqliteReferenceRepository(_Repo):
    def add(self, ref: Reference) -> Reference:
        now = _now()
        cur = self._c.execute(
            "INSERT INTO asset_references(from_asset_id, to_asset_id, type_id, label, from_timestamp_ms, "
            "to_timestamp_ms, note, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (ref.from_asset_id, ref.to_asset_id, ref.type_id, ref.label, ref.from_timestamp_ms,
             ref.to_timestamp_ms, ref.note, now),
        )
        ref.id = cur.lastrowid
        ref.created_at = _from_iso(now)
        return ref

    def get(self, ref_id: int) -> Reference | None:
        row = self._c.execute("SELECT * FROM asset_references WHERE id = ?", (ref_id,)).fetchone()
        return _reference(row) if row else None

    def update(self, ref: Reference) -> None:
        self._c.execute(
            "UPDATE asset_references SET type_id=?, label=?, note=?, from_timestamp_ms=?, to_timestamp_ms=? WHERE id=?",
            (ref.type_id, ref.label, ref.note, ref.from_timestamp_ms, ref.to_timestamp_ms, ref.id),
        )

    def delete(self, ref_id: int) -> None:
        self._c.execute("DELETE FROM asset_references WHERE id = ?", (ref_id,))

    def list_outgoing(self, asset_id: int) -> list[Reference]:
        rows = self._c.execute(
            "SELECT * FROM asset_references WHERE from_asset_id = ? ORDER BY id", (asset_id,)
        ).fetchall()
        return [_reference(r) for r in rows]

    def list_incoming(self, asset_id: int) -> list[Reference]:
        rows = self._c.execute(
            "SELECT * FROM asset_references WHERE to_asset_id = ? ORDER BY id", (asset_id,)
        ).fetchall()
        return [_reference(r) for r in rows]

    def count_for_asset(self, asset_id: int) -> int:
        return int(self._c.execute(
            "SELECT COUNT(*) AS n FROM asset_references WHERE from_asset_id = ? OR to_asset_id = ?",
            (asset_id, asset_id),
        ).fetchone()["n"])

    def counts_for_assets(self, asset_ids: Sequence[int]) -> dict[int, int]:
        result: dict[int, int] = {aid: 0 for aid in asset_ids}
        if not asset_ids:
            return result
        slots = ",".join("?" * len(asset_ids))
        rows = self._c.execute(
            f"SELECT aid, COUNT(*) AS n FROM ("
            f"SELECT from_asset_id AS aid FROM asset_references WHERE from_asset_id IN ({slots}) "
            f"UNION ALL "
            f"SELECT to_asset_id AS aid FROM asset_references WHERE to_asset_id IN ({slots})) GROUP BY aid",
            [*list(asset_ids), *list(asset_ids)],
        ).fetchall()
        for r in rows:
            if r["aid"] in result:
                result[r["aid"]] = int(r["n"])
        return result


# --------------------------------------------------------------------------- sources


class SqliteSourceRepository(_Repo):
    def add(self, source: Source) -> Source:
        try:
            cur = self._c.execute(
                "INSERT INTO sources(path, recursive, enabled, media_types_json, last_scanned_at) VALUES (?,?,?,?,?)",
                (source.path, 1 if source.recursive else 0, 1 if source.enabled else 0,
                 json.dumps(source.media_types), _iso_or_none(source.last_scanned_at)),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"a source at {source.path!r} already exists") from exc
        source.id = cur.lastrowid
        return source

    def get(self, source_id: int) -> Source | None:
        row = self._c.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        return _source(row) if row else None

    def get_by_path(self, path: str) -> Source | None:
        row = self._c.execute("SELECT * FROM sources WHERE path = ?", (path,)).fetchone()
        return _source(row) if row else None

    def list_all(self) -> list[Source]:
        rows = self._c.execute("SELECT * FROM sources ORDER BY path COLLATE NOCASE").fetchall()
        return [_source(r) for r in rows]

    def update(self, source: Source) -> None:
        self._c.execute(
            "UPDATE sources SET path=?, recursive=?, enabled=?, media_types_json=? WHERE id=?",
            (source.path, 1 if source.recursive else 0, 1 if source.enabled else 0,
             json.dumps(source.media_types), source.id),
        )

    def delete(self, source_id: int) -> None:
        self._c.execute("DELETE FROM sources WHERE id = ?", (source_id,))

    def touch_scanned(self, source_id: int) -> None:
        self._c.execute("UPDATE sources SET last_scanned_at = ? WHERE id = ?", (_now(), source_id))


# --------------------------------------------------------------------------- saved views


class SqliteSavedViewRepository(_Repo):
    def add(self, view: SavedView) -> SavedView:
        try:
            cur = self._c.execute(
                "INSERT INTO saved_views(name, filter_json, sort_key, sort_order) VALUES (?,?,?,?)",
                (view.name, view.filter_json, view.sort_key, view.sort_order),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"a saved view named {view.name!r} already exists") from exc
        view.id = cur.lastrowid
        return view

    def get(self, view_id: int) -> SavedView | None:
        row = self._c.execute("SELECT * FROM saved_views WHERE id = ?", (view_id,)).fetchone()
        return _saved_view(row) if row else None

    def list_all(self) -> list[SavedView]:
        rows = self._c.execute("SELECT * FROM saved_views ORDER BY sort_order, name COLLATE NOCASE").fetchall()
        return [_saved_view(r) for r in rows]

    def update(self, view: SavedView) -> None:
        try:
            self._c.execute(
                "UPDATE saved_views SET name=?, filter_json=?, sort_key=?, sort_order=? WHERE id=?",
                (view.name, view.filter_json, view.sort_key, view.sort_order, view.id),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"a saved view named {view.name!r} already exists") from exc

    def delete(self, view_id: int) -> None:
        self._c.execute("DELETE FROM saved_views WHERE id = ?", (view_id,))

    def reorder(self, ordered_ids: Sequence[int]) -> None:
        for index, view_id in enumerate(ordered_ids):
            self._c.execute("UPDATE saved_views SET sort_order = ? WHERE id = ?", (index, view_id))


# --------------------------------------------------------------------------- hash cache & meta


class SqliteHashCacheStore(_Repo):
    def get(self, path: str, size: int, mtime_ns: int) -> str | None:
        row = self._c.execute(
            "SELECT identity_hash FROM hash_cache WHERE path = ? AND size = ? AND mtime_ns = ?",
            (path, size, mtime_ns),
        ).fetchone()
        return row["identity_hash"] if row else None

    def entry(self, path: str) -> tuple[int, int, str] | None:
        row = self._c.execute(
            "SELECT size, mtime_ns, identity_hash FROM hash_cache WHERE path = ?", (path,)
        ).fetchone()
        return (row["size"], row["mtime_ns"], row["identity_hash"]) if row else None

    def put(self, path: str, size: int, mtime_ns: int, identity_hash: str) -> None:
        self._c.execute(
            "INSERT INTO hash_cache(path, size, mtime_ns, identity_hash) VALUES (?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime_ns=excluded.mtime_ns, "
            "identity_hash=excluded.identity_hash",
            (path, size, mtime_ns, identity_hash),
        )

    def forget(self, path: str) -> None:
        self._c.execute("DELETE FROM hash_cache WHERE path = ?", (path,))


class SqliteMetaStore(_Repo):
    def get(self, key: str) -> str | None:
        row = self._c.execute("SELECT value FROM app_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set(self, key: str, value: str) -> None:
        self._c.execute(
            "INSERT INTO app_meta(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
