"""An in-process publish/subscribe :class:`~clippycap.core.events.EventBus`.

Dispatch is by the event's class *and its base classes*: subscribing to
:class:`~clippycap.core.events.Event` receives every event. Subscriber exceptions are caught and
logged -- publishing an event must never break the operation that raised it.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from collections.abc import Callable

from clippycap.core.events import Event, EventHandler

_log = logging.getLogger(__name__)


class InProcessEventBus:
    """Synchronous, thread-safe event bus suitable for a single-process app."""

    def __init__(self) -> None:
        self._handlers: dict[type[Event], list[EventHandler]] = defaultdict(list)
        self._lock = threading.Lock()

    def publish(self, event: Event) -> None:
        with self._lock:
            handlers: list[EventHandler] = []
            for cls in type(event).__mro__:
                if isinstance(cls, type) and issubclass(cls, Event):
                    handlers.extend(self._handlers.get(cls, ()))
        for handler in handlers:
            try:
                handler(event)
            except Exception:
                # A misbehaving subscriber must not break the operation that published the event.
                _log.exception("event subscriber %r failed handling %r", handler, event)

    def subscribe(self, event_type: type[Event], handler: Callable[..., None]) -> None:
        with self._lock:
            self._handlers[event_type].append(handler)

    def subscribe_all(self, handler: EventHandler) -> None:
        self.subscribe(Event, handler)
