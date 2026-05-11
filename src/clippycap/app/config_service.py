"""Editing the app's own configuration through the UI.

Writes user overrides into ``<data_dir>/local.toml`` (atomically) and returns a freshly merged
:class:`~clippycap.infra.config.Config`. The caller (the HTTP route) replaces the running
:class:`~clippycap.app.bootstrap.Application`'s ``config`` field with the new one -- frontend-facing
settings (player, keybindings) take effect immediately because the UI re-fetches via
``GET /api/config``; backend services (the ffmpeg editor, the scanner, the thumbnailer) captured
their config values at startup, so changes there only land on the next app launch.
"""

from __future__ import annotations

import logging
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import tomli_w

from clippycap.infra.config import LOCAL_FILENAME, Config, ConfigError, load_config

_log = logging.getLogger(__name__)


class ConfigService:
    """Persists partial configuration overrides and reloads the merged :class:`Config`."""

    def __init__(
        self,
        *,
        default_toml_path: Path,
        data_dir: Path,
        install_dir: Path,
        env: Mapping[str, str] | None,
    ) -> None:
        self._default_toml_path = default_toml_path
        self._data_dir = data_dir
        self._install_dir = install_dir
        self._env = env
        self._local_toml_path = data_dir / LOCAL_FILENAME

    def update(
        self,
        *,
        editing: dict[str, Any] | None = None,
        player: dict[str, Any] | None = None,
        keybindings: dict[str, str] | None = None,
    ) -> Config:
        """Apply a partial override, persist it, and return the reloaded :class:`Config`.

        Sections set to ``None`` are left untouched. If the reload fails validation, the previous
        ``local.toml`` is restored so the running app stays in a valid state.
        """
        previous = self._read_local()
        next_local: dict[str, Any] = dict(previous)
        sections: list[str] = []
        if editing is not None:
            next_local["editing"] = dict(editing)
            sections.append("editing")
        if player is not None:
            next_local["player"] = dict(player)
            sections.append("player")
        if keybindings is not None:
            next_local["keybindings"] = dict(keybindings)
            sections.append("keybindings")
        self._write_local(next_local)
        try:
            config = load_config(
                default_path=self._default_toml_path,
                data_dir_override=self._data_dir,
                install_dir_override=self._install_dir,
                env=self._env,
            )
        except ConfigError:
            # roll back so the running app stays valid
            self._write_local(previous)
            raise
        _log.info("config updated (%s)", ", ".join(sections) if sections else "<no changes>")
        return config

    def _read_local(self) -> dict[str, Any]:
        if not self._local_toml_path.is_file():
            return {}
        with self._local_toml_path.open("rb") as handle:
            return tomllib.load(handle)

    def _write_local(self, data: dict[str, Any]) -> None:
        self._local_toml_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._local_toml_path.with_name(self._local_toml_path.name + ".tmp")
        with tmp.open("wb") as handle:
            tomli_w.dump(data, handle)
        tmp.replace(self._local_toml_path)
