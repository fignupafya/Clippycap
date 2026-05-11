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
from clippycap.app.reference_service import ReferenceView
from clippycap.app.services import AssetDetail, AssetSummary, NoteView
from clippycap.core.entities import Asset, ReferenceType, Source, Tag
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
    }


def _asset_dict(asset: Asset) -> dict[str, Any]:
    return {
        "id": asset.id, "media_type": asset.media_type, "title": asset.title,
        "size_bytes": asset.size_bytes, "metadata": asset.metadata, "added_at": _iso(asset.added_at),
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
        **_asset_dict(detail.asset), "tag_ids": detail.tag_ids,
        "paths": [{"path": p.path, "present": p.present, "volume_id": p.volume_id} for p in detail.paths],
        "general_note": detail.general_note.body if detail.general_note is not None else None,
        "general_note_id": detail.general_note.id if detail.general_note is not None else None,
        "timestamped_notes": [_note_dict(n) for n in detail.timestamped_notes],
    }


# --------------------------------------------------------------------------- request bodies


class TagBody(BaseModel):
    name: str = Field(min_length=1)
    color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    icon: str | None = None
    image_ref: str | None = None
    description: str = ""
    sort_order: int = 0


class TitleBody(BaseModel):
    title: str = Field(min_length=1)


class RenameFileBody(BaseModel):
    name: str = Field(min_length=1)


class ConfigPatchBody(BaseModel):
    """A partial settings update. Each present section is fully validated against its schema."""
    editing: EditingConfig | None = None
    player: PlayerConfig | None = None
    keybindings: dict[str, str] | None = None


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


def _filter_from_query(
    *, tags_all: list[int], tags_any: list[int], tags_none: list[int], untagged: bool,
    text: str | None, only_missing: bool, never_opened: bool, media_type: str | None,
) -> AssetFilter:
    return AssetFilter(
        tags_all=list(tags_all), tags_any=list(tags_any), tags_none=list(tags_none),
        untagged_only=untagged, text=text or None, only_missing=only_missing,
        never_opened=never_opened, media_type=media_type,
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
        sort: str = "recorded_desc",
        offset: int = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> dict[str, Any]:
        criteria = _filter_from_query(
            tags_all=tags_all, tags_any=tags_any, tags_none=tags_none, untagged=untagged,
            text=text, only_missing=only_missing, never_opened=never_opened, media_type=media_type,
        )
        page = app.assets.list_assets(filter=criteria, sort_key=sort, offset=offset, limit=limit)
        return {"items": [_summary_dict(s) for s in page.items], "total": page.total,
                "offset": page.offset, "limit": page.limit}

    @api.get("/api/assets/{asset_id}")
    def get_asset(app: AppDep, asset_id: int) -> dict[str, Any]:
        return _detail_dict(app.assets.get_detail(asset_id))

    @api.patch("/api/assets/{asset_id}")
    def rename_asset(app: AppDep, asset_id: int, body: TitleBody) -> dict[str, Any]:
        return _asset_dict(app.assets.update_title(asset_id, body.title))

    @api.post("/api/assets/{asset_id}/rename-file")
    def rename_asset_file(app: AppDep, asset_id: int, body: RenameFileBody) -> dict[str, Any]:
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
        )
        return _tag_dict(tag)

    @api.put("/api/tags/{tag_id}")
    def update_tag(app: AppDep, tag_id: int, body: TagBody) -> dict[str, Any]:
        tag = app.tags.update(
            tag_id, name=body.name, color=body.color, icon=body.icon, image_ref=body.image_ref,
            description=body.description, sort_order=body.sort_order,
        )
        return _tag_dict(tag)

    @api.delete("/api/tags/{tag_id}", status_code=204)
    def delete_tag(app: AppDep, tag_id: int) -> Response:
        app.tags.delete(tag_id)
        return Response(status_code=204)

    @api.post("/api/tags/reorder", status_code=204)
    def reorder_tags(app: AppDep, body: IdsBody) -> Response:
        app.tags.reorder(body.ids)
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
