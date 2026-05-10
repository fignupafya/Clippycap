"""Command-line entry point / desktop launcher.

  clippycap                       -- run the server and open the UI in the default browser
  clippycap add-source <folder>   -- add a library source folder
  clippycap scan [<source-id>]    -- scan all enabled sources (or one), printing progress
  --data-dir <path>               -- override where the library / config / caches live

A native ``pywebview`` window (instead of the browser) is a planned enhancement; ``[shell].mode`` in
the config already reserves it.
"""

from __future__ import annotations

import argparse
import logging
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

from clippycap.api.app import create_app
from clippycap.app.bootstrap import Application, build_application
from clippycap.core.errors import ClippycapError
from clippycap.infra.config.loader import default_install_dir


def _default_toml_path() -> Path:
    return default_install_dir() / "config" / "default.toml"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _build(data_dir: Path | None) -> Application:
    return build_application(default_toml_path=_default_toml_path(), data_dir_override=data_dir, env=None)


def _cmd_run(args: argparse.Namespace) -> int:
    application = _build(args.data_dir)
    api = create_app(application)
    host = application.config.server.host
    port = application.config.server.port or _free_port()
    url = f"http://{host}:{port}/"
    if not getattr(args, "no_browser", False):
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"{application.config.app.name} running at {url}  (Ctrl+C to stop)")
    try:
        uvicorn.run(api, host=host, port=port, log_level="warning")
    finally:
        application.shutdown()
    return 0


def _cmd_add_source(args: argparse.Namespace) -> int:
    application = _build(args.data_dir)
    try:
        source = application.sources.create(args.path)
        print(f"added source #{source.id}: {source.path}")
    finally:
        application.shutdown()
    return 0


def _cmd_scan(args: argparse.Namespace) -> int:
    application = _build(args.data_dir)
    try:
        job_id = (
            application.scans.scan_source(args.source_id)
            if args.source_id is not None
            else application.scans.scan_all()
        )
        print(f"scan job {job_id} started ...")
        last_count = -1
        while True:
            handle = application.jobs.get(job_id)
            if handle is None:
                return 0
            if handle.scanned != last_count:
                last_count = handle.scanned
                print(f"  ... {handle.scanned} files seen   {handle.message}")
            if handle.state in ("done", "error"):
                print(f"scan {handle.state}: {handle.error or 'ok'}")
                return 0 if handle.state == "done" else 1
            time.sleep(0.25)
    finally:
        application.shutdown()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="clippycap", description="Local extensible media library + annotation tool")
    parser.add_argument("--data-dir", type=Path, default=None, help="override the application data directory")
    sub = parser.add_subparsers(dest="command")
    run_p = sub.add_parser("run", help="run the server and open the UI (the default action)")
    run_p.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    add_p = sub.add_parser("add-source", help="add a library source folder")
    add_p.add_argument("path", help="path to a folder to watch for media")
    scan_p = sub.add_parser("scan", help="scan sources for media files")
    scan_p.add_argument("source_id", nargs="?", type=int, default=None, help="optional: scan just this source id")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    args = _build_parser().parse_args(argv)
    handlers: dict[str | None, object] = {
        None: _cmd_run, "run": _cmd_run, "add-source": _cmd_add_source, "scan": _cmd_scan,
    }
    handler = handlers[args.command]
    assert callable(handler)
    try:
        result = handler(args)
        assert isinstance(result, int)
        return result
    except ClippycapError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
