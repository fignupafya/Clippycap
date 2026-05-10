"""Tests for the command-line entry point (the ``run`` command opens a window, so it is not tested)."""

from __future__ import annotations

from pathlib import Path

from clippycap.app.bootstrap import build_application
from clippycap.shell.cli import main

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_add_source_cli(tmp_path: Path) -> None:
    library = tmp_path / "lib"
    library.mkdir()
    data_dir = tmp_path / "data"
    # need the shipped default.toml on @install -- run from the repo (default_install_dir() == repo root)
    code = main(["--data-dir", str(data_dir), "add-source", str(library)])
    assert code == 0
    app = build_application(default_toml_path=REPO_ROOT / "config" / "default.toml",
                            data_dir_override=data_dir, env={"CLIPPYCAP__MEDIA__FFMPEG__ENABLED": "false"})
    try:
        sources = app.sources.list_all()
        assert len(sources) == 1 and sources[0].path == str(library.resolve())
    finally:
        app.shutdown()


def test_add_source_cli_bad_path_returns_error(tmp_path: Path) -> None:
    code = main(["--data-dir", str(tmp_path / "data"), "add-source", str(tmp_path / "nope")])
    assert code == 2


def test_scan_cli_runs_to_completion(tmp_path: Path) -> None:
    library = tmp_path / "lib"
    library.mkdir()
    (library / "clip.mp4").write_text("DATA")
    data_dir = tmp_path / "data"
    main(["--data-dir", str(data_dir), "add-source", str(library)])
    assert main(["--data-dir", str(data_dir), "scan"]) == 0   # files newer than the skip-window -> "done" with 0 added
