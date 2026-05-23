"""Command-line entry point / desktop launcher.

  clippycap                       -- run the server and open the UI as a desktop window (pywebview --
                                     a frameless window with our own title bar; see [shell].mode in
                                     the config: "pywebview" -> native window, "browser" -> a tab)
  clippycap run --browser         -- open the default browser as a tab instead of the native window
  clippycap run --no-browser      -- run the server only, open nothing
  clippycap add-source <folder>   -- add a library source folder
  clippycap scan [<source-id>]    -- scan all enabled sources (or one), printing progress
  --data-dir <path>               -- override where the library / config / caches live

Closing the window stops the server and exits. A second instance refuses to start (the first holds a
lock on <data_dir>/.lock).
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
from typing import Any

import uvicorn

from clippycap.api.app import create_app
from clippycap.app.bootstrap import Application, build_application
from clippycap.core.errors import ClippycapError
from clippycap.infra.config.loader import default_install_dir
from clippycap.infra.config.schema import ShellConfig

_log = logging.getLogger(__name__)

# Minimum window size -- below this the sidebar + grid stop being usable.
_WINDOW_MIN_SIZE = (820, 560)
_MIN_SANE_WINDOW_PX = 200      # don't persist obviously-bogus dimensions (e.g. a minimized window)
_SERVER_START_TIMEOUT = 10.0  # seconds to wait for uvicorn to come up before pointing the window at it

# Shown instantly in the (frameless) window while uvicorn finishes starting; replaced by the SPA via
# load_url() the moment the server is up. Self-contained -- no external assets to bundle.
_SPLASH_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;height:100%;background:#0e1014;color:#e7eaf0;
 font-family:'Segoe UI Variable Text','Segoe UI',system-ui,sans-serif;overflow:hidden}
.w{height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:18px;
 -webkit-user-select:none;user-select:none}
.n{font-size:30px;font-weight:700;letter-spacing:.04em}.n .c{color:#7c5cf5}
.b{width:170px;height:3px;border-radius:3px;background:#222732;overflow:hidden;position:relative}
.b::after{content:"";position:absolute;top:0;bottom:0;left:-40%;width:40%;background:#7c5cf5;border-radius:3px;
 animation:s 1.1s ease-in-out infinite}@keyframes s{0%{left:-40%}100%{left:100%}}
.s{font-size:12px;color:#6b7480;letter-spacing:.06em}
</style></head><body><div class="w"><div class="n"><span class="c">C</span>lippycap</div>
<div class="b"></div><div class="s">starting…</div></div></body></html>"""

# Chromium "app mode" (a chromeless window, no tabs / address bar) -- the fallback when pywebview's
# WebView2 backend isn't available. Chrome first (its app mode is quieter than Edge's), then Edge.
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
    "msEdgeShoppingFeature", "msEdgeCoupons", "EdgeShoppingAssistant", "msEdgeCollections",
    "msEdgeAutofillFeatureFlag", "msEdgeEditorFeature", "EdgeEditor", "msEdgeReadAloud",
))


def _default_toml_path() -> Path:
    return default_install_dir() / "config" / "default.toml"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _build(data_dir: Path | None) -> Application:
    return build_application(default_toml_path=_default_toml_path(), data_dir_override=data_dir, env=None)


def _wait_until_started(server: uvicorn.Server, *, timeout: float = _SERVER_START_TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.03)


def _acquire_single_instance_lock(data_dir: Path) -> Any:
    """Take an exclusive lock on ``<data_dir>/.lock``. Returns the open file (the caller must keep it
    alive for the process's lifetime) on success, or ``None`` if another instance already holds it.
    The OS releases the lock when the process exits, so a crash never leaves it stuck."""
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        handle = (data_dir / ".lock").open("a+b")
    except OSError:
        return object()   # can't even create the lock file -> don't block startup over it
    try:
        if sys.platform == "win32":
            import msvcrt  # noqa: PLC0415

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl  # noqa: PLC0415

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def _persist_window_size(application: Application, width: int, height: int) -> None:
    if width < _MIN_SANE_WINDOW_PX or height < _MIN_SANE_WINDOW_PX:   # minimized / nonsense -- ignore
        return
    with contextlib.suppress(Exception):
        shell_cfg = application.config.shell
        application.config_service.update(shell={
            **shell_cfg.model_dump(mode="python"), "window_width": width, "window_height": height,
        })


# Win32 hit-test code for WM_NCLBUTTONDOWN: we synthesize this with HTCAPTION to hand a mouse-down
# off to Windows as if the user had clicked a native title bar -- DefWindowProc then enters its modal
# move loop, which is what gives us Aero Snap (drag-to-edge → half-screen preview), the proper drag
# cursor, and double-click-to-maximize. NB: resize uses a different mechanism (JS-driven, below),
# because Windows' modal SIZE loop silently bails out on a window without ``WS_THICKFRAME``, and
# adding ``WS_THICKFRAME`` to a frameless WinForms form would paint a visible gray sizing border that
# we'd then need a fragile ctypes WndProc subclass to hide via ``WM_NCCALCSIZE``.
_WM_NCLBUTTONDOWN = 0x00A1
_HTCAPTION = 2


def _get_form(window: Any) -> Any | None:
    """Best-effort lookup of the WinForms ``BrowserView`` form backing a pywebview ``Window``.
    Returns ``None`` off-Windows or if pywebview's internals have changed."""
    if sys.platform != "win32" or window is None:
        return None
    try:
        from webview.platforms.winforms import BrowserView  # noqa: PLC0415

        form = BrowserView.instances.get(window.uid)
        return form if form is not None and not form.IsDisposed else None
    except Exception:
        return None


class _WindowApi:
    """Exposed to the renderer as ``window.pywebview.api`` -- the custom title bar's buttons call these.
    (``_window`` / ``_maximized`` start with ``_`` so pywebview doesn't expose them to the page.)"""

    def __init__(self) -> None:
        self._window: Any = None
        self._maximized = False

    def minimize(self) -> None:
        if self._window is not None:
            self._window.minimize()

    def toggle_maximize(self) -> None:
        if self._window is None:
            return
        if self._maximized:
            self._window.restore()
        else:
            self._window.maximize()
        self._maximized = not self._maximized

    def close(self) -> None:
        if self._window is not None:
            self._window.destroy()

    def start_drag(self) -> None:
        """Hand the in-progress mouse-down off to Windows so it does NATIVE title-bar dragging --
        this gives Aero Snap (drag-to-edge → half-screen preview), double-click-to-maximize, and the
        proper drag cursor, none of which pywebview's JS-driven MoveWindow loop triggers.

        Why this is more involved than a single ``SendMessage``: WebView2 captured the mouse on its
        own mousedown handler, on the form's UI thread. ``ReleaseCapture`` only affects the *calling*
        thread, so calling it from pywebview's js_api worker thread is a silent no-op -- and then
        Windows refuses to enter the modal move loop because the mouse is still captured elsewhere.
        We marshal both calls onto the UI thread via ``Form.BeginInvoke`` (this is what Tauri does
        on Windows for ``start_dragging`` for the same reason)."""
        form = _get_form(self._window)
        if form is None:
            return
        try:
            import ctypes  # noqa: PLC0415

            from System import Action  # type: ignore[import-not-found]  # noqa: PLC0415 -- pythonnet

            hwnd = int(form.Handle.ToInt64())

            def _on_ui_thread() -> None:
                user32 = ctypes.windll.user32
                user32.ReleaseCapture()
                user32.SendMessageW(hwnd, _WM_NCLBUTTONDOWN, _HTCAPTION, 0)

            form.BeginInvoke(Action(_on_ui_thread))
        except Exception:
            _log.exception("start_drag dispatch failed")

    def get_window_bounds(self) -> list[int]:
        """Return the current ``[left, top, width, height]`` of the form, in screen pixels.
        Used by the JS-driven resize loop to compute the new bounds from mouse-move deltas."""
        form = _get_form(self._window)
        if form is None:
            return [0, 0, 0, 0]
        return [int(form.Left), int(form.Top), int(form.Width), int(form.Height)]

    def set_window_bounds(self, left: int, top: int, width: int, height: int) -> None:
        """Set the form's outer ``[left, top, width, height]`` (screen pixels). Marshalled to the
        form's UI thread -- ``Form.SetBounds`` from any other thread races with WinForms' layout."""
        form = _get_form(self._window)
        if form is None:
            return
        try:
            from System import Action  # noqa: PLC0415 -- pythonnet; .NET BCL, resolved at runtime

            x, y, w, h = int(left), int(top), int(width), int(height)

            def _on_ui_thread() -> None:
                form.SetBounds(x, y, w, h)

            form.BeginInvoke(Action(_on_ui_thread))
        except Exception:
            _log.exception("set_window_bounds dispatch failed")

    def pick_folder(self) -> str | None:
        """Open the OS folder picker and return the chosen absolute path (or ``None`` on cancel).
        Used by the renderer's "+ Add source folder" / Settings → FFmpeg → custom path UI so the
        user gets a real Explorer dialog instead of having to type a path by hand."""
        if self._window is None:
            return None
        try:
            import webview  # noqa: PLC0415  -- pywebview package; import lazy so the rest of the API works without it
            result = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        except Exception:
            _log.exception("pick_folder failed")
            return None
        if not result:
            return None
        first = result[0] if isinstance(result, list | tuple) else result
        return str(first) if first else None

    def pick_file(self, file_types: list[str] | None = None) -> str | None:
        """Open the OS file picker. ``file_types`` is a list of pywebview-style filter specs
        (each looks like ``"Description (*.ext;*.ext2)"``); pass an empty list / ``None`` to show
        every file. Returns the chosen absolute file path or ``None`` on cancel."""
        if self._window is None:
            return None
        try:
            import webview  # noqa: PLC0415
            kwargs: dict[str, Any] = {}
            if file_types:
                kwargs["file_types"] = tuple(file_types)
            result = self._window.create_file_dialog(webview.OPEN_DIALOG, **kwargs)
        except Exception:
            _log.exception("pick_file failed")
            return None
        if not result:
            return None
        first = result[0] if isinstance(result, list | tuple) else result
        return str(first) if first else None


def _run_pywebview_window(url: str, shell: ShellConfig, server: uvicorn.Server, application: Application) -> bool:
    """Open the UI as a frameless desktop window (pywebview / WebView2), blocking until it is closed.
    Returns ``False`` -- without opening anything -- if pywebview isn't installed or no WebView2 backend
    is available, so the caller can fall back to a chromeless ``--app`` window or a browser tab."""
    try:
        import webview  # noqa: PLC0415  -- the package is "pywebview"; the module is "webview"
    except ImportError:
        return False

    api = _WindowApi()
    # Track the window's size live: pywebview's `window.width` / `window.height` query the platform
    # for the *current* size, which returns None for an already-destroyed window -- so reading them
    # AFTER `webview.start()` returns raises ``cannot unpack non-iterable NoneType object`` and used
    # to wrongly fall back to a Chrome --app window the moment the user closed our window.
    last_size = [int(shell.window_width), int(shell.window_height)]

    try:
        window = webview.create_window(
            shell.window_title, html=_SPLASH_HTML, js_api=api,
            width=shell.window_width, height=shell.window_height, min_size=_WINDOW_MIN_SIZE,
            frameless=True, easy_drag=False, background_color="#0e1014",
        )
        if window is None:                            # shouldn't happen, but the type allows it
            return False
        api._window = window                          # the only writer of this "private by convention" attr

        def _on_resized(w: int, h: int) -> None:
            last_size[0] = int(w)
            last_size[1] = int(h)
        window.events.resized += _on_resized

        def _go_live() -> None:                       # runs on a worker thread once the GUI loop is up
            _wait_until_started(server)
            window.load_url(url)

        webview.start(func=_go_live, gui="edgechromium")   # blocks until the window is closed
    except Exception as exc:
        _log.warning("pywebview window unavailable (%s); falling back to a browser window", exc)
        return False

    # The window opened and closed cleanly. Persist its final size best-effort; even if THAT fails,
    # the window's job is done -- don't fall back to a second window in the user's face.
    if shell.remember_window_state:
        with contextlib.suppress(Exception):
            _persist_window_size(application, last_size[0], last_size[1])
    return True


def _open_app_window(url: str, shell: ShellConfig) -> bool:
    """Open the UI in a chromeless Chrome/Edge ``--app`` window, blocking until it is closed. Returns
    ``False`` (without opening anything) if no suitable browser is found."""
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
            "--disable-extensions", "--disable-component-extensions-with-background-pages",
            "--disable-sync", "--disable-component-update", "--disable-background-networking",
            "--disable-default-apps", "--no-service-autorun", "--password-store=basic",
            f"--disable-features={_QUIET_FEATURES}",
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
    lock = _acquire_single_instance_lock(application.data_dir)
    if lock is None:
        print(f"{application.config.app.name} is already running.")
        application.shutdown()
        return 1
    # Heal the library in the background before the user does anything: re-identify a library left
    # on a superseded identity format (submitted first, so any later scan queues behind it), then
    # finish metadata for clips a previous run left pending. Both are instant no-ops when there is
    # nothing to do.
    application.scans.upgrade_identity_format()
    application.scans.enrich_pending()
    api = create_app(application)
    cfg = application.config
    host = cfg.server.host
    port = cfg.server.port or _free_port()
    url = f"http://{host}:{port}/"
    server = uvicorn.Server(uvicorn.Config(api, host=host, port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    thread.start()
    try:
        if getattr(args, "no_browser", False):
            print(f"{cfg.app.name} running at {url}  (Ctrl+C to stop; --no-browser)")
            with contextlib.suppress(KeyboardInterrupt):
                thread.join()
            return 0
        if getattr(args, "browser", False) or cfg.shell.mode == "browser":
            _wait_until_started(server)
            webbrowser.open(url)
            print(f"{cfg.app.name} running at {url}  (Ctrl+C to stop)")
            with contextlib.suppress(KeyboardInterrupt):
                thread.join()
            return 0
        # [shell].mode == "pywebview" (the default): native window -> chromeless --app window -> browser tab
        print(f"{cfg.app.name} -- desktop window  ({url})")
        if _run_pywebview_window(url, cfg.shell, server, application):
            return 0
        _wait_until_started(server)
        if _open_app_window(url, cfg.shell):
            return 0
        webbrowser.open(url)
        print(f"{cfg.app.name} running at {url}  (Ctrl+C to stop)")
        with contextlib.suppress(KeyboardInterrupt):
            thread.join()
        return 0
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        application.shutdown()
        with contextlib.suppress(Exception):
            lock.close()


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
        application.scans.upgrade_identity_format()   # re-identify a legacy library before scanning
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
    run_p = sub.add_parser("run", help="run the server and open the UI (a native desktop window by default)")
    run_p.add_argument("--browser", action="store_true", help="open the default browser as a tab instead of a window")
    run_p.add_argument("--no-browser", action="store_true", help="run the server only; open no window or browser")
    add_p = sub.add_parser("add-source", help="add a library source folder")
    add_p.add_argument("path", help="path to a folder to watch for media")
    scan_p = sub.add_parser("scan", help="scan sources for media files")
    scan_p.add_argument("source_id", nargs="?", type=int, default=None, help="optional: scan just this source id")
    return parser


def _redirect_headless_output() -> None:
    """A windowed (no-console) build has no stdout/stderr -- send them to a log file instead."""
    base = os.environ.get("APPDATA") or tempfile.gettempdir()
    log_path = Path(base) / "Clippycap" / "logs" / "clippycap.log"
    with contextlib.suppress(OSError):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = log_path.open("a", encoding="utf-8", buffering=1)
        sys.stdout = handle
        sys.stderr = handle


def main(argv: list[str] | None = None) -> int:
    if sys.stdout is None or sys.stderr is None:        # PyInstaller --windowed build
        _redirect_headless_output()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    args = _build_parser().parse_args(argv)
    handlers: dict[str | None, Any] = {
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
