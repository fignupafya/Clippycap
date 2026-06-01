"""The HTTP API: a FastAPI application built around a wired :class:`~clippycap.app.bootstrap.Application`.

Routes are thin -- they parse input, call an application service, and return JSON (built by the small
``_*_dict`` helpers, so the API has a stable shape independent of the domain dataclasses). Domain
errors are mapped to HTTP status codes. ``/media/{id}/stream`` honours HTTP ``Range`` so the player
can seek; ``/thumbnails/{id}`` serves (or 404s) the generated poster. The built-in detail-view
panels (tags / notes / references / info) are plain frontend code; the backend's extension surface
is the registries + the event bus + plugin-contributed routers (mounted under ``/plugins/...``).
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from clippycap.app.bootstrap import Application
from clippycap.app.ffmpeg_service import FfmpegStatus
from clippycap.app.linking.engine import EngineResult
from clippycap.app.reference_service import ReferenceView
from clippycap.app.services import AssetDetail, AssetSummary, NoteView
from clippycap.app.update_service import ReleaseAsset, UpdateStatus
from clippycap.core.entities import Asset, Attachment, Linker, ReferenceType, Source, Tag, TagGroup
from clippycap.core.errors import ClippycapError, ConflictError, InvalidInputError, NotFoundError, UnsupportedError
from clippycap.core.query import AssetFilter
from clippycap.infra.config.schema import EditingConfig, PlayerConfig

_THUMBNAIL_FORMAT_EXT = {"webp": ".webp", "jpg": ".jpg", "jpeg": ".jpg", "png": ".png"}  # config -> ext
_THUMBNAIL_EXTS = (".webp", ".jpg", ".png")  # tried in order when serving an existing thumbnail
_THUMBNAIL_CONTENT_TYPE_EXT = {
    "image/webp": ".webp", "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
}


def get_app(request: Request) -> Application:
    return request.app.state.application  # type: ignore[no-any-return]


AppDep = Annotated[Application, Depends(get_app)]


# --------------------------------------------------------------------------- JSON shapes


def _iso(value: object) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None


def _tag_dict(tag: Tag) -> dict[str, Any]:
    return {
        "id": tag.id, "name": tag.name, "color": tag.color, "icon": tag.icon,
        "image_ref": tag.image_ref, "description": tag.description, "sort_order": tag.sort_order,
        "group_id": tag.group_id, "has_page": tag.has_page, "notes": tag.notes,
    }


def _tag_group_dict(group: TagGroup) -> dict[str, Any]:
    return {
        "id": group.id, "name": group.name, "color": group.color,
        "sort_order": group.sort_order, "has_page": group.has_page,
        "parent_id": group.parent_id, "notes": group.notes,
    }


def _linker_dict(linker: Linker) -> dict[str, Any]:
    return {
        "id": linker.id, "name": linker.name, "description": linker.description, "color": linker.color,
        "enabled": linker.enabled, "sort_order": linker.sort_order,
        "schema_version": linker.schema_version, "definition_json": linker.definition_json,
        "created_at": _iso(linker.created_at), "updated_at": _iso(linker.updated_at),
    }


def _attachment_dict(att: Attachment) -> dict[str, Any]:
    return {
        "id": att.id, "asset_id": att.asset_id, "linker_id": att.linker_id, "path": att.path,
        "label": att.label, "ext": att.ext, "score": att.score, "matched": att.matched,
        "status": att.status, "origin": att.origin, "size": att.size,
    }


def _engine_result_dict(result: EngineResult) -> dict[str, Any]:
    return {
        "links": [
            {"clip_id": lk.clip_id, "file_path": lk.file_path, "score": lk.score,
             "origin": lk.origin, "reasons": lk.reasons}
            for lk in result.links
        ],
        "ambiguous": {
            str(cid): [{"file_path": c.file_path, "score": c.score} for c in choices]
            for cid, choices in result.ambiguous.items()
        },
        "unmatched_clip_ids": result.unmatched_clip_ids,
        "unused_files": result.unused_files,
        "clip_errors": {str(k): v for k, v in result.clip_errors.items()},
        "file_errors": result.file_errors,
        "candidate_count": result.candidate_count,
        "counts": {
            "matched": len({lk.clip_id for lk in result.links}),
            "links": len(result.links),
            "ambiguous": len(result.ambiguous),
            "unmatched": len(result.unmatched_clip_ids),
            "unused": len(result.unused_files),
        },
    }


def _preset_dict(preset: object) -> dict[str, Any]:
    from clippycap.app.linking.presets import Preset  # noqa: PLC0415 -- local to keep the import surface small
    assert isinstance(preset, Preset)
    return {
        "key": preset.key, "name": preset.name, "description": preset.description,
        "color": preset.color, "definition_json": preset.definition.model_dump_json(),
    }


def _update_asset_dict(a: ReleaseAsset | None) -> dict[str, Any] | None:
    if a is None:
        return None
    return {"name": a.name, "url": a.url, "size": a.size}


def _update_status_dict(s: UpdateStatus) -> dict[str, Any]:
    return {
        "current_version": s.current_version,
        "mode": s.mode,
        "enabled": s.enabled,
        "latest_version": s.latest_version,
        "release_url": s.release_url,
        "release_notes_chain": [
            {"version": n.version, "name": n.name,
             "published_at": n.published_at, "body": n.body}
            for n in s.release_notes_chain
        ],
        "setup_asset": _update_asset_dict(s.setup_asset),
        "portable_asset": _update_asset_dict(s.portable_asset),
        "notified_version": s.notified_version,
        "skipped_version": s.skipped_version,
        "last_checked_at": s.last_checked_at,
        "error": s.error,
        "has_update": s.has_update,
        "is_new_notification": s.is_new_notification,
    }


def _asset_dict(asset: Asset) -> dict[str, Any]:
    return {
        "id": asset.id, "media_type": asset.media_type, "title": asset.title,
        "size_bytes": asset.size_bytes, "metadata": asset.metadata,
        "metadata_pending": asset.metadata_pending, "added_at": _iso(asset.added_at),
        "last_seen_at": _iso(asset.last_seen_at), "last_opened_at": _iso(asset.last_opened_at),
        "thumbnail_url": f"/thumbnails/{asset.id}", "stream_url": f"/media/{asset.id}/stream",
    }


def _summary_dict(summary: AssetSummary) -> dict[str, Any]:
    return {
        **_asset_dict(summary.asset), "tag_ids": summary.tag_ids, "note_count": summary.note_count,
        "reference_count": summary.reference_count, "is_new": summary.is_new,
    }


def _note_dict(view: NoteView) -> dict[str, Any]:
    note = view.note
    return {
        "id": note.id, "asset_id": note.asset_id, "body": note.body, "timestamp_ms": note.timestamp_ms,
        "end_timestamp_ms": note.end_timestamp_ms, "tag_ids": view.tag_ids,
        "created_at": _iso(note.created_at), "updated_at": _iso(note.updated_at),
    }


def _ref_view_dict(view: ReferenceView) -> dict[str, Any]:
    ref = view.reference
    return {
        "id": ref.id, "from_asset_id": ref.from_asset_id, "to_asset_id": ref.to_asset_id,
        "type_id": ref.type_id, "type_name": view.type_name, "label": ref.label, "note": ref.note,
        "from_timestamp_ms": ref.from_timestamp_ms, "to_timestamp_ms": ref.to_timestamp_ms,
        "other_asset_id": view.other_asset_id, "other_asset_title": view.other_asset_title,
        "to_note_body": view.to_note_body,
    }


def _ffmpeg_status_dict(s: FfmpegStatus) -> dict[str, Any]:
    return {
        "available": s.available, "ffprobe_available": s.ffprobe_available,
        "ffmpeg_path": s.ffmpeg_path, "ffprobe_path": s.ffprobe_path, "version": s.version,
        "enabled": s.enabled, "configured_path": s.configured_path,
        "offer_install": s.offer_install, "can_install": s.can_install,
        "installing": s.installing, "install_job_id": s.install_job_id,
    }


def _reference_type_dict(rt: ReferenceType) -> dict[str, Any]:
    return {
        "id": rt.id, "name": rt.name, "reverse_name": rt.reverse_name,
        "color": rt.color, "sort_order": rt.sort_order,
    }


def _source_dict(src: Source) -> dict[str, Any]:
    return {
        "id": src.id, "path": src.path, "recursive": src.recursive, "enabled": src.enabled,
        "media_types": src.media_types, "last_scanned_at": _iso(src.last_scanned_at),
    }


def _detail_dict(detail: AssetDetail) -> dict[str, Any]:
    return {
        **_asset_dict(detail.asset), "tag_ids": detail.tag_ids, "category_ids": detail.category_ids,
        "paths": [{"path": p.path, "present": p.present, "volume_id": p.volume_id} for p in detail.paths],
        "general_note": detail.general_note.body if detail.general_note is not None else None,
        "general_note_id": detail.general_note.id if detail.general_note is not None else None,
        "timestamped_notes": [_note_dict(n) for n in detail.timestamped_notes],
        "mentioned_assets": {str(k): v for k, v in detail.mentioned.items()},
        "mentioned_notes": {str(k): v for k, v in detail.mentioned_notes.items()},
    }


# --------------------------------------------------------------------------- request bodies


class TagBody(BaseModel):
    name: str = Field(min_length=1)
    color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    icon: str | None = None
    image_ref: str | None = None
    description: str = ""
    sort_order: int = 0
    group_id: int | None = None        # tag category; None => uncategorised
    has_page: bool = False             # give this tag its own page (notes + tagged clips)
    notes: str = ""                    # markdown body shown on the tag's page


class TagNotesBody(BaseModel):
    notes: str = ""


class TagGroupBody(BaseModel):
    name: str = Field(min_length=1)
    color: str = ""                    # "" = no colour, or hex "#rrggbb"
    has_page: bool = False
    sort_order: int = 0
    parent_id: int | None = None       # nest under another category; None => top-level


class TagGroupNotesBody(BaseModel):
    notes: str = ""


class RenameFileBody(BaseModel):
    name: str = Field(min_length=1)


class ConfigPatchBody(BaseModel):
    """A partial settings update. Each present section is fully validated against its schema."""
    editing: EditingConfig | None = None
    player: PlayerConfig | None = None
    keybindings: dict[str, str] | None = None


class FfmpegPathBody(BaseModel):
    path: str = Field(min_length=1)


class GeneralNoteBody(BaseModel):
    body: str = ""


class TimestampNoteBody(BaseModel):
    timestamp_ms: int = Field(ge=0)
    end_timestamp_ms: int | None = Field(default=None, ge=0)   # set => an interval note
    body: str = ""


class NoteUpdateBody(BaseModel):
    body: str = ""


class IdsBody(BaseModel):
    ids: list[int]


class BulkTagsBody(BaseModel):
    """A bulk tag change. When ``replace_with`` is set, the listed clips' tags become exactly that
    set (others removed); otherwise ``add`` / ``remove`` are applied as a diff."""

    ids: list[int] = Field(min_length=1)
    add: list[int] = Field(default_factory=list)
    remove: list[int] = Field(default_factory=list)
    replace_with: list[int] | None = None


class BulkCategoriesBody(BaseModel):
    """A bulk direct-category change -- semantics mirror BulkTagsBody. Tag-derived category
    membership is independent and isn't touched here."""

    ids: list[int] = Field(min_length=1)
    add: list[int] = Field(default_factory=list)
    remove: list[int] = Field(default_factory=list)
    replace_with: list[int] | None = None


class LinkerBody(BaseModel):
    name: str = Field(min_length=1)
    definition_json: str = Field(min_length=2)
    description: str = ""
    color: str = ""
    enabled: bool = False


class EnabledBody(BaseModel):
    enabled: bool


class PreviewBody(BaseModel):
    definition_json: str = Field(min_length=2)


class OverrideBody(BaseModel):
    linker_id: int
    path: str = Field(min_length=1)
    decision: str = "pin"           # pin | exclude


class ClearOverrideBody(BaseModel):
    linker_id: int
    path: str = Field(min_length=1)


class OpenWithBody(BaseModel):
    action: str = Field(min_length=1)


class NoteTagsBody(BaseModel):
    tag_ids: list[int]


class NoteTimeBody(BaseModel):
    timestamp_ms: int = Field(ge=0)
    end_timestamp_ms: int | None = Field(default=None, ge=0)


class SourceBody(BaseModel):
    path: str = Field(min_length=1)
    recursive: bool = True
    media_types: list[str] = Field(default_factory=list)


class SourceUpdateBody(BaseModel):
    recursive: bool
    enabled: bool
    media_types: list[str]


class ReferenceBody(BaseModel):
    from_asset_id: int
    to_asset_id: int
    type_id: int | None = None
    label: str = ""
    note: str = ""
    from_timestamp_ms: int | None = None
    to_timestamp_ms: int | None = None


class ReferenceUpdateBody(BaseModel):
    note: str = ""


class ReferenceTypeBody(BaseModel):
    name: str = Field(min_length=1)
    color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    reverse_name: str | None = None
    sort_order: int = 0


class SavedViewBody(BaseModel):
    name: str = Field(min_length=1)
    filter_json: str = "{}"
    sort_key: str
    sort_order: int = 0


class MetadataBody(BaseModel):
    metadata: dict[str, Any]


class SegmentBody(BaseModel):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)


class ExtractSegmentBody(BaseModel):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    remove_from_source: bool = False


# --------------------------------------------------------------------------- helpers


def _filter_from_query(  # noqa: PLR0913 -- one keyword per independent filter is the point
    *, tags_all: list[int], tags_any: list[int], tags_none: list[int], untagged: bool,
    text: str | None, only_missing: bool, never_opened: bool, media_type: str | None,
    path_under: str | None = None, in_categories: list[int] | None = None,
) -> AssetFilter:
    return AssetFilter(
        tags_all=list(tags_all), tags_any=list(tags_any), tags_none=list(tags_none),
        untagged_only=untagged, text=text or None, only_missing=only_missing,
        never_opened=never_opened, media_type=media_type, path_under=path_under,
        in_categories=list(in_categories or []),
    )


def _existing_thumbnail(app: Application, asset_id: int) -> Path | None:
    for ext in _THUMBNAIL_EXTS:
        candidate = app.thumbnail_dir / f"{asset_id}{ext}"
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    return None


def _thumbnail_dest(app: Application, asset_id: int, content_type: str | None) -> Path:
    head = (content_type or "").split(";", 1)[0].strip().lower()
    ext = _THUMBNAIL_CONTENT_TYPE_EXT.get(head) or _THUMBNAIL_FORMAT_EXT.get(
        app.config.thumbnails.format.lower(), ".webp"
    )
    return app.thumbnail_dir / f"{asset_id}{ext}"


def _require_editing(app: Application) -> None:
    if not app.editing.available:
        raise HTTPException(
            status_code=503,
            detail="clip editing is unavailable: ffmpeg is not configured "
                   "(media.ffmpeg.enabled is false, or no ffmpeg binary was found)",
        )


# --------------------------------------------------------------------------- the app


def create_app(application: Application) -> FastAPI:  # noqa: PLR0915 -- a route-registry function is long
    api = FastAPI(title=application.config.app.name, version="0.1.0")
    api.state.application = application

    @api.exception_handler(ClippycapError)
    async def _domain_error(_request: Request, exc: ClippycapError) -> JSONResponse:
        status = {NotFoundError: 404, ConflictError: 409, InvalidInputError: 400, UnsupportedError: 503}.get(
            type(exc), 400
        )
        return JSONResponse(status_code=status, content={"detail": str(exc)})

    # ---- assets ----------------------------------------------------------

    @api.get("/api/assets")
    def list_assets(  # noqa: PLR0913 -- many independent filter query parameters
        app: AppDep,
        tags_all: Annotated[list[int], Query()] = [],  # noqa: B006 -- FastAPI Query default
        tags_any: Annotated[list[int], Query()] = [],  # noqa: B006
        tags_none: Annotated[list[int], Query()] = [],  # noqa: B006
        untagged: bool = False,
        text: str | None = None,
        only_missing: bool = False,
        never_opened: bool = False,
        media_type: str | None = None,
        path_under: str | None = None,
        in_categories: Annotated[list[int], Query()] = [],  # noqa: B006
        sort: str = "recorded_desc",
        offset: int = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> dict[str, Any]:
        criteria = _filter_from_query(
            tags_all=tags_all, tags_any=tags_any, tags_none=tags_none, untagged=untagged,
            text=text, only_missing=only_missing, never_opened=never_opened, media_type=media_type,
            path_under=path_under, in_categories=in_categories,
        )
        page = app.assets.list_assets(filter=criteria, sort_key=sort, offset=offset, limit=limit)
        return {"items": [_summary_dict(s) for s in page.items], "total": page.total,
                "offset": page.offset, "limit": page.limit}

    @api.get("/api/assets/ids")
    def list_asset_ids(  # noqa: PLR0913 -- mirrors /api/assets' filter parameters
        app: AppDep,
        tags_all: Annotated[list[int], Query()] = [],  # noqa: B006
        tags_any: Annotated[list[int], Query()] = [],  # noqa: B006
        tags_none: Annotated[list[int], Query()] = [],  # noqa: B006
        untagged: bool = False,
        text: str | None = None,
        only_missing: bool = False,
        never_opened: bool = False,
        media_type: str | None = None,
        path_under: str | None = None,
        in_categories: Annotated[list[int], Query()] = [],  # noqa: B006
        sort: str = "added_desc",
    ) -> list[int]:
        """Every asset id matching the filter (no pagination). The frontend uses this for the
        bulk-bar "select all matching" shortcut so an action can hit clips beyond the current page."""
        criteria = _filter_from_query(
            tags_all=tags_all, tags_any=tags_any, tags_none=tags_none, untagged=untagged,
            text=text, only_missing=only_missing, never_opened=never_opened, media_type=media_type,
            path_under=path_under, in_categories=in_categories,
        )
        return app.assets.all_matching_ids(filter=criteria, sort_key=sort)

    @api.get("/api/folders")
    def list_folders(app: AppDep) -> list[dict[str, Any]]:
        """Folders that hold at least one present clip, each with the cumulative descendant clip
        count. The frontend builds a collapsible folder tree from this list."""
        return [{"path": p, "count": c} for p, c in app.assets.folder_counts()]

    @api.post("/api/assets/tag-counts")
    def asset_tag_counts(app: AppDep, body: IdsBody) -> dict[int, int]:
        """For each tag id, how many of ``ids`` currently have that tag. Drives the bulk-tag
        modal's per-tag "N of M have this" indicator and pre-fills Replace mode's checkboxes."""
        return app.tags.bulk_tag_counts(body.ids)

    @api.post("/api/assets/bulk-tags")
    def bulk_tags(app: AppDep, body: BulkTagsBody) -> dict[str, int]:
        """Apply tag changes across many clips in one transaction. ``replace_with`` (when given)
        sets each asset's tags to exactly that set; otherwise ``add`` / ``remove`` are applied
        as a diff. Returns counts of links actually created / removed."""
        return app.tags.bulk_apply(
            asset_ids=body.ids, add=body.add, remove=body.remove,
            replace_with=body.replace_with,
        )

    @api.post("/api/assets/category-counts")
    def asset_category_counts(app: AppDep, body: IdsBody) -> dict[int, int]:
        """For each category id: how many of ``body.ids`` are DIRECTLY in that category. Powers
        the bulk-edit modal's per-category 'N of M selected are here' indicator."""
        return app.assets.bulk_category_counts(body.ids)

    @api.post("/api/assets/bulk-categories")
    def bulk_categories(app: AppDep, body: BulkCategoriesBody) -> dict[str, int]:
        """Apply direct-category changes across many clips in one transaction (mirror of
        ``/api/assets/bulk-tags``). Tag-derived category membership is independent and unaffected."""
        return app.assets.bulk_apply_categories(
            asset_ids=body.ids, add=body.add, remove=body.remove,
            replace_with=body.replace_with,
        )

    @api.get("/api/assets/{asset_id}")
    def get_asset(app: AppDep, asset_id: int) -> dict[str, Any]:
        return _detail_dict(app.assets.get_detail(asset_id))

    @api.post("/api/assets/{asset_id}/rename-file")
    def rename_asset_file(app: AppDep, asset_id: int, body: RenameFileBody) -> dict[str, Any]:
        # The clip's name IS its file name; renaming the file is the only "rename" -- it keeps the
        # title, the on-disk file and the path index consistent by construction.
        return _asset_dict(app.assets.rename_file(asset_id, body.name))

    @api.patch("/api/assets/{asset_id}/metadata")
    def patch_metadata(app: AppDep, asset_id: int, body: MetadataBody) -> dict[str, Any]:
        return _asset_dict(app.assets.merge_metadata(asset_id, body.metadata))

    @api.post("/api/assets/{asset_id}/opened", status_code=204)
    def mark_opened(app: AppDep, asset_id: int) -> Response:
        app.assets.mark_opened(asset_id)
        return Response(status_code=204)

    @api.delete("/api/assets/{asset_id}", status_code=204)
    def delete_asset(app: AppDep, asset_id: int, delete_files: bool = False) -> Response:
        app.assets.delete(asset_id, delete_files=delete_files)
        return Response(status_code=204)

    @api.get("/api/assets/{asset_id}/references")
    def asset_references(app: AppDep, asset_id: int) -> dict[str, Any]:
        listing = app.references.for_asset(asset_id)
        return {"outgoing": [_ref_view_dict(v) for v in listing.outgoing],
                "incoming": [_ref_view_dict(v) for v in listing.incoming]}

    @api.get("/api/assets/{asset_id}/notes")
    def asset_notes(app: AppDep, asset_id: int) -> list[dict[str, Any]]:
        return [_note_dict(n) for n in app.notes.list_for_asset(asset_id)]

    @api.put("/api/assets/{asset_id}/notes/general")
    def set_general_note(app: AppDep, asset_id: int, body: GeneralNoteBody) -> dict[str, Any]:
        return _note_dict(app.notes.set_general(asset_id, body.body))

    @api.post("/api/assets/{asset_id}/notes", status_code=201)
    def add_timestamp_note(app: AppDep, asset_id: int, body: TimestampNoteBody) -> dict[str, Any]:
        return _note_dict(app.notes.add_timestamped(
            asset_id, body.timestamp_ms, body.body, end_timestamp_ms=body.end_timestamp_ms
        ))

    # ---- editing: trim / cut / extract (needs ffmpeg) --------------------

    @api.post("/api/assets/{asset_id}/trim")
    def trim_asset(app: AppDep, asset_id: int, body: SegmentBody) -> dict[str, Any]:
        _require_editing(app)
        return _asset_dict(app.editing.trim(asset_id, start_ms=body.start_ms, end_ms=body.end_ms))

    @api.post("/api/assets/{asset_id}/remove-segment")
    def remove_asset_segment(app: AppDep, asset_id: int, body: SegmentBody) -> dict[str, Any]:
        _require_editing(app)
        return _asset_dict(app.editing.remove_segment(asset_id, start_ms=body.start_ms, end_ms=body.end_ms))

    @api.post("/api/assets/{asset_id}/extract-segment", status_code=201)
    def extract_asset_segment(app: AppDep, asset_id: int, body: ExtractSegmentBody) -> dict[str, Any]:
        _require_editing(app)
        return _asset_dict(app.editing.extract_segment(
            asset_id, start_ms=body.start_ms, end_ms=body.end_ms, remove_from_source=body.remove_from_source,
        ))

    # ---- media: streaming + thumbnails -----------------------------------

    @api.get("/media/{asset_id}/stream")
    def stream_media(app: AppDep, asset_id: int) -> Response:
        path = app.assets.present_file_path(asset_id)
        if path is None:
            raise HTTPException(status_code=404, detail="no readable file for this asset")
        # FileResponse honours the Range header itself and -- via an ``async with`` over the file --
        # releases the handle as soon as the response ends or the client disconnects, so a playing /
        # buffered clip is never left open (which would otherwise block trimming it on Windows).
        return FileResponse(path, media_type=mimetypes.guess_type(str(path))[0] or "application/octet-stream")

    @api.get("/thumbnails/{asset_id}")
    def thumbnail(app: AppDep, asset_id: int) -> Response:
        asset = app.assets.get(asset_id)
        if asset is None:
            raise HTTPException(status_code=404, detail="no such asset")
        existing = _existing_thumbnail(app, asset_id)
        if existing is not None:
            return FileResponse(existing)
        provider = app.registries.media_types.get(asset.media_type)
        source = app.assets.present_file_path(asset_id)
        if app.ffmpeg_available and provider is not None and source is not None:
            provider.make_thumbnail(source, _thumbnail_dest(app, asset_id, None), metadata=asset.metadata)
            existing = _existing_thumbnail(app, asset_id)
            if existing is not None:
                return FileResponse(existing)
        if not app.ffmpeg_available:                       # not a 404: the client should make one itself
            return JSONResponse(
                status_code=503,
                content={
                    "detail": (
                        "Server-side thumbnail generation is unavailable: ffmpeg is not configured "
                        "(media.ffmpeg.enabled is false, or no ffmpeg binary was found). Capture a frame "
                        f"from /media/{asset_id}/stream in the browser and PUT it back to this URL."
                    ),
                    "reason": "ffmpeg_unavailable",
                    "stream_url": f"/media/{asset_id}/stream",
                },
            )
        raise HTTPException(status_code=404, detail="no thumbnail could be generated for this asset")

    @api.put("/thumbnails/{asset_id}", status_code=204)
    async def upload_thumbnail(app: AppDep, asset_id: int, request: Request) -> Response:
        if app.assets.get(asset_id) is None:
            raise HTTPException(status_code=404, detail="no such asset")
        data = await request.body()
        if not data:
            raise HTTPException(status_code=400, detail="empty request body")
        content_type = request.headers.get("content-type")
        head = (content_type or "").split(";", 1)[0].strip().lower()
        if head and not head.startswith("image/"):
            raise HTTPException(status_code=415, detail=f"expected an image/* body, got {head!r}")
        dest = _thumbnail_dest(app, asset_id, content_type)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        for ext in _THUMBNAIL_EXTS:                        # keep one thumbnail per asset
            other = app.thumbnail_dir / f"{asset_id}{ext}"
            if other != dest and other.is_file():
                other.unlink(missing_ok=True)
        return Response(status_code=204)

    # ---- tags ------------------------------------------------------------

    @api.get("/api/tags")
    def list_tags(app: AppDep) -> list[dict[str, Any]]:
        return [{**_tag_dict(t), "asset_count": n} for t, n in app.tags.list_with_counts()]

    @api.post("/api/tags", status_code=201)
    def create_tag(app: AppDep, body: TagBody) -> dict[str, Any]:
        tag = app.tags.create(
            name=body.name, color=body.color, icon=body.icon, image_ref=body.image_ref,
            description=body.description, sort_order=body.sort_order,
            group_id=body.group_id, has_page=body.has_page, notes=body.notes,
        )
        return _tag_dict(tag)

    @api.put("/api/tags/{tag_id}")
    def update_tag(app: AppDep, tag_id: int, body: TagBody) -> dict[str, Any]:
        tag = app.tags.update(
            tag_id, name=body.name, color=body.color, icon=body.icon, image_ref=body.image_ref,
            description=body.description, sort_order=body.sort_order,
            group_id=body.group_id, has_page=body.has_page, notes=body.notes,
        )
        return _tag_dict(tag)

    @api.put("/api/tags/{tag_id}/notes")
    def set_tag_notes(app: AppDep, tag_id: int, body: TagNotesBody) -> dict[str, Any]:
        """Update only a tag's page notes -- the tag page's autosaving notes editor calls this."""
        return _tag_dict(app.tags.set_notes(tag_id, body.notes))

    @api.delete("/api/tags/{tag_id}", status_code=204)
    def delete_tag(app: AppDep, tag_id: int) -> Response:
        app.tags.delete(tag_id)
        return Response(status_code=204)

    @api.post("/api/tags/reorder", status_code=204)
    def reorder_tags(app: AppDep, body: IdsBody) -> Response:
        app.tags.reorder(body.ids)
        return Response(status_code=204)

    # ---- tag groups (categories) -----------------------------------------
    @api.get("/api/tag-groups")
    def list_tag_groups(app: AppDep) -> list[dict[str, Any]]:
        return [_tag_group_dict(g) for g in app.tag_groups.list_all()]

    @api.post("/api/tag-groups", status_code=201)
    def create_tag_group(app: AppDep, body: TagGroupBody) -> dict[str, Any]:
        group = app.tag_groups.create(
            name=body.name, color=body.color, has_page=body.has_page, parent_id=body.parent_id,
        )
        return _tag_group_dict(group)

    @api.put("/api/tag-groups/{group_id}")
    def update_tag_group(app: AppDep, group_id: int, body: TagGroupBody) -> dict[str, Any]:
        group = app.tag_groups.update(
            group_id, name=body.name, color=body.color, has_page=body.has_page,
            sort_order=body.sort_order, parent_id=body.parent_id,
        )
        return _tag_group_dict(group)

    @api.put("/api/tag-groups/{group_id}/notes")
    def set_tag_group_notes(app: AppDep, group_id: int, body: TagGroupNotesBody) -> dict[str, Any]:
        """Update only a category's page notes -- the category page's autosaving editor calls this."""
        return _tag_group_dict(app.tag_groups.set_notes(group_id, body.notes))

    @api.delete("/api/tag-groups/{group_id}", status_code=204)
    def delete_tag_group(app: AppDep, group_id: int) -> Response:
        app.tag_groups.delete(group_id)        # its tags fall back to uncategorised
        return Response(status_code=204)

    @api.post("/api/tag-groups/reorder", status_code=204)
    def reorder_tag_groups(app: AppDep, body: IdsBody) -> Response:
        app.tag_groups.reorder(body.ids)
        return Response(status_code=204)

    @api.post("/api/tag-images")
    async def upload_tag_image(app: AppDep, request: Request) -> dict[str, str]:
        data = await request.body()
        return {
            "image_ref": app.tags.store_image(
                data, ext=request.query_params.get("ext", ""), content_type=request.headers.get("content-type", "")
            )
        }

    @api.get("/api/tag-images/{ref}")
    def get_tag_image(app: AppDep, ref: str) -> FileResponse:
        if not ref or "/" in ref or "\\" in ref or ".." in ref:
            raise HTTPException(status_code=404)
        path = app.tag_images_dir / ref
        if not path.is_file():
            raise HTTPException(status_code=404)
        media_type, _ = mimetypes.guess_type(path.name)
        return FileResponse(
            path, media_type=media_type or "application/octet-stream",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @api.post("/api/assets/{asset_id}/tags/{tag_id}", status_code=204)
    def apply_tag(app: AppDep, asset_id: int, tag_id: int) -> Response:
        app.tags.apply_to_asset(asset_id, tag_id)
        return Response(status_code=204)

    @api.delete("/api/assets/{asset_id}/tags/{tag_id}", status_code=204)
    def unapply_tag(app: AppDep, asset_id: int, tag_id: int) -> Response:
        app.tags.remove_from_asset(asset_id, tag_id)
        return Response(status_code=204)

    @api.post("/api/assets/{asset_id}/categories/{category_id}", status_code=204)
    def add_asset_category(app: AppDep, asset_id: int, category_id: int) -> Response:
        """Assign a clip DIRECTLY to a category -- no tag needed."""
        app.assets.add_category(asset_id, category_id)
        return Response(status_code=204)

    @api.delete("/api/assets/{asset_id}/categories/{category_id}", status_code=204)
    def remove_asset_category(app: AppDep, asset_id: int, category_id: int) -> Response:
        app.assets.remove_category(asset_id, category_id)
        return Response(status_code=204)

    # ---- updates (GitHub Releases check + one-click install) ----------------
    @api.get("/api/updates/status")
    def update_status(app: AppDep) -> dict[str, Any]:
        return _update_status_dict(app.updates.get_status())

    @api.post("/api/updates/check")
    def update_check(app: AppDep) -> dict[str, Any]:
        """Skip the cache and re-hit the GitHub Releases endpoint right now."""
        return _update_status_dict(app.updates.get_status(force_check=True))

    @api.post("/api/updates/install")
    def update_install(app: AppDep) -> dict[str, Any]:
        return app.updates.start_install()

    @api.get("/api/updates/install-progress")
    def update_install_progress(app: AppDep) -> dict[str, Any]:
        return app.updates.install_progress

    @api.post("/api/updates/dismiss")
    def update_dismiss(app: AppDep) -> dict[str, Any]:
        """Mark the current latest version as "seen" so subsequent sessions just show the badge
        instead of auto-popping the modal again."""
        return _update_status_dict(app.updates.mark_notified())

    @api.post("/api/updates/skip")
    def update_skip(app: AppDep) -> dict[str, Any]:
        """Hide the badge for this version (and any older) until a strictly newer one ships."""
        return _update_status_dict(app.updates.mark_skipped())

    # ---- notes (by id) ---------------------------------------------------

    @api.patch("/api/notes/{note_id}")
    def update_note(app: AppDep, note_id: int, body: NoteUpdateBody) -> dict[str, Any]:
        return _note_dict(app.notes.update(note_id, body.body))

    @api.delete("/api/notes/{note_id}", status_code=204)
    def delete_note(app: AppDep, note_id: int) -> Response:
        app.notes.delete(note_id)
        return Response(status_code=204)

    @api.put("/api/notes/{note_id}/tags", status_code=204)
    def set_note_tags(app: AppDep, note_id: int, body: NoteTagsBody) -> Response:
        app.notes.set_tags(note_id, body.tag_ids)
        return Response(status_code=204)

    @api.put("/api/notes/{note_id}/time")
    def retime_note(app: AppDep, note_id: int, body: NoteTimeBody) -> dict[str, Any]:
        return _note_dict(app.notes.retime(note_id, body.timestamp_ms, end_timestamp_ms=body.end_timestamp_ms))

    # ---- references ------------------------------------------------------

    @api.post("/api/references", status_code=201)
    def create_reference(app: AppDep, body: ReferenceBody) -> dict[str, Any]:
        ref = app.references.create(
            from_asset_id=body.from_asset_id, to_asset_id=body.to_asset_id, type_id=body.type_id,
            label=body.label, note=body.note, from_timestamp_ms=body.from_timestamp_ms,
            to_timestamp_ms=body.to_timestamp_ms,
        )
        return {"id": ref.id}

    @api.patch("/api/references/{reference_id}", status_code=204)
    def update_reference(app: AppDep, reference_id: int, body: ReferenceUpdateBody) -> Response:
        app.references.update(reference_id, note=body.note)
        return Response(status_code=204)

    @api.delete("/api/references/{reference_id}", status_code=204)
    def delete_reference(app: AppDep, reference_id: int) -> Response:
        app.references.delete(reference_id)
        return Response(status_code=204)

    @api.get("/api/reference-types")
    def list_reference_types(app: AppDep) -> list[dict[str, Any]]:
        return [_reference_type_dict(rt) for rt in app.reference_types.list_all()]

    @api.post("/api/reference-types", status_code=201)
    def create_reference_type(app: AppDep, body: ReferenceTypeBody) -> dict[str, Any]:
        rt = app.reference_types.create(
            name=body.name, color=body.color, reverse_name=body.reverse_name, sort_order=body.sort_order
        )
        return _reference_type_dict(rt)

    @api.put("/api/reference-types/{type_id}")
    def update_reference_type(app: AppDep, type_id: int, body: ReferenceTypeBody) -> dict[str, Any]:
        rt = app.reference_types.update(
            type_id, name=body.name, color=body.color, reverse_name=body.reverse_name, sort_order=body.sort_order
        )
        return _reference_type_dict(rt)

    @api.delete("/api/reference-types/{type_id}", status_code=204)
    def delete_reference_type(app: AppDep, type_id: int) -> Response:
        app.reference_types.delete(type_id)
        return Response(status_code=204)

    @api.post("/api/reference-types/reorder", status_code=204)
    def reorder_reference_types(app: AppDep, body: IdsBody) -> Response:
        app.reference_types.reorder(body.ids)
        return Response(status_code=204)

    # ---- sources + scanning ----------------------------------------------

    @api.get("/api/sources")
    def list_sources(app: AppDep) -> list[dict[str, Any]]:
        return [_source_dict(s) for s in app.sources.list_all()]

    @api.post("/api/sources", status_code=201)
    def add_source(app: AppDep, body: SourceBody) -> dict[str, Any]:
        return _source_dict(app.sources.create(body.path, recursive=body.recursive, media_types=body.media_types))

    @api.put("/api/sources/{source_id}")
    def update_source(app: AppDep, source_id: int, body: SourceUpdateBody) -> dict[str, Any]:
        return _source_dict(app.sources.update(
            source_id, recursive=body.recursive, enabled=body.enabled, media_types=body.media_types
        ))

    @api.delete("/api/sources/{source_id}", status_code=204)
    def delete_source(app: AppDep, source_id: int) -> Response:
        app.sources.delete(source_id)
        return Response(status_code=204)

    @api.post("/api/sources/{source_id}/scan", status_code=202)
    def scan_source(app: AppDep, source_id: int) -> dict[str, str]:
        return {"job_id": app.scans.scan_source(source_id)}

    @api.post("/api/scan", status_code=202)
    def scan_all(app: AppDep) -> dict[str, str]:
        return {"job_id": app.scans.scan_all()}

    @api.post("/api/reconcile")
    def reconcile(app: AppDep) -> dict[str, Any]:
        # A fast, hashing-free re-sync of the path index: picks up files renamed / moved / deleted
        # outside the app. A plain (non-async) handler, so FastAPI runs it in a worker thread and
        # the filesystem walk never blocks the event loop. The UI calls this on window focus.
        r = app.scans.reconcile()
        return {"changed": r.changed, "renamed": r.renamed,
                "vanished": r.vanished, "restored": r.restored}

    @api.get("/api/jobs")
    def list_jobs(app: AppDep) -> list[dict[str, Any]]:
        return [vars(h) for h in app.jobs.list_all()]

    @api.get("/api/jobs/{job_id}")
    def get_job(app: AppDep, job_id: str) -> dict[str, Any]:
        handle = app.jobs.get(job_id)
        if handle is None:
            raise HTTPException(status_code=404, detail="no such job")
        return vars(handle)

    # ---- saved views -----------------------------------------------------

    @api.get("/api/saved-views")
    def list_saved_views(app: AppDep) -> list[dict[str, Any]]:
        return [{"id": v.id, "name": v.name, "filter_json": v.filter_json, "sort_key": v.sort_key,
                 "sort_order": v.sort_order} for v in app.saved_views.list_all()]

    @api.post("/api/saved-views", status_code=201)
    def create_saved_view(app: AppDep, body: SavedViewBody) -> dict[str, Any]:
        v = app.saved_views.create(name=body.name, filter_json=body.filter_json, sort_key=body.sort_key,
                                   sort_order=body.sort_order)
        return {"id": v.id, "name": v.name, "filter_json": v.filter_json, "sort_key": v.sort_key,
                "sort_order": v.sort_order}

    @api.put("/api/saved-views/{view_id}")
    def update_saved_view(app: AppDep, view_id: int, body: SavedViewBody) -> dict[str, Any]:
        v = app.saved_views.update(view_id, name=body.name, filter_json=body.filter_json,
                                   sort_key=body.sort_key, sort_order=body.sort_order)
        return {"id": v.id, "name": v.name, "filter_json": v.filter_json, "sort_key": v.sort_key,
                "sort_order": v.sort_order}

    @api.delete("/api/saved-views/{view_id}", status_code=204)
    def delete_saved_view(app: AppDep, view_id: int) -> Response:
        app.saved_views.delete(view_id)
        return Response(status_code=204)

    @api.post("/api/saved-views/reorder", status_code=204)
    def reorder_saved_views(app: AppDep, body: IdsBody) -> Response:
        app.saved_views.reorder(body.ids)
        return Response(status_code=204)

    # ---- linkers (companion-file linking) --------------------------------

    @api.get("/api/linkers")
    def list_linkers(app: AppDep) -> list[dict[str, Any]]:
        return [_linker_dict(lk) for lk in app.linkers.list_all()]

    @api.get("/api/linkers/presets")
    def linker_presets(app: AppDep) -> list[dict[str, Any]]:
        return [_preset_dict(p) for p in app.linkers.presets()]

    @api.post("/api/linkers", status_code=201)
    def create_linker(app: AppDep, body: LinkerBody) -> dict[str, Any]:
        return _linker_dict(app.linkers.create(
            name=body.name, definition_json=body.definition_json, description=body.description,
            color=body.color, enabled=body.enabled,
        ))

    @api.post("/api/linkers/preview")
    def preview_linker(app: AppDep, body: PreviewBody) -> dict[str, Any]:
        return _engine_result_dict(app.linkers.preview(body.definition_json))

    @api.post("/api/linkers/run-all", status_code=202)
    def run_all_linkers(app: AppDep) -> dict[str, str]:
        return {"job_id": app.linkers.run_all_enabled()}

    @api.post("/api/linkers/reorder", status_code=204)
    def reorder_linkers(app: AppDep, body: IdsBody) -> Response:
        app.linkers.reorder(body.ids)
        return Response(status_code=204)

    @api.get("/api/linkers/{linker_id}")
    def get_linker(app: AppDep, linker_id: int) -> dict[str, Any]:
        return _linker_dict(app.linkers.get(linker_id))

    @api.put("/api/linkers/{linker_id}")
    def update_linker(app: AppDep, linker_id: int, body: LinkerBody) -> dict[str, Any]:
        return _linker_dict(app.linkers.update(
            linker_id, name=body.name, definition_json=body.definition_json,
            description=body.description, color=body.color, enabled=body.enabled,
        ))

    @api.delete("/api/linkers/{linker_id}", status_code=204)
    def delete_linker(app: AppDep, linker_id: int) -> Response:
        app.linkers.delete(linker_id)
        return Response(status_code=204)

    @api.post("/api/linkers/{linker_id}/enabled")
    def set_linker_enabled(app: AppDep, linker_id: int, body: EnabledBody) -> dict[str, Any]:
        return _linker_dict(app.linkers.set_enabled(linker_id, body.enabled))

    @api.post("/api/linkers/{linker_id}/clone", status_code=201)
    def clone_linker(app: AppDep, linker_id: int) -> dict[str, Any]:
        return _linker_dict(app.linkers.clone(linker_id))

    @api.post("/api/linkers/{linker_id}/run", status_code=202)
    def run_linker(app: AppDep, linker_id: int) -> dict[str, str]:
        return {"job_id": app.linkers.run(linker_id)}

    @api.get("/api/assets/{asset_id}/attachments")
    def asset_attachments(app: AppDep, asset_id: int) -> list[dict[str, Any]]:
        return [_attachment_dict(a) for a in app.linkers.attachments_for_asset(asset_id)]

    @api.post("/api/assets/{asset_id}/overrides", status_code=204)
    def set_override(app: AppDep, asset_id: int, body: OverrideBody) -> Response:
        app.linkers.set_override(
            asset_id=asset_id, linker_id=body.linker_id, path=body.path, decision=body.decision
        )
        return Response(status_code=204)

    @api.post("/api/assets/{asset_id}/overrides/clear", status_code=204)
    def clear_override(app: AppDep, asset_id: int, body: ClearOverrideBody) -> Response:
        app.linkers.clear_override(asset_id=asset_id, linker_id=body.linker_id, path=body.path)
        return Response(status_code=204)

    @api.post("/api/attachments/{attachment_id}/reveal", status_code=204)
    def reveal_attachment(app: AppDep, attachment_id: int) -> Response:
        app.linkers.reveal(attachment_id)
        return Response(status_code=204)

    @api.post("/api/attachments/{attachment_id}/open", status_code=204)
    def open_attachment(app: AppDep, attachment_id: int) -> Response:
        app.linkers.open_default(attachment_id)
        return Response(status_code=204)

    @api.post("/api/attachments/{attachment_id}/open-with", status_code=204)
    def open_attachment_with(app: AppDep, attachment_id: int, body: OpenWithBody) -> Response:
        app.linkers.open_with(attachment_id, body.action)
        return Response(status_code=204)

    # ---- config / plugins / health ---------------------------------------

    @api.get("/api/config")
    def get_config(app: AppDep) -> dict[str, Any]:
        return app.config.model_dump(mode="json")

    @api.put("/api/config")
    def update_config(app: AppDep, body: ConfigPatchBody) -> dict[str, Any]:
        # Pydantic has already validated each section against its EditingConfig / PlayerConfig
        # schema. ConfigService writes local.toml + reloads (and rolls back local.toml on a failing
        # cross-section validation), then swaps the shared ConfigHolder so the ffmpeg editor and the
        # editing service read the new values on their very next call -- no restart needed.
        new_config = app.config_service.update(
            editing=body.editing.model_dump(mode="python") if body.editing else None,
            player=body.player.model_dump(mode="python") if body.player else None,
            keybindings=body.keybindings,
        )
        return new_config.model_dump(mode="json")

    # ---- ffmpeg: status, on-demand install, point at an existing build ---

    @api.get("/api/ffmpeg")
    def ffmpeg_status(app: AppDep) -> dict[str, Any]:
        return _ffmpeg_status_dict(app.ffmpeg.status())

    @api.post("/api/ffmpeg/install", status_code=202)
    def ffmpeg_install(app: AppDep) -> dict[str, str]:
        return {"job_id": app.ffmpeg.start_install()}        # poll GET /api/jobs/{job_id} for progress

    @api.post("/api/ffmpeg/path")
    def ffmpeg_set_path(app: AppDep, body: FfmpegPathBody) -> dict[str, Any]:
        return _ffmpeg_status_dict(app.ffmpeg.use_path(body.path))

    @api.post("/api/ffmpeg/auto")
    def ffmpeg_use_auto(app: AppDep) -> dict[str, Any]:
        return _ffmpeg_status_dict(app.ffmpeg.use_auto())

    @api.post("/api/ffmpeg/dismiss-prompt", status_code=204)
    def ffmpeg_dismiss_prompt(app: AppDep) -> Response:
        app.ffmpeg.dismiss_install_prompt()
        return Response(status_code=204)

    @api.get("/api/plugins")
    def list_plugins(app: AppDep) -> list[str]:
        return list(app.loaded_plugins)

    @api.get("/api/health")
    def health(app: AppDep) -> dict[str, Any]:
        return {
            "name": app.config.app.name,
            "ffmpeg": app.ffmpeg_available,
            "media_types": [p.media_type for p in app.registries.media_types],
            "plugins": list(app.loaded_plugins),
        }

    # ---- plugin-contributed routers --------------------------------------

    for router in application.registries.api_routers:
        api.include_router(router, prefix="/plugins")

    # ---- the frontend (built SPA), if present ----------------------------

    web_dist = application.install_dir / "web" / "dist"
    if web_dist.is_dir():
        api.mount("/", StaticFiles(directory=str(web_dist), html=True), name="frontend")
    else:
        @api.get("/", response_class=HTMLResponse)
        def _placeholder() -> str:
            return (
                "<!doctype html><meta charset=utf-8><title>Clippycap</title>"
                "<body style='font:16px/1.5 system-ui;max-width:42rem;margin:3rem auto;padding:0 1rem'>"
                "<h1>Clippycap backend is running</h1>"
                "<p>The web UI hasn't been built into <code>web/dist</code> yet. Meanwhile: the "
                "interactive API is at <a href='/docs'>/docs</a>, and "
                "<code>python -m clippycap add-source &lt;folder&gt;</code> then "
                "<code>python -m clippycap scan</code> populate the library.</p></body>"
            )

    return api
