"""Registries -- the "plug things in without editing the core" mechanism.

A :class:`Registry` is a name -> component map that refuses duplicate registrations. The app
creates one :class:`Registries` bundle at startup; built-in components register themselves through
it exactly as a third-party plugin would.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

from clippycap.core.ports import (
    IdentityStrategy,
    MediaTypeProvider,
    PortableExporter,
    PortableImporter,
)


class Registry[K, V]:
    """A name -> component map; registering the same key twice is an error."""

    def __init__(self, kind: str) -> None:
        self._kind = kind
        self._items: dict[K, V] = {}

    def register(self, key: K, value: V) -> None:
        if key in self._items:
            raise ValueError(f"{self._kind} {key!r} is already registered")
        self._items[key] = value

    def get(self, key: K) -> V | None:
        return self._items.get(key)

    def require(self, key: K) -> V:
        try:
            return self._items[key]
        except KeyError:
            raise LookupError(f"no {self._kind} registered as {key!r}") from None

    def items(self) -> Mapping[K, V]:
        return dict(self._items)

    def __iter__(self) -> Iterator[V]:
        return iter(list(self._items.values()))

    def __contains__(self, key: object) -> bool:
        return key in self._items

    def __len__(self) -> int:
        return len(self._items)


class Registries:
    """The full set of registries handed to plugins and used by the composition root."""

    def __init__(self) -> None:
        self.media_types: Registry[str, MediaTypeProvider] = Registry("media type")
        self.identity_strategies: Registry[str, IdentityStrategy] = Registry("identity strategy")
        self.exporters: Registry[str, PortableExporter] = Registry("exporter")
        self.importers: Registry[str, PortableImporter] = Registry("importer")
        # FastAPI routers contributed by plugins (kept loosely typed so this module needs no web
        # framework dependency); the API layer mounts each one under a namespaced prefix.
        self.api_routers: list[Any] = []
