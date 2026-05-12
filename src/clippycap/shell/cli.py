"""Command-line entry point / desktop launcher.

  clippycap                       -- run the server and open the UI (a native window by default; see
                                     [shell].mode in the config: "pywebview" -> native window, "browser")
  clippycap run --browser         -- use the default browser instead of the native window
  clippycap run --no-browser      -- run the server only, open nothing
  clippycap add-source <folder>   -- add a library source folder
  clippycap scan [<source-id>]    -- scan all enabled sources (or one), printing progress
  --data-dir <path>               -- override where the library / config / caches live

Closing the native window stops the server and exits.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

from clippycap.api.app import create_app
from clippycap.app.bootstrap import Application, build_application
from clippycap.core.errors import ClippycapError
from clippycap.infra.config.loader import default_install_dir
from clippycap.infra.config.schema import ShellConfig


def _default_toml_path() -> Path:
    return default_install_dir() / "config" / "default.toml"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _build(data_dir: Path | None) -> Application:
    return build_application(default_toml_path=_default_toml_path(), data_dir_override=data_dir, env=None)


# Chromium "app mode" (a chromeless window, no tabs / address bar) -- the fallback when pywebview
# isn't installed. Chrome first (its app mode is quieter than Edge's), then Edge (on every Windows).
_APP_BROWSERS = (
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
)
# Edge especially likes to pop welcome / sidebar / Copilot / Discover windows on a fresh profile;
# turn off as many of those as we can (Chromium just ignores flag names it doesn't know).
_QUIET_FEATURES = ",".join((
    "Translate", "msEdgeWelcomeUX", "msEdgeFirstRunUX", "EdgeWelcomeUX", "msImplicitSignin",
    "msSpartanFeatures", "msEdgeSplitScreen", "msUndersideButton", "msSidebarV2", "msEdgeSideBarV2",
    "msEdgeCopilot", "msEdgeDiscoverEntrypoint", "EdgeDiscoverEntrypoint", "msEdgeNTPCardsRefresh",
))


def _show_native_window(url: str, shell: ShellConfig) -> bool:
    """Open the UI as a desktop-app window, blocking until it is closed. Returns ``False`` if no
    suitable window can be opened (so the caller can fall back to a normal browser tab)."""
    return _show_pywebview(url, shell) or _open_app_window(url, shell)


def _show_pywebview(url: str, shell: ShellConfig) -> bool:
    try:
        import webview  # type: ignore  # noqa: PLC0415  -- the package is "pywebview"; module is "webview"
    except ImportError:
        return False
    try:
        webview.create_window(
            shell.window_title, url, width=shell.window_width, height=shell.window_height, min_size=(720, 480)
        )
        webview.start()                                   # blocks until the window is closed
        return True
    except Exception as exc:
        logging.getLogger(__name__).warning("pywebview unavailable (%s)", exc)
        return False


def _open_app_window(url: str, shell: ShellConfig) -> bool:
    exe = ""
    for raw in _APP_BROWSERS:
        candidate = os.path.expandvars(raw)
        if os.path.isfile(candidate):
            exe = candidate
            break
    if not exe:
        return False
    profile = tempfile.mkdtemp(prefix="clippycap-window-")          # an isolated profile so closing the window exits it
    try:
        proc = subprocess.Popen([
            exe, f"--app={url}", f"--user-data-dir={profile}",
            f"--window-size={shell.window_width},{shell.window_height}",
            "--no-first-run", "--no-default-browser-check", "--disable-search-engine-choice-screen",
            "--disable-extensions", "--disable-sync", "--disable-component-update",
            "--disable-background-networking", "--disable-default-apps", "--no-service-autorun",
            "--password-store=basic", f"--disable-features={_QUIET_FEATURES}",
        ])
    except OSError:
        shutil.rmtree(profile, ignore_errors=True)
        return False
    with contextlib.suppress(KeyboardInterrupt):
        proc.wait()                                                # blocks until the window is closed
    shutil.rmtree(profile, ignore_errors=True)
    return True


def _cmd_run(args: argparse.Namespace) -> int:
    application = _build(args.data_dir)
    api = create_app(application)
    cfg = application.config
    host = cfg.server.host
    port = cfg.server.port or _free_port()
    url = f"http://{host}:{port}/"
    server = uvicorn.Server(uvicorn.Config(api, host=host, port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    thread.start()
    for _ in range(120):                                  # ~6 s grace period for the server to come up
        if server.started:
            break
        time.sleep(0.05)
    try:
        mode = ("browser" if getattr(args, "browser", False)
                else "none" if getattr(args, "no_browser", False)
                else cfg.shell.mode)
        if mode == "pywebview":
            print(f"{cfg.app.name} -- desktop window  ({url})")
            if _show_native_window(url, cfg.shell):
                return 0                                  # the window was closed -> done
            mode = "browser"                              # no webview backend -> fall through
        if mode == "browser":
            webbrowser.open(url)
        print(f"{cfg.app.name} running at {url}  (Ctrl+C to stop)")
        with contextlib.suppress(KeyboardInterrupt):
            thread.join()
        return 0
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        application.shutdown()


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
    run_p = sub.add_parser("run", help="run the server and open the UI (a native window by default)")
    run_p.add_argument("--browser", action="store_true", help="open the default browser instead of a native window")
    run_p.add_argument("--no-browser", action="store_true", help="run the server only; open no window or browser")
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
