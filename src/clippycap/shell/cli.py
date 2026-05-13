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
# because Windows' modal SIZE loop silently bails out on a window without ``WS_THICKFRAME``.
_WM_NCLBUTTONDOWN = 0x00A1
_HTCAPTION = 2

# Holds the ctypes-allocated SUBCLASSPROC trampolines for any windows we've subclassed so the GC
# doesn't free them while Windows is still calling into them. (We only ever open one main window,
# but a list keeps it generic.)
_native_keepalive: list[Any] = []


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


def _enable_aero_snap(window: Any) -> None:                      # noqa: PLR0915 -- one Win32 setup, all of a piece
    """Re-enable Windows' native Aero Snap (drag-to-edge → half-screen / quarter-screen snap previews,
    Win+arrow shortcuts, Snap Assist) on a frameless pywebview window.

    pywebview's ``frameless=True`` sets ``FormBorderStyle.None`` on the WinForms form, which strips
    ``WS_THICKFRAME`` -- and that flag is what Windows' modal move loop (which our :meth:`_WindowApi.start_drag`
    enters via ``WM_NCLBUTTONDOWN(HTCAPTION)``) looks for when deciding whether a window is snappable.
    No flag → drag works, but no snap preview, no Win+arrow snap, no Snap Assist.

    Adding ``WS_THICKFRAME`` back makes the OS draw a visible ~8px gray sizing border, which we hide
    by subclassing the form's WndProc to return 0 from ``WM_NCCALCSIZE`` -- this tells Windows "the
    entire window rect is client area, no non-client frame to draw". This is exactly the Aero
    Borderless pattern Visual Studio / VS Code / Windows Terminal use for their custom title bars.

    We attach via comctl32's ``SetWindowSubclass`` chain rather than overwriting ``GWL_WNDPROC``, so
    WinForms' own WndProc is left intact and every message we don't care about flows through to it
    unmodified."""
    if sys.platform != "win32":
        return
    form = _get_form(window)
    if form is None:
        return
    try:
        import ctypes  # noqa: PLC0415
        from ctypes import wintypes  # noqa: PLC0415

        hwnd = wintypes.HWND(int(form.Handle.ToInt64()))
        user32 = ctypes.windll.user32
        comctl32 = ctypes.windll.comctl32

        GWL_STYLE = -16
        WS_THICKFRAME = 0x00040000        # OS draws a sizing border + considers us snap-eligible
        WS_MAXIMIZEBOX = 0x00010000       # also required for Aero Snap to engage on drag

        user32.GetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int)
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.SetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t)
        user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t

        cur_style = user32.GetWindowLongPtrW(hwnd, GWL_STYLE)
        user32.SetWindowLongPtrW(hwnd, GWL_STYLE, cur_style | WS_THICKFRAME | WS_MAXIMIZEBOX)

        # NCCALCSIZE_PARAMS.rgrc[0] -- the proposed window rect; we shrink it on the maximized state
        # by the would-be frame thickness so a maximized borderless window doesn't bleed off-screen.
        # And we override WM_GETMINMAXINFO to clamp the max size + position to the *work area* of
        # the nearest monitor (not the full screen), so drag-to-top snap and Win+Up don't cover the
        # taskbar -- WinForms' default for a frameless form is screen-bounds, which would.
        WM_NCCALCSIZE = 0x0083
        WM_GETMINMAXINFO = 0x0024
        SM_CXSIZEFRAME, SM_CYSIZEFRAME, SM_CXPADDEDBORDER = 32, 33, 92
        MONITOR_DEFAULTTONEAREST = 0x00000002

        class _POINT(ctypes.Structure):
            _fields_ = (("x", ctypes.c_long), ("y", ctypes.c_long))

        class _RECT(ctypes.Structure):
            _fields_ = (
                ("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long),
            )

        class _MINMAXINFO(ctypes.Structure):
            _fields_ = (
                ("ptReserved", _POINT), ("ptMaxSize", _POINT), ("ptMaxPosition", _POINT),
                ("ptMinTrackSize", _POINT), ("ptMaxTrackSize", _POINT),
            )

        class _MONITORINFO(ctypes.Structure):
            _fields_ = (
                ("cbSize", ctypes.c_ulong), ("rcMonitor", _RECT), ("rcWork", _RECT),
                ("dwFlags", ctypes.c_ulong),
            )

        SUBCLASSPROC = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, wintypes.HWND, ctypes.c_uint,
            ctypes.c_size_t, ctypes.c_ssize_t, ctypes.c_size_t, ctypes.c_size_t,
        )
        comctl32.DefSubclassProc.argtypes = (
            wintypes.HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t,
        )
        comctl32.DefSubclassProc.restype = ctypes.c_ssize_t
        comctl32.SetWindowSubclass.argtypes = (
            wintypes.HWND, SUBCLASSPROC, ctypes.c_size_t, ctypes.c_size_t,
        )
        comctl32.SetWindowSubclass.restype = wintypes.BOOL

        def _proc(h: int, msg: int, wp: int, lp: int, sub_id: int, ref_data: int) -> int:
            if msg == WM_NCCALCSIZE and wp:
                if user32.IsZoomed(h):
                    rect = ctypes.cast(lp, ctypes.POINTER(_RECT))[0]
                    fx = user32.GetSystemMetrics(SM_CXSIZEFRAME) + user32.GetSystemMetrics(SM_CXPADDEDBORDER)
                    fy = user32.GetSystemMetrics(SM_CYSIZEFRAME) + user32.GetSystemMetrics(SM_CXPADDEDBORDER)
                    rect.left += fx
                    rect.top += fy
                    rect.right -= fx
                    rect.bottom -= fy
                return 0                  # whole window is client area -> NO visible frame
            if msg == WM_GETMINMAXINFO:
                monitor = user32.MonitorFromWindow(h, MONITOR_DEFAULTTONEAREST)
                mi = _MONITORINFO()
                mi.cbSize = ctypes.sizeof(_MONITORINFO)
                user32.GetMonitorInfoW(monitor, ctypes.byref(mi))
                mmi = ctypes.cast(lp, ctypes.POINTER(_MINMAXINFO))[0]
                mmi.ptMaxPosition.x = mi.rcWork.left - mi.rcMonitor.left
                mmi.ptMaxPosition.y = mi.rcWork.top - mi.rcMonitor.top
                mmi.ptMaxSize.x = mi.rcWork.right - mi.rcWork.left
                mmi.ptMaxSize.y = mi.rcWork.bottom - mi.rcWork.top
                return 0
            return int(comctl32.DefSubclassProc(h, msg, wp, lp))

        sub_proc = SUBCLASSPROC(_proc)
        comctl32.SetWindowSubclass(hwnd, sub_proc, 0xC11005A, 0)
        _native_keepalive.append(sub_proc)            # keep the trampoline alive for the window's lifetime

        # Trigger a fresh NCCALCSIZE -> the frame layout takes effect right now (no flicker on first drag).
        SWP_FLAGS = 0x0020 | 0x0001 | 0x0002 | 0x0004 | 0x0010  # FRAMECHANGED|NOSIZE|NOMOVE|NOZORDER|NOACTIVATE
        user32.SetWindowPos.argtypes = (
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_uint,
        )
        user32.SetWindowPos(hwnd, wintypes.HWND(0), 0, 0, 0, 0, SWP_FLAGS)
    except Exception:
        _log.exception("Aero Snap setup failed")


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
            from System import Action  # type: ignore[import-not-found]  # noqa: PLC0415 -- pythonnet

            x, y, w, h = int(left), int(top), int(width), int(height)

            def _on_ui_thread() -> None:
                form.SetBounds(x, y, w, h)

            form.BeginInvoke(Action(_on_ui_thread))
        except Exception:
            _log.exception("set_window_bounds dispatch failed")


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

        # Once the form exists, give Windows back the WS_THICKFRAME flag (hidden via NCCALCSIZE) so
        # native Aero Snap kicks in on drag-to-edge / Win+arrow / Snap Assist -- pywebview's
        # frameless mode otherwise strips that flag and Windows refuses to consider us snappable.
        window.events.shown += lambda: _enable_aero_snap(window)

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
