"""Discovering and loading plugins.

A *plugin* is a Python package or single-file module placed in one of the configured plugin
directories and exposing ``def register(context: PluginContext) -> None``. Each is imported in
isolation; an import or registration failure is logged and the plugin is skipped -- a bad plugin
must never crash the application.
"""

from __future__ import annotations

import importlib
import logging
import sys
from collections.abc import Iterable, Sequence
from dataclasses import replace
from pathlib import Path

from .context import PluginContext

_log = logging.getLogger(__name__)
_ENTRY_POINT = "register"


def _candidate_names(directory: Path) -> list[str]:
    """Top-level importable names directly under ``directory`` (packages and single-file modules)."""
    if not directory.is_dir():
        return []
    names: list[str] = []
    for entry in sorted(directory.iterdir()):
        if entry.name.startswith((".", "_")):
            continue
        if entry.is_dir() and (entry / "__init__.py").is_file():
            names.append(entry.name)
        elif entry.is_file() and entry.suffix == ".py":
            names.append(entry.stem)
    return names


def _should_load(name: str, enabled: Sequence[str], disabled: Sequence[str]) -> bool:
    if name in disabled:
        return False
    return not enabled or name in enabled


def discover_and_load(
    *,
    directories: Iterable[Path],
    enabled: Sequence[str],
    disabled: Sequence[str],
    base_context: PluginContext,
) -> list[str]:
    """Import every eligible plugin and call its ``register``; return the names that loaded OK."""
    loaded: list[str] = []
    seen: set[str] = set()
    for raw_dir in directories:
        directory = raw_dir.resolve()
        if not directory.is_dir():
            continue
        if str(directory) not in sys.path:
            sys.path.append(str(directory))
            importlib.invalidate_caches()
        for name in _candidate_names(directory):
            if name in seen:
                _log.warning("plugin %r found in more than one directory; ignoring the later one", name)
                continue
            seen.add(name)
            if not _should_load(name, enabled, disabled):
                _log.info("plugin %r is disabled by configuration", name)
                continue
            try:
                module = importlib.import_module(name)
            except Exception:
                _log.exception("failed to import plugin %r", name)
                continue
            entry = getattr(module, _ENTRY_POINT, None)
            if not callable(entry):
                _log.warning("plugin %r has no callable %s(); skipping", name, _ENTRY_POINT)
                continue
            ctx = replace(
                base_context,
                plugin_data_dir=base_context.data_dir / "plugins" / name,
                logger=logging.getLogger(f"clippycap.plugin.{name}"),
            )
            try:
                entry(ctx)
            except Exception:
                _log.exception("plugin %r failed in %s()", name, _ENTRY_POINT)
                continue
            loaded.append(name)
            _log.info("loaded plugin %r", name)
    return loaded
