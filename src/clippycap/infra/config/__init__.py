"""Application configuration: a validated schema plus the layered loader."""

from __future__ import annotations

from .loader import LOCAL_FILENAME, ConfigError, load_config
from .schema import Config

__all__ = ["LOCAL_FILENAME", "Config", "ConfigError", "load_config"]
