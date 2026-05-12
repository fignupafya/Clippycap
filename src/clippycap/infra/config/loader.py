"""Loads the layered application configuration.

Sources, lowest precedence first:

1. ``config/default.toml`` -- shipped with the app; the *only* place defaults are defined.
2. ``<data_dir>/local.toml`` -- the user's overrides; written from a copy of (1) on first run.
3. environment variables ``CLIPPYCAP__SECTION__KEY=value`` -- for deployment/ops.

The merged mapping has its ``@path`` tokens expanded and is then validated against
:class:`~clippycap.infra.config.schema.Config`. There are *no* code-level defaults: if
``config/default.toml`` is missing a key, validation fails loudly.

``@path`` tokens (replaced wherever a string value *starts* with one):

* ``@appdata`` -- the OS application-data directory
* ``@data``    -- the resolved application data directory (``[app].data_dir``)
* ``@install`` -- the directory the app is installed/run from (the bundle dir when frozen)
* ``@bundled`` -- the bundled-tools directory (``@install/bin``)

(``@videos`` may appear in ``[firstrun].suggest_sources``; it is resolved later by the
first-run flow, not here, because it needs the OS "known folders" API.)
"""

from __future__ import annotations

import os
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .schema import Config

ENV_PREFIX = "CLIPPYCAP__"
ENV_NESTED_SEPARATOR = "__"
LOCAL_FILENAME = "local.toml"

_LOCAL_FILE_HEADER = (
    "# Clippycap -- your local configuration overrides.\n"
    "# Any key set here wins over config/default.toml. Edit freely; delete a key to fall\n"
    "# back to the shipped default. (Created on first run as a copy of default.toml.)\n"
    "\n"
)


class ConfigError(Exception):
    """Raised when the configuration cannot be read, parsed, or validated."""


# --------------------------------------------------------------------------- locations


def os_appdata_dir() -> Path:
    """The OS convention for a per-user application-data directory."""
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise ConfigError(
                "Cannot resolve @appdata: the APPDATA environment variable is not set"
            )
        return Path(appdata)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def default_install_dir() -> Path:
    """Where the app is installed/run from: the bundle dir when frozen, else the repo root."""
    if getattr(sys, "frozen", False):
        # PyInstaller: bundled data (config/, web/dist/, bin/) lives in sys._MEIPASS -- a temp dir for
        # a one-file build, or the _internal/ dir for a one-folder build; fall back to the exe's dir.
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    # this file is src/clippycap/infra/config/loader.py:
    #   parents -> [0]=config [1]=infra [2]=clippycap [3]=src [4]=repo root
    return Path(__file__).resolve().parents[4]


# --------------------------------------------------------------------------- merging / env


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` onto a copy of ``base`` (sub-tables merge; others replace)."""
    merged: dict[str, Any] = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(value, Mapping) and isinstance(existing, Mapping):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def _env_override_tree(env: Mapping[str, str]) -> dict[str, Any]:
    """Turn ``CLIPPYCAP__A__B=value`` variables into a nested ``{"a": {"b": "value"}}`` mapping.

    Values stay as strings; Pydantic coerces them to bool/int/float where the schema requires it.
    """
    tree: dict[str, Any] = {}
    for raw_key, raw_value in env.items():
        if not raw_key.startswith(ENV_PREFIX):
            continue
        parts = [p.lower() for p in raw_key[len(ENV_PREFIX):].split(ENV_NESTED_SEPARATOR) if p]
        if not parts:
            continue
        cursor: dict[str, Any] = tree
        for part in parts[:-1]:
            child = cursor.setdefault(part, {})
            if not isinstance(child, dict):
                raise ConfigError(f"Conflicting environment override for {raw_key!r}")
            cursor = child
        cursor[parts[-1]] = raw_value
    return tree


# --------------------------------------------------------------------------- token expansion


def _expand_token(value: str, tokens: Mapping[str, Path]) -> str:
    """Replace a leading ``@token`` (alone, or followed by ``/`` or ``\\``) with its path."""
    for name in sorted(tokens, key=len, reverse=True):
        marker = "@" + name
        if value == marker:
            return str(tokens[name])
        for separator in ("/", "\\"):
            prefix = marker + separator
            if value.startswith(prefix):
                return str(tokens[name] / value[len(prefix):])
    return value


def _expand_tokens(node: Any, tokens: Mapping[str, Path]) -> Any:
    if isinstance(node, str):
        return _expand_token(node, tokens)
    if isinstance(node, Mapping):
        return {key: _expand_tokens(value, tokens) for key, value in node.items()}
    if isinstance(node, list):
        return [_expand_tokens(item, tokens) for item in node]
    return node


# --------------------------------------------------------------------------- TOML reading


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"Configuration file not found: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"Cannot read configuration file {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc


# --------------------------------------------------------------------------- public API


def load_config(
    *,
    default_path: Path,
    data_dir_override: Path | None = None,
    install_dir_override: Path | None = None,
    env: Mapping[str, str] | None = None,
    write_local_on_first_run: bool | None = None,
) -> Config:
    """Load, merge, expand, and validate the configuration.

    :param default_path: path to ``config/default.toml`` (the shipped defaults).
    :param data_dir_override: force the application data directory (where ``local.toml`` lives,
        and what ``@data`` resolves to); otherwise it comes from ``[app].data_dir`` in
        ``default.toml`` (which may only use ``@appdata`` / ``@install``).
    :param install_dir_override: force ``@install``; otherwise auto-detected.
    :param env: environment mapping (defaults to :data:`os.environ`).
    :param write_local_on_first_run: force the first-run ``local.toml`` write on/off; otherwise
        follow ``[app].write_local_on_first_run``.
    :raises ConfigError: if a file is missing/unreadable, the TOML is invalid, the data dir is
        misconfigured, or the merged configuration fails validation.
    """
    environ = os.environ if env is None else env
    install_dir = install_dir_override or default_install_dir()
    appdata_dir = os_appdata_dir()

    default_raw = _read_toml(default_path)

    # The data dir can't be relocated via local.toml (that file lives inside it), so it is
    # taken from default.toml's [app].data_dir -- or the explicit override.
    if data_dir_override is not None:
        data_dir = data_dir_override
    else:
        try:
            raw_data_dir = default_raw["app"]["data_dir"]
        except (KeyError, TypeError) as exc:
            raise ConfigError(f"{default_path}: missing [app].data_dir") from exc
        data_dir = Path(
            _expand_token(str(raw_data_dir), {"appdata": appdata_dir, "install": install_dir})
        )
        if str(data_dir).startswith("@"):
            raise ConfigError(
                f"{default_path}: [app].data_dir may only use @appdata or @install "
                f"(got {raw_data_dir!r})"
            )

    tokens: dict[str, Path] = {
        "appdata": appdata_dir,
        "install": install_dir,
        "data": data_dir,
        "bundled": install_dir / "bin",
    }

    local_path = data_dir / LOCAL_FILENAME
    local_existed = local_path.exists()
    local_raw = _read_toml(local_path) if local_existed else {}
    env_raw = _env_override_tree(environ)

    merged = _deep_merge(_deep_merge(default_raw, local_raw), env_raw)
    # Keep [app].data_dir consistent with the dir we actually used.
    if isinstance(merged.get("app"), dict):
        merged["app"] = {**merged["app"], "data_dir": str(data_dir)}
    expanded = _expand_tokens(merged, tokens)

    try:
        config = Config.model_validate(expanded)
    except ValidationError as exc:
        raise ConfigError(f"Configuration is invalid:\n{exc}") from exc

    should_write = (
        config.app.write_local_on_first_run
        if write_local_on_first_run is None
        else write_local_on_first_run
    )
    if should_write and not local_existed:
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            local_path.write_text(
                _LOCAL_FILE_HEADER + default_path.read_text(encoding="utf-8"), encoding="utf-8"
            )
        except OSError as exc:
            raise ConfigError(f"Cannot write first-run config to {local_path}: {exc}") from exc

    return config
