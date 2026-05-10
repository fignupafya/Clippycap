"""Domain events plus the :class:`EventBus` port.

Anything interested in "something happened" -- internal subscribers and plugins alike --
subscribes to these. Events are immutable value objects; publishing one must never fail the
operation that raised it (an :class:`EventBus` implementation swallows/logs subscriber errors).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, TypeVar


@dataclass(frozen=True, slots=True)
class Event:
    """Marker base for all domain events."""


@dataclass(frozen=True, slots=True)
class AssetAdded(Event):
    asset_id: int
    identity_hash: str
    media_type: str


@dataclass(frozen=True, slots=True)
class AssetUpdated(Event):
    asset_id: int


@dataclass(frozen=True, slots=True)
class AssetMissing(Event):
    asset_id: int


@dataclass(frozen=True, slots=True)
class AssetRemoved(Event):
    asset_id: int


@dataclass(frozen=True, slots=True)
class AssetOpened(Event):
    asset_id: int


@dataclass(frozen=True, slots=True)
class TagCreated(Event):
    tag_id: int
    name: str


@dataclass(frozen=True, slots=True)
class TagUpdated(Event):
    tag_id: int


@dataclass(frozen=True, slots=True)
class TagDeleted(Event):
    tag_id: int


@dataclass(frozen=True, slots=True)
class TagApplied(Event):
    asset_id: int
    tag_id: int


@dataclass(frozen=True, slots=True)
class TagUnapplied(Event):
    asset_id: int
    tag_id: int


@dataclass(frozen=True, slots=True)
class NoteCreated(Event):
    note_id: int
    asset_id: int


@dataclass(frozen=True, slots=True)
class NoteUpdated(Event):
    note_id: int
    asset_id: int


@dataclass(frozen=True, slots=True)
class NoteDeleted(Event):
    note_id: int
    asset_id: int


@dataclass(frozen=True, slots=True)
class ReferenceCreated(Event):
    reference_id: int
    from_asset_id: int
    to_asset_id: int


@dataclass(frozen=True, slots=True)
class ReferenceDeleted(Event):
    reference_id: int


@dataclass(frozen=True, slots=True)
class SourceAdded(Event):
    source_id: int
    path: str


@dataclass(frozen=True, slots=True)
class SourceRemoved(Event):
    source_id: int


@dataclass(frozen=True, slots=True)
class ScanStarted(Event):
    scan_id: str


@dataclass(frozen=True, slots=True)
class ScanProgress(Event):
    scan_id: str
    scanned: int
    total: int | None
    message: str = ""


@dataclass(frozen=True, slots=True)
class ScanCompleted(Event):
    scan_id: str
    added: int
    updated: int
    missing: int


_E = TypeVar("_E", bound=Event)
EventHandler = Callable[[Event], None]


class EventBus(Protocol):
    """Publish/subscribe for domain events. The implementation lives in :mod:`clippycap.plugins_runtime`."""

    def publish(self, event: Event) -> None: ...
    def subscribe(self, event_type: type[_E], handler: Callable[[_E], None]) -> None: ...
    def subscribe_all(self, handler: EventHandler) -> None: ...
