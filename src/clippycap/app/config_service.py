"""Editing the app's own configuration through the UI.

Writes user overrides into ``<data_dir>/local.toml`` (atomically, deep-merged onto whatever is there)
and returns a freshly merged :class:`~clippycap.infra.config.Config`. After a successful update the
shared :class:`~clippycap.infra.config.ConfigHolder` is swapped, so the services that read it through
the holder -- the ffmpeg video editor, the editing service, the ffmpeg resolver -- pick up the new
values on their very next call; the frontend re-fetches via ``GET /api/config``. If the merged result
fails validation the previous ``local.toml`` is restored so the running app stays in a valid state.
"""

from __future__ import annotations

import logging
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import tomli_w

from clippycap.infra.config import LOCAL_FILENAME, Config, ConfigError, ConfigHolder, load_config

_log = logging.getLogger(__name__)


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` onto a copy of ``base`` (sub-tables merge; scalars replace)."""
    merged: dict[str, Any] = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(value, Mapping) and isinstance(existing, Mapping):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


class ConfigService:
    """Persists partial configuration overrides and reloads the merged :class:`Config`."""

    def __init__(
        self,
        *,
        holder: ConfigHolder,
        default_toml_path: Path,
        data_dir: Path,
        install_dir: Path,
        env: Mapping[str, str] | None,
    ) -> None:
        self._holder = holder           # swapped after a successful update so live services see the new values
        self._default_toml_path = default_toml_path
        self._data_dir = data_dir
        self._install_dir = install_dir
        self._env = env
        self._local_toml_path = data_dir / LOCAL_FILENAME

    def update(
        self,
        *,
        editing: Mapping[str, Any] | None = None,
        player: Mapping[str, Any] | None = None,
        keybindings: Mapping[str, str] | None = None,
        media: Mapping[str, Any] | None = None,
        shell: Mapping[str, Any] | None = None,
    ) -> Config:
        """Apply a partial override, persist it (deep-merged onto ``local.toml``), and return the
        reloaded :class:`Config`. Sections passed as ``None`` are left untouched.

        :raises ConfigError: if the merged configuration fails validation -- ``local.toml`` is
            rolled back to its previous contents before the error propagates.
        """
        overrides: dict[str, Any] = {}
        if editing is not None:
            overrides["editing"] = dict(editing)
        if player is not None:
            overrides["player"] = dict(player)
        if keybindings is not None:
            overrides["keybindings"] = dict(keybindings)
        if media is not None:
            overrides["media"] = dict(media)
        if shell is not None:
            overrides["shell"] = dict(shell)
        previous = self._read_local()
        self._write_local(_deep_merge(previous, overrides))
        try:
            config = load_config(
                default_path=self._default_toml_path,
                data_dir_override=self._data_dir,
                install_dir_override=self._install_dir,
                env=self._env,
            )
        except ConfigError:
            self._write_local(previous)                      # keep the running app on a valid config
            raise
        self._holder.current = config       # live: the ffmpeg editor + editing service + resolver see this next call
        _log.info("config updated (%s)", ", ".join(overrides) if overrides else "<no changes>")
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
