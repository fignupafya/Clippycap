"""Tests for the event bus, registries, and plugin discovery."""

from __future__ import annotations

import logging
import textwrap
from pathlib import Path

import pytest

from clippycap.core import events
from clippycap.infra.config import load_config
from clippycap.plugins_runtime.context import PluginContext
from clippycap.plugins_runtime.discovery import discover_and_load
from clippycap.plugins_runtime.event_bus import InProcessEventBus
from clippycap.plugins_runtime.registries import Registries, Registry

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TOML = REPO_ROOT / "config" / "default.toml"


def test_event_bus_dispatches_by_type_and_base() -> None:
    bus = InProcessEventBus()
    specific: list[events.Event] = []
    everything: list[events.Event] = []
    bus.subscribe(events.TagApplied, specific.append)
    bus.subscribe_all(everything.append)
    bus.publish(events.TagApplied(asset_id=1, tag_id=2))
    bus.publish(events.AssetAdded(asset_id=3, identity_hash="b3:x", media_type="video"))
    assert [type(e).__name__ for e in specific] == ["TagApplied"]
    assert [type(e).__name__ for e in everything] == ["TagApplied", "AssetAdded"]


def test_event_bus_swallows_subscriber_errors(caplog: pytest.LogCaptureFixture) -> None:
    bus = InProcessEventBus()
    calls: list[int] = []
    bus.subscribe_all(lambda _e: (_ for _ in ()).throw(RuntimeError("boom")))
    bus.subscribe_all(lambda _e: calls.append(1))
    with caplog.at_level(logging.ERROR):
        bus.publish(events.AssetUpdated(asset_id=1))   # must not raise
    assert calls == [1]
    assert "boom" in caplog.text


def test_registry_rejects_duplicates_and_requires() -> None:
    reg: Registry[str, int] = Registry("widget")
    reg.register("a", 1)
    assert reg.get("a") == 1
    assert reg.get("missing") is None
    assert reg.require("a") == 1
    assert "a" in reg
    with pytest.raises(ValueError, match="already registered"):
        reg.register("a", 2)
    with pytest.raises(LookupError):
        reg.require("missing")
    assert len(reg) == 1


def test_registries_bundle_is_empty_initially() -> None:
    regs = Registries()
    assert len(regs.media_types) == 0
    assert len(regs.identity_strategies) == 0
    assert regs.api_routers == []


def _write_plugin(directory: Path, name: str, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.py").write_text(textwrap.dedent(body).lstrip("\n"), encoding="utf-8")


def test_discovery_calls_register_and_respects_disabled(tmp_path: Path) -> None:
    plugins_dir = tmp_path / "plugins"
    _write_plugin(
        plugins_dir, "good_plugin",
        """
        def register(context):
            d = context.plugin_data_dir
            d.mkdir(parents=True, exist_ok=True)
            (d / "ran.txt").write_text(context.config.app.name, encoding="utf-8")
        """,
    )
    _write_plugin(plugins_dir, "off_plugin", "def register(context):\n    raise AssertionError\n")
    _write_plugin(plugins_dir, "broken_plugin", "def register(context):\n    raise RuntimeError('kaboom')\n")
    _write_plugin(plugins_dir, "no_entry_plugin", "X = 1\n")

    cfg = load_config(
        default_path=DEFAULT_TOML,
        data_dir_override=tmp_path / "data",
        install_dir_override=tmp_path / "install",
        env={},
        write_local_on_first_run=False,
    )
    ctx = PluginContext(
        registries=Registries(),
        event_bus=InProcessEventBus(),
        config=cfg,
        data_dir=tmp_path / "data",
        plugin_data_dir=tmp_path / "data" / "plugins",
        logger=logging.getLogger("test"),
    )
    loaded = discover_and_load(
        directories=[plugins_dir],
        enabled=[],
        disabled=["off_plugin"],
        base_context=ctx,
    )
    assert loaded == ["good_plugin"]
    assert (tmp_path / "data" / "plugins" / "good_plugin" / "ran.txt").read_text() == "Clippycap"
