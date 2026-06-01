"""The linker rule language -- validated, JSON-serialisable value objects.

A :class:`LinkerDefinition` is the whole rule: which assets (``source``) get which files
(``target``); how each side's name/metadata is read into typed **fields** (``clip`` / ``file``);
which **conditions** make a (clip, file) pair a candidate (``match``); how candidates are selected
into links under cardinality rules (``resolve``); and the optional "open with" programs (``actions``).

It serialises to the ``definition_json`` blob stored on a :class:`~clippycap.core.entities.Linker`,
and is ``schema_version``-stamped so the language can evolve. Everything is ``extra="forbid"`` so a
typo in an imported recipe fails loudly rather than being silently ignored.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from clippycap.core.errors import InvalidInputError

SCHEMA_VERSION = 1

# A typed field's value type. Internally datetimes and durations are normalised to **epoch seconds**
# / **seconds** (float) so every numeric/time predicate compares like with like (no timezone/DST
# pitfalls -- see LINKERS.md §11). ``string`` stays text; ``bool`` stays bool.
FieldType = Literal["int", "float", "string", "datetime", "duration", "bool"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- field sources


class CaptureSource(_Model):
    """A named capture from this side's filename ``template`` (e.g. the ``scene`` in ``Scene12``)."""

    kind: Literal["capture"] = "capture"
    name: str


class MetadataSource(_Model):
    """A value from the asset's stored metadata, by key (e.g. ``duration_ms``, ``width``)."""

    kind: Literal["metadata"] = "metadata"
    key: str


class AttrSource(_Model):
    """A filesystem attribute. ``created`` is the Windows birthtime; ``folder_index`` is the file's
    position when its folder is sorted by name (fragile -- a last resort)."""

    kind: Literal["attr"] = "attr"
    attr: Literal["mtime", "created", "size", "name", "stem", "ext", "path", "folder", "folder_index"]


class ConstSource(_Model):
    """A fixed literal -- offsets, unit factors, a default."""

    kind: Literal["const"] = "const"
    value: Any = None


FieldSource = Annotated[
    CaptureSource | MetadataSource | AttrSource | ConstSource,
    Field(discriminator="kind"),
]


# --------------------------------------------------------------------------- transform steps


# One visual transform "step" (LINKERS.md §9.3). A flat shape so the JSON stays readable; the
# evaluator enforces per-op requirements with clear errors. Operand = ``value`` (a literal) or
# ``field`` (another field on the same side, evaluated earlier).
StepOp = Literal[
    # text
    "trim", "lower", "upper", "keep_digits", "keep_letters", "replace", "split_take",
    "substr", "regex_extract", "concat",
    # number
    "to_number", "add", "sub", "mul", "div", "round", "abs",
    # datetime / duration
    "parse_date", "date_add", "date_round", "date_part", "to_epoch", "parse_duration",
]


class Step(_Model):
    op: StepOp
    value: Any = None                       # a literal operand
    field: str | None = None                # operand = another field's value on the same side
    fmt: str | None = None                  # date format / regex pattern
    unit: str | None = None                 # date unit: second|minute|hour|day  ·  date_part: date|time
    sep: str | None = None                  # split separator / concat joiner
    index: int | None = None                # split_take part / substr start
    end: int | None = None                  # substr end
    group: int | None = None                # regex capture group (default 1)


class FieldDef(_Model):
    """A named, typed value read from one side. ``source`` gives the raw value; ``steps`` transform
    it; ``type`` casts/normalises the result (datetime/duration -> epoch/seconds floats)."""

    name: str
    type: FieldType = "string"
    source: FieldSource
    steps: list[Step] = Field(default_factory=list)
    date_format: str | None = None          # how to parse a datetime field's raw string (None = detect)
    tz: Literal["local", "utc"] = "local"   # assumed zone of a naive datetime string


class SideSpec(_Model):
    """How to read one side (the asset, or the companion file) into fields."""

    template: str | None = None             # the %name% parse, run against `template_target`
    template_target: Literal["stem", "name", "path", "folder"] = "stem"
    template_anchored: bool = True          # match the whole target vs. search anywhere
    case_insensitive: bool = True
    fields: list[FieldDef] = Field(default_factory=list)


# --------------------------------------------------------------------------- match (join)


class Ref(_Model):
    """A reference to a field on one side."""

    side: Literal["clip", "file"]
    field: str


ConditionOp = Literal[
    "equals", "not_equals",                 # any types (normalised)
    "within",                               # |left - right| <= tolerance (+ optional directional offset)
    "interval_contains",                    # start <= left <= end (+ slack)
    "interval_overlap",                     # [left,right] overlaps [start,end]
    "lt", "lte", "gt", "gte",               # ordered compare left vs right
    "contains", "startswith", "endswith", "regex",   # string left vs right/pattern
    "fuzzy",                                # similarity(left,right) >= threshold
]


class Condition(_Model):
    """One sentence-built predicate (LINKERS.md §9.4). ``left`` is the primary operand (the clip's
    point for interval ops); ``right`` the comparison operand; ``start``/``end`` the interval."""

    op: ConditionOp
    left: Ref
    right: Ref | None = None
    start: Ref | None = None
    end: Ref | None = None
    tolerance: float | None = None          # "within": max |delta| in the field's units (seconds for time)
    offset: float = 0.0                     # "within": expected directional offset (right = left + offset)
    slack: float = 0.0                      # interval ops: widen the interval by this on both ends
    threshold: float = 0.8                  # "fuzzy": minimum similarity 0..1
    weight: float = 1.0                     # contribution to the combined score
    case_insensitive: bool = True


class MatchSpec(_Model):
    combine: Literal["all", "any"] = "all"
    conditions: list[Condition] = Field(default_factory=list)
    min_score: float = 0.0                  # drop a candidate scoring below this


# --------------------------------------------------------------------------- resolve


TiebreakKey = Literal["score", "nearest_time", "newest", "oldest", "smallest", "largest", "name"]


def _default_tiebreak() -> list[TiebreakKey]:
    return ["score"]


class ResolveSpec(_Model):
    """How candidates become links, judged relative to one another (LINKERS.md §6)."""

    strategy: Literal["keep_all", "best_per_clip", "best_per_file", "best_overall", "quota"] = "best_per_clip"
    per_clip_max: int | None = None         # None = unlimited
    per_clip_min: int = 0                    # >=1 => mandatory (an unmatched clip is an error)
    per_file_max: int | None = None          # None = a file may be shared by any number of clips
    quota: int | None = None                 # "quota" strategy: keep only the best N overall
    relative_threshold: float | None = None  # keep candidates within this of the clip's best score
    absolute_floor: float = 0.0              # ...but never below this absolute score
    contested: Literal["drop", "cascade", "flag"] = "flag"   # a capped file wanted by several clips
    ambiguity_margin: float = 0.0            # top-2 within this => flag as "needs you"
    tiebreak: list[TiebreakKey] = Field(default_factory=_default_tiebreak)
    one_per_group: Ref | None = None         # at most one match per value of this field
    stable: bool = True                      # keep existing links sticky on re-run


# --------------------------------------------------------------------------- actions (open-with)


class OpenAction(_Model):
    """A user-configured "open with" program, keyed by extension. ``args`` is an argv template;
    every ``%PATH%`` element is replaced by the file path (passed as a single argument -- never a
    shell string, so spaces/quotes in the path are safe)."""

    name: str
    extensions: list[str] = Field(default_factory=list)   # lowercase, no dot; empty = any
    program: str
    args: list[str] = Field(default_factory=lambda: ["%PATH%"])


class ActionsSpec(_Model):
    open_with: list[OpenAction] = Field(default_factory=list)


# --------------------------------------------------------------------------- scopes


class AssetScope(_Model):
    """Which assets a linker considers (a subset of an AssetFilter plus duration bounds)."""

    media_type: str | None = None
    path_under: str | None = None
    tags_all: list[int] = Field(default_factory=list)
    tags_any: list[int] = Field(default_factory=list)
    in_categories: list[int] = Field(default_factory=list)
    min_duration_ms: int | None = None
    max_duration_ms: int | None = None


class TargetScope(_Model):
    """Where companion files live and which qualify."""

    directories: list[str] = Field(default_factory=list)
    recursive: bool = True
    extensions: list[str] = Field(default_factory=list)   # lowercase, no dot; empty = any
    ignore_globs: list[str] = Field(default_factory=list)
    min_size: int | None = None
    max_size: int | None = None


# --------------------------------------------------------------------------- top-level


class LinkerDefinition(_Model):
    schema_version: int = SCHEMA_VERSION
    source: AssetScope = Field(default_factory=AssetScope)
    target: TargetScope = Field(default_factory=TargetScope)
    clip: SideSpec = Field(default_factory=SideSpec)
    file: SideSpec = Field(default_factory=SideSpec)
    match: MatchSpec = Field(default_factory=MatchSpec)
    resolve: ResolveSpec = Field(default_factory=ResolveSpec)
    actions: ActionsSpec = Field(default_factory=ActionsSpec)
    label_template: str = "%name%"          # attachment label; %name%/%stem%/%ext% + file captures


def load_definition(definition_json: str) -> LinkerDefinition:
    """Parse + validate a stored ``definition_json``; raise :class:`InvalidInputError` on bad input."""
    try:
        return LinkerDefinition.model_validate_json(definition_json)
    except ValidationError as exc:
        raise InvalidInputError(f"invalid linker definition: {exc}") from exc


def dump_definition(definition: LinkerDefinition) -> str:
    return definition.model_dump_json()
