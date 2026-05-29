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
    """A user-defined, flat category that organises tags into a dimension (e.g. "players",
    "maps"). Entirely user-created -- there are NO built-in groups. ``has_page`` opts the group
    into its own hub view (a directory of its tags); off by default."""

    name: str                                # unique
    color: str = ""                          # hex "#rrggbb" or "" for none
    sort_order: int = 0
    has_page: bool = False
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
