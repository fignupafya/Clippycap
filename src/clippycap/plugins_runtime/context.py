"""The :class:`PluginContext` handed to every plugin's ``register(context)`` entry point."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from clippycap.core.events import EventBus
from clippycap.infra.config import Config

from .registries import Registries


@dataclass
class PluginContext:
    """Everything a plugin gets in order to wire itself in. Treat ``config`` as read-only."""

    registries: Registries
    event_bus: EventBus
    config: Config
    data_dir: Path
    plugin_data_dir: Path        # <data_dir>/plugins/<plugin_name> -- the plugin's own scratch space
    logger: logging.Logger
