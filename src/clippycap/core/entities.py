"""Domain entities -- plain dataclasses, no I/O and no framework dependencies.

An entity whose ``id`` is ``None`` is *transient* (not yet persisted); repositories set the
``id`` on insert. Timestamps are timezone-aware UTC :class:`~datetime.datetime` (or ``None`` when
the repository has not yet assigned them). ``media_type`` is a free string ("video", ...) resolved
against the :class:`~clippycap.core.ports.MediaTypeProvider` registry at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class Asset:
    """One piece of media, identified by its content hash (the natural key)."""

    identity_hash: str                       # algo-prefixed, e.g. "b3:<hex>"
    media_type: str
    title: str                               # display name; defaults from the file name, user-editable
    size_bytes: int
    metadata: dict[str, Any] = field(default_factory=dict)   # media-type-specific (duration, fps, ...)
    # True between a scan's discovery phase (which records the asset) and its enrichment phase
    # (which reads the clip's duration / resolution into `metadata`); False once enriched.
    metadata_pending: bool = False
    added_at: datetime | None = None
    last_seen_at: datetime | None = None
    last_opened_at: datetime | None = None   # None => "new" (never opened in the app)
    id: int | None = None


@dataclass(slots=True)
class AssetPath:
    """A known on-disk location of an :class:`Asset`. An Asset may have several."""

    asset_id: int
    path: str                                # absolute, normalised
    volume_id: str | None = None             # filesystem volume id (de-dups the same file via 2 paths)
    present: bool = True                     # False => last scan did not find it here
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    id: int | None = None


@dataclass(slots=True)
class TagGroup:
    """A user-defined category that organises tags into a dimension (e.g. "players", "maps").
    Entirely user-created -- there are NO built-in groups. ``parent_id`` nests it under another
    category (``None`` => top-level). ``has_page`` opts it into its own page (a directory of its
    sub-categories + tags, plus the editable ``notes`` write-up); off / empty by default."""

    name: str                                # unique
    color: str = ""                          # hex "#rrggbb" or "" for none
    sort_order: int = 0
    has_page: bool = False
    parent_id: int | None = None             # nest under another category; None => top-level
    notes: str = ""                          # free-form markdown body shown on the category's page
    id: int | None = None


@dataclass(slots=True)
class Tag:
    """A user-defined label. ``icon`` and ``image_ref`` are mutually exclusive. ``group_id`` puts
    it under a :class:`TagGroup` (``None`` => uncategorised). ``has_page`` opts the tag into its
    own page (free-form ``notes`` + the clips carrying it); both off by default so a plain tag
    stays exactly as before."""

    name: str                                # unique
    color: str                               # hex "#rrggbb"
    icon: str | None = None                  # name from the bundled icon set
    image_ref: str | None = None             # filename of an uploaded image stored under the data dir
    description: str = ""
    sort_order: int = 0                      # display order; also the 1..9 quick-tag order
    group_id: int | None = None              # FK to TagGroup; None => uncategorised
    has_page: bool = False                   # show this tag its own page (notes + tagged clips)
    notes: str = ""                          # free-form markdown body shown on the tag's page
    created_at: datetime | None = None
    id: int | None = None


@dataclass(slots=True)
class Note:
    """Markdown text attached to an Asset.

    ``timestamp_ms is None``                        => the asset's single general note.
    ``timestamp_ms`` set, ``end_timestamp_ms`` None => pinned to one moment.
    both set                                        => covers the interval ``[timestamp_ms, end_timestamp_ms]``.
    """

    asset_id: int
    body: str
    timestamp_ms: int | None = None
    end_timestamp_ms: int | None = None     # set (with timestamp_ms) => an interval note
    created_at: datetime | None = None
    updated_at: datetime | None = None
    id: int | None = None


@dataclass(slots=True)
class ReferenceType:
    """An optional, user-defined kind of relationship. ``reverse_name`` set => directed."""

    name: str                                # unique
    color: str
    reverse_name: str | None = None
    sort_order: int = 0
    id: int | None = None


@dataclass(slots=True)
class Reference:
    """A directed link ``from_asset -> to_asset``; surfaced on both Assets."""

    from_asset_id: int
    to_asset_id: int
    type_id: int | None = None               # None => free-text label only
    label: str = ""
    from_timestamp_ms: int | None = None
    to_timestamp_ms: int | None = None
    note: str = ""
    created_at: datetime | None = None
    id: int | None = None


@dataclass(slots=True)
class Source:
    """A watched folder. ``media_types == []`` => scan for every registered media type."""

    path: str                                # absolute folder path
    recursive: bool = True
    enabled: bool = True
    media_types: list[str] = field(default_factory=list)
    last_scanned_at: datetime | None = None
    id: int | None = None


@dataclass(slots=True)
class SavedView:
    """A named, saved filter ("smart collection"). ``filter_json`` is a serialised AssetFilter."""

    name: str
    filter_json: str
    sort_key: str
    sort_order: int = 0
    id: int | None = None


@dataclass(slots=True)
class Linker:
    """A user-defined rule that auto-attaches companion files to assets (see LINKERS.md).

    ``definition_json`` is the whole rule serialised (a
    :class:`~clippycap.app.linking.types.LinkerDefinition`): the source/target scopes, how each side
    is read into typed fields, the match predicates, the resolution policy, and the open-with
    actions. It is a versioned JSON blob so the rule language can evolve and linkers export/import
    as JSON. ``enabled`` is the main-menu toggle; a disabled linker keeps its rows but stops matching.
    """

    name: str                                # unique
    definition_json: str
    description: str = ""
    color: str = ""                          # hex "#rrggbb" or "" for none
    enabled: bool = False
    sort_order: int = 0                      # display order / resolution priority
    schema_version: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None
    id: int | None = None


@dataclass(slots=True)
class Attachment:
    """A resolved link from an asset to a companion file on disk (produced by a :class:`Linker`).

    Identified by ``path`` (not a content hash -- companion files can be large and we don't read
    them). ``matched_json`` snapshots the field values that justified the link, for the "why"
    explanation. ``origin`` is ``"auto"`` for a rule-produced link or ``"manual"`` for a user pin.
    ``status`` flips to ``"missing"`` when the file vanishes (never auto-deleted -- the drive may
    just be unmounted)."""

    asset_id: int
    linker_id: int
    path: str
    label: str = ""
    ext: str = ""                            # lowercase, no leading dot
    score: float = 0.0
    matched: dict[str, Any] = field(default_factory=dict)
    status: str = "linked"                   # linked | missing
    origin: str = "auto"                     # auto | manual
    size: int | None = None
    mtime_ns: int | None = None
    created_at: datetime | None = None
    last_verified_at: datetime | None = None
    id: int | None = None


@dataclass(slots=True)
class AttachmentOverride:
    """A manual decision that survives every re-run: ``"pin"`` force-links a file even if the rule
    would not, ``"exclude"`` force-unlinks one the rule wrongly produced. Keyed by
    ``(asset_id, linker_id, path)`` so the resolver can honour it forever."""

    asset_id: int
    linker_id: int
    path: str
    decision: str                            # pin | exclude
    created_at: datetime | None = None
