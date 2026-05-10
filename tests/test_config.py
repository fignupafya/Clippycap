"""Tests for the layered configuration loader (default.toml -> local.toml -> env)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from clippycap.infra.config import LOCAL_FILENAME, Config, ConfigError, load_config

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TOML = REPO_ROOT / "config" / "default.toml"


def _load(
    tmp_path: Path,
    *,
    env: dict[str, str] | None = None,
    write_local: bool | None = False,
) -> Config:
    return load_config(
        default_path=DEFAULT_TOML,
        data_dir_override=tmp_path / "data",
        install_dir_override=tmp_path / "install",
        env={} if env is None else env,
        write_local_on_first_run=write_local,
    )


def _expect_config_error(default_path: Path, tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(
            default_path=default_path,
            data_dir_override=tmp_path / "data",
            install_dir_override=tmp_path / "install",
            env={},
            write_local_on_first_run=False,
        )


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_shipped_default(tmp_path: Path) -> None:
    cfg = _load(tmp_path)
    assert isinstance(cfg, Config)
    assert cfg.app.name == "Clippycap"
    assert cfg.identity.strategy == "blake3"
    assert cfg.media.ffmpeg.enabled is True
    assert "mp4" in cfg.media.video.extensions
    assert len(cfg.seed.reference_types) == 5
    assert cfg.ui.default_sort in cfg.sort
    assert cfg.player.default_speed in cfg.player.speeds


def test_path_tokens_are_expanded(tmp_path: Path) -> None:
    cfg = _load(tmp_path)
    data_dir = tmp_path / "data"
    assert cfg.app.data_dir == str(data_dir)
    assert cfg.thumbnails.cache_dir == str(data_dir / "thumbnails")
    assert cfg.logging.dir == str(data_dir / "logs")
    assert str(data_dir / "plugins") in cfg.plugins.dirs
    assert str(tmp_path / "install" / "plugins") in cfg.plugins.dirs
    # @videos is intentionally left for the first-run flow to resolve, not the config loader:
    assert cfg.firstrun.suggest_sources == ["@videos"]


def test_local_toml_overrides_default(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    (data_dir / LOCAL_FILENAME).write_text(
        '[ui]\ntheme = "light"\n\n[server]\nport = 9000\n', encoding="utf-8"
    )
    cfg = _load(tmp_path)
    assert cfg.ui.theme == "light"
    assert cfg.server.port == 9000
    assert cfg.ui.accent_color == "#ff6a2b"  # untouched keys keep the default


def test_env_overrides_beat_local_and_default(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    (data_dir / LOCAL_FILENAME).write_text('[ui]\ntheme = "light"\n', encoding="utf-8")
    cfg = _load(
        tmp_path,
        env={"CLIPPYCAP__UI__THEME": "dark", "CLIPPYCAP__SERVER__PORT": "8080"},
    )
    assert cfg.ui.theme == "dark"  # env beats local
    assert cfg.server.port == 8080  # the env string was coerced to int


def test_first_run_writes_local_then_reloads(tmp_path: Path) -> None:
    local_path = tmp_path / "data" / LOCAL_FILENAME
    assert not local_path.exists()
    _load(tmp_path, write_local=True)
    assert local_path.exists()
    text = local_path.read_text(encoding="utf-8")
    assert text.startswith("# Clippycap -- your local configuration overrides.")
    assert "[app]" in text and "Clippycap" in text  # it is a copy of default.toml
    # With local.toml now present, a normal load still succeeds:
    cfg = load_config(
        default_path=DEFAULT_TOML,
        data_dir_override=tmp_path / "data",
        install_dir_override=tmp_path / "install",
        env={},
    )
    assert cfg.app.name == "Clippycap"


def test_missing_required_key_fails(tmp_path: Path) -> None:
    text = re.sub(r"(?m)^name\s*=.*\n", "", DEFAULT_TOML.read_text(encoding="utf-8"), count=1)
    _expect_config_error(_write(tmp_path, "missing_key.toml", text), tmp_path)


def test_unknown_key_fails(tmp_path: Path) -> None:
    text = DEFAULT_TOML.read_text(encoding="utf-8").replace(
        "[app]\n", "[app]\nbogus_setting = 123\n", 1
    )
    _expect_config_error(_write(tmp_path, "unknown_key.toml", text), tmp_path)


def test_invalid_value_fails(tmp_path: Path) -> None:
    text = re.sub(
        r'accent_color\s*=\s*"[^"]*"',
        'accent_color = "not-a-hex-colour"',
        DEFAULT_TOML.read_text(encoding="utf-8"),
    )
    _expect_config_error(_write(tmp_path, "bad_value.toml", text), tmp_path)


def test_missing_default_file_fails(tmp_path: Path) -> None:
    _expect_config_error(tmp_path / "does-not-exist.toml", tmp_path)


def test_invalid_toml_fails(tmp_path: Path) -> None:
    _expect_config_error(_write(tmp_path, "bad.toml", "not = valid toml [[["), tmp_path)
