"""Domain-level exceptions (no I/O, no framework dependencies).

The API layer maps these to HTTP status codes; the rest of the app raises them.
``ConfigError`` is deliberately *not* here -- configuration failures are an
infrastructure concern and live in :mod:`clippycap.infra.config`.
"""

from __future__ import annotations


class ClippycapError(Exception):
    """Base class for every application-level error."""


class NotFoundError(ClippycapError):
    """A requested entity does not exist."""


class ConflictError(ClippycapError):
    """An operation conflicts with current state (e.g. a duplicate tag name)."""


class InvalidInputError(ClippycapError):
    """Input the domain rejects (unknown sort key, malformed filter, ...)."""


class UnsupportedError(ClippycapError):
    """A requested operation is not supported here (e.g. server-side thumbnailing with no ffmpeg)."""
