"""Application configuration: a validated schema plus the layered loader."""

from __future__ import annotations

from .loader import LOCAL_FILENAME, ConfigError, load_config
from .schema import Config


class ConfigHolder:
    """Mutable reference to the active :class:`Config`.

    Long-lived services (the ffmpeg editor, the editing service, ...) keep a *holder* rather than
    a bare ``Config`` value, so settings edited at runtime via ``PUT /api/config`` (which swaps
    ``holder.current``) are picked up on the very next call. The held :class:`Config` itself stays
    immutable (its sections are ``frozen=True``); only the reference is replaced.
    """

    __slots__ = ("current",)

    def __init__(self, config: Config) -> None:
        self.current: Config = config


__all__ = ["LOCAL_FILENAME", "Config", "ConfigError", "ConfigHolder", "load_config"]
