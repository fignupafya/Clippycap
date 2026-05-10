"""Test that the composition root wires a complete, usable application from configuration."""

from __future__ import annotations

from pathlib import Path

from clippycap.app.bootstrap import build_application
from clippycap.app.services import AssetService
from clippycap.core.query import AssetFilter

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TOML = REPO_ROOT / "config" / "default.toml"
_NO_FFMPEG = {"CLIPPYCAP__MEDIA__FFMPEG__ENABLED": "false"}


def test_build_application_is_fully_wired(tmp_path: Path) -> None:
    app = build_application(
        default_toml_path=DEFAULT_TOML, data_dir_override=tmp_path / "data",
        install_dir_override=tmp_path / "inst", env=_NO_FFMPEG,
    )
    try:
        assert isinstance(app.assets, AssetService)
        assert app.loaded_plugins == []
        assert "video" in app.registries.media_types
        assert "blake3" in app.registries.identity_strategies
        assert app.data_dir.is_dir() and app.thumbnail_dir.is_dir() and app.tag_images_dir.is_dir()
        assert (app.data_dir / "library.sqlite").is_file()
        assert [rt.name for rt in app.reference_types.list_all()] == [
            "better version of", "same mistake as", "see also", "continues from",
        ]
        assert app.tags.list_all() == []
        assert app.assets.list_assets(filter=AssetFilter(), sort_key="added_desc", offset=0, limit=10).total == 0
    finally:
        app.shutdown()


def test_build_application_is_idempotent_on_first_run(tmp_path: Path) -> None:
    common = {
        "default_toml_path": DEFAULT_TOML, "data_dir_override": tmp_path / "data",
        "install_dir_override": tmp_path / "inst", "env": _NO_FFMPEG,
    }
    build_application(**common).shutdown()
    second = build_application(**common)   # second build must not re-seed or fail
    try:
        assert len(second.reference_types.list_all()) == 4
    finally:
        second.shutdown()
