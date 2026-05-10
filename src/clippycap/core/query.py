"""The asset-query specification used by services, the API, and saved views.

A :class:`AssetFilter` is intentionally serialisable to/from JSON (datetimes as ISO strings) so it
can be persisted in a :class:`~clippycap.core.entities.SavedView`. Valid *sort keys* are not listed
here -- they come from the ``[sort]`` section of the configuration; the repository maps each known
key to an ``ORDER BY`` clause and rejects unknown ones.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class AssetFilter:
    """What to include when listing assets. Empty lists / ``None`` fields mean "no constraint"."""

    media_type: str | None = None
    tags_all: list[int] = field(default_factory=list)    # asset must have ALL of these tag ids
    tags_any: list[int] = field(default_factory=list)    # asset must have AT LEAST ONE of these tag ids
    tags_none: list[int] = field(default_factory=list)   # asset must have NONE of these tag ids
    untagged_only: bool = False
    text: str | None = None                              # full-text query over titles + note bodies
    recorded_after: datetime | None = None
    recorded_before: datetime | None = None
    added_after: datetime | None = None
    only_missing: bool = False                           # the "missing files" view
    never_opened: bool = False                           # the "new" view (last_opened_at IS NULL)
