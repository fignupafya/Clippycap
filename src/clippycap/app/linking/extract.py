"""Read one side (an asset or a companion file) into a record of typed field values -- **pure**.

All filesystem/metadata access is done by the caller and handed in as an :class:`ExtractContext`
(precomputed name parts, file attributes as epoch seconds, the metadata dict). That keeps the whole
extract→transform→type pipeline testable without touching the disk. :func:`extract_side` returns the
field values *and* a per-field error map, so the preview can show exactly where a template or a cast
failed rather than silently dropping the item (LINKERS.md §9.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from clippycap.app.linking.steps import StepError, apply_steps
from clippycap.app.linking.template import parse_with_template
from clippycap.app.linking.types import (
    AttrSource,
    CaptureSource,
    ConstSource,
    FieldDef,
    MetadataSource,
    SideSpec,
)
from clippycap.app.linking.values import CoerceError, cast_value


@dataclass(slots=True)
class ExtractContext:
    """Everything :func:`extract_side` needs about one item, precomputed (no I/O happens here)."""

    name: str = ""                              # base name with extension
    stem: str = ""                              # base name without extension
    ext: str = ""                               # lowercase, no leading dot
    path: str = ""
    folder: str = ""                            # the containing folder's name
    folder_index: int | None = None             # position when the folder is sorted by name
    mtime_epoch: float | None = None
    created_epoch: float | None = None
    size: int | None = None
    metadata: dict[str, Any] = dc_field(default_factory=dict)


@dataclass(slots=True)
class SideRecord:
    values: dict[str, Any]
    errors: dict[str, str]                      # field name -> human message (empty = clean)

    @property
    def ok(self) -> bool:
        return not self.errors


def _template_target(ctx: ExtractContext, target: str) -> str:
    return {"stem": ctx.stem, "name": ctx.name, "path": ctx.path, "folder": ctx.folder}.get(target, ctx.stem)


def _raw_value(field: FieldDef, ctx: ExtractContext, captures: dict[str, str]) -> Any:
    source = field.source
    if isinstance(source, CaptureSource):
        return captures.get(source.name)
    if isinstance(source, MetadataSource):
        return ctx.metadata.get(source.key)
    if isinstance(source, ConstSource):
        return source.value
    if isinstance(source, AttrSource):
        return {
            "mtime": ctx.mtime_epoch, "created": ctx.created_epoch, "size": ctx.size,
            "name": ctx.name, "stem": ctx.stem, "ext": ctx.ext, "path": ctx.path,
            "folder": ctx.folder, "folder_index": ctx.folder_index,
        }[source.attr]
    return None                                  # pragma: no cover -- exhaustive above


def extract_side(side: SideSpec, ctx: ExtractContext) -> SideRecord:
    """Parse the template (if any), then evaluate every field (source -> steps -> type). Fields are
    evaluated in order, so a later field may reference an earlier one via a step's ``field`` operand."""
    captures: dict[str, str] = {}
    errors: dict[str, str] = {}
    if side.template:
        # a token inherits the type of the field that reads it, so numeric captures are bounded.
        token_types = {
            f.source.name: f.type for f in side.fields if isinstance(f.source, CaptureSource)
        }
        parsed = parse_with_template(
            _template_target(ctx, side.template_target), side.template,
            anchored=side.template_anchored, case_insensitive=side.case_insensitive,
            token_types=token_types,
        )
        if parsed is None:
            errors["_template"] = f"the name doesn't match the pattern {side.template!r}"
        else:
            captures = parsed

    values: dict[str, Any] = {}
    for field in side.fields:
        try:
            raw = _raw_value(field, ctx, captures)
            transformed = apply_steps(raw, field.steps, values)
            values[field.name] = cast_value(
                transformed, field.type, date_format=field.date_format, tz=field.tz
            )
        except (StepError, CoerceError) as exc:
            errors[field.name] = str(exc)
            values[field.name] = None
    return SideRecord(values=values, errors=errors)
