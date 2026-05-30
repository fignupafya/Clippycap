"""GitHub-release based update check + one-click install.

What this service does:

1. Periodically asks the configured GitHub repository's ``/releases`` endpoint for the published
   releases and remembers which (non-draft, non-prerelease) ones are newer than the running
   :data:`clippycap.__version__`. The result is cached for ``[updates].check_interval_hours`` so
   re-opening the app several times in a row doesn't re-hit the API.
2. Persists two small pieces of UI state in the meta store so the renderer's badge / modal stay
   sane across restarts:

   * ``updates.notified_version`` -- the latest version the renderer has already auto-opened a
     modal for. The next session knows not to auto-pop the same release again; the badge is
     still shown so the user can come back to it.
   * ``updates.skipped_version`` -- a version the user explicitly asked us to stop nagging them
     about. The badge stays hidden until a strictly newer version ships.
3. Downloads the right asset for the run mode (``Setup.exe`` for an installed copy,
   ``Portable.exe`` for a portable run) and launches the update:

   * **Installed**: runs the Inno installer with
     ``/SILENT /SUPPRESSMSGBOXES /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS`` so Inno's Restart
     Manager closes the running app, swaps files, and starts the app again -- no manual steps.
   * **Portable**: uses the Windows rename trick (a running ``.exe`` may be renamed but not
     overwritten). The running binary moves to ``<stem>.old.exe``, the freshly-downloaded one
     takes the original name, the new exe is spawned, and the old process exits. The
     ``*.old.exe`` backup is cleaned up on the next start.

Robustness notes:

* Every network call is wrapped in a broad ``except`` that turns failures into an
  :class:`UpdateStatus` with ``error`` set -- the renderer just hides the badge in that case and
  retries on the next interval.
* The portable swap falls back to "downloaded but not installed -- please replace by hand" via
  :class:`~clippycap.core.errors.UnsupportedError` when the running folder isn't writable
  (read-only mounts, OneDrive sync locks, antivirus, ...). The new file stays in the data dir
  with an open Explorer pointing at it.
* Install actions are serialized with a lock so a double-click doesn't kick off two jobs.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clippycap import __version__ as APP_VERSION
from clippycap.core.errors import UnsupportedError
from clippycap.core.ports import Database
from clippycap.infra.config import ConfigHolder

_log = logging.getLogger(__name__)

_API_TIMEOUT_S = 10
_DOWNLOAD_TIMEOUT_S = 600
_RELEASES_PAGE_SIZE = 30                # plenty for any realistic catch-up window
_USER_AGENT = f"Clippycap/{APP_VERSION} (update-check)"
_NOTIFIED_KEY = "updates.notified_version"
_SKIPPED_KEY = "updates.skipped_version"
_PORTABLE_RESTART_DELAY_S = 1.5         # let the spawned child fully start before we exit


# --------------------------------------------------------------------------- data shapes


@dataclass(slots=True)
class ReleaseAsset:
    name: str
    url: str
    size: int


@dataclass(slots=True)
class ReleaseNote:
    """One release shown in the modal's cumulative changelog (newest-first)."""

    version: str
    name: str
    published_at: str            # raw ISO-8601 from GitHub; the renderer slices it for display
    body: str


@dataclass(slots=True)
class UpdateStatus:
    current_version: str
    mode: str                                          # "installed" | "portable" | "dev"
    enabled: bool                                      # ``[updates].enabled``
    latest_version: str | None = None
    release_url: str | None = None
    release_notes_chain: list[ReleaseNote] = field(default_factory=list)
    setup_asset: ReleaseAsset | None = None
    portable_asset: ReleaseAsset | None = None
    notified_version: str | None = None
    skipped_version: str | None = None
    last_checked_at: float | None = None
    error: str | None = None

    @property
    def has_update(self) -> bool:
        """True iff a newer release exists AND the user hasn't asked us to skip it."""
        if self.latest_version is None or not _is_newer(self.latest_version, self.current_version):
            return False
        # The skipped-version gate: if the user said "skip 0.4.0", show nothing again until a
        # strictly newer one ships.
        return not self.skipped_version or _is_newer(self.latest_version, self.skipped_version)

    @property
    def is_new_notification(self) -> bool:
        """True iff the renderer should auto-open the modal once -- whenever the latest version
        differs from the one we've previously marked as notified."""
        return self.has_update and self.notified_version != self.latest_version


# --------------------------------------------------------------------------- version + mode


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse a (possibly v-prefixed) dotted version into a tuple of ints. Raises ``ValueError``
    on anything that can't be cleanly interpreted -- callers fall back to "no update" then."""
    stripped = v.lstrip("vV").strip()
    if not stripped:
        raise ValueError(f"empty version: {v!r}")
    return tuple(int(p) for p in stripped.split("."))


def _is_newer(a: str, b: str) -> bool:
    """True iff ``a`` parses to a strictly newer version than ``b``. Unparseable versions are
    treated as "not newer" so a malformed tag never falsely advertises an update."""
    try:
        return _parse_version(a) > _parse_version(b)
    except ValueError:
        return False


def detect_mode() -> str:
    """How is the app running? An installed Inno build keeps ``unins000.exe`` next to the .exe;
    a portable build is the single PyInstaller .exe alone; "dev" is anything not frozen."""
    if not getattr(sys, "frozen", False):
        return "dev"
    exe = Path(sys.executable)
    if (exe.parent / "unins000.exe").exists():
        return "installed"
    return "portable"


def _backup_path(exe: Path) -> Path:
    """Where the old portable binary lands during the rename-trick swap. The trailing ``.exe``
    is kept so the file is still executable if the user wants to roll back by hand."""
    return exe.parent / f"{exe.stem}.old.exe"


def cleanup_stale_backups() -> None:
    """Best-effort removal of ``<stem>.old.exe`` left behind by a prior portable swap, called
    once on app startup. Failures are logged and ignored -- the file is harmless if it stays."""
    if detect_mode() != "portable":
        return
    backup = _backup_path(Path(sys.executable))
    if not backup.exists() or backup == Path(sys.executable):
        return
    try:
        backup.unlink()
    except OSError as exc:
        _log.info("could not remove stale portable backup %s: %s", backup, exc)


# --------------------------------------------------------------------------- HTTP helpers


def _fetch_releases(repository: str) -> list[dict[str, Any]]:
    """One GitHub call that returns up to ``_RELEASES_PAGE_SIZE`` releases for the repo,
    newest-first. Anonymous requests get 60/hour; we use a fraction of one."""
    url = (
        f"https://api.github.com/repos/{repository}/releases"
        f"?per_page={_RELEASES_PAGE_SIZE}"
    )
    req = urllib.request.Request(
        url,
        headers={"User-Agent": _USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=_API_TIMEOUT_S) as resp:
        raw = resp.read().decode("utf-8")
    data = json.loads(raw)
    if not isinstance(data, list):
        return []
    return [r for r in data if isinstance(r, dict)]


def _download_to(url: str, target: Path, on_progress: Callable[[int], None]) -> None:
    """Stream an HTTPS URL to disk in 64 KiB chunks. A partial file is removed if the download
    fails so the next attempt starts fresh."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    downloaded = 0
    try:
        with urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT_S) as resp, target.open("wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                on_progress(downloaded)
    except Exception:
        if target.exists():
            with contextlib.suppress(OSError):
                target.unlink()
        raise


def _spawn_detached(args: list[str]) -> None:
    """Spawn an external process that survives our exit -- detached, own group, no inherited
    pipes -- so updaters and the post-update portable binary keep running after we go."""
    subprocess.Popen(
        args,
        creationflags=(
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        ),
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _spawn_installed_setup(setup_exe: Path) -> None:
    """Run Inno's installer silently with CloseApplications + RestartApplications so it closes
    the running app via the Restart Manager, swaps files in ``{app}``, and re-launches us."""
    _spawn_detached([
        str(setup_exe),
        "/SILENT", "/SUPPRESSMSGBOXES", "/CLOSEAPPLICATIONS",
        "/RESTARTAPPLICATIONS", "/NORESTART",
    ])


def _swap_portable(new_exe: Path) -> None:
    """Replace the running portable binary using the Windows rename trick (a running .exe can
    be renamed but not overwritten). Spawns the new exe and exits so the swap completes."""
    current = Path(sys.executable)
    backup = _backup_path(current)
    if backup.exists():
        try:
            backup.unlink()
        except OSError as exc:
            raise UnsupportedError(
                "could not remove the previous portable backup -- close any tool that may have "
                "a handle on it (e.g. an explorer preview) and try again"
            ) from exc
    try:
        os.replace(current, backup)
        os.replace(new_exe, current)
    except OSError as exc:
        # Read-only / OneDrive sync / antivirus / write-protected mount. Tell the user the new
        # file is still on disk so they can do the swap themselves.
        raise UnsupportedError(
            f"could not swap the portable binary in place ({exc}). The new version was saved "
            f"to {new_exe} -- close Clippycap and replace your portable .exe with that file."
        ) from exc
    _spawn_detached([str(current)])
    # Let the spawned child fully start before our process exits -- without the delay the new
    # exe sometimes hasn't taken the single-instance lock yet and refuses to start.
    threading.Timer(_PORTABLE_RESTART_DELAY_S, lambda: os._exit(0)).start()


# --------------------------------------------------------------------------- the service


class UpdateService:
    """Coordinates the GitHub release check + the download/install. Thread-safe."""

    def __init__(self, config_holder: ConfigHolder, database: Database, data_dir: Path) -> None:
        self._config_holder = config_holder
        self._db = database
        self._data_dir = data_dir
        self._lock = threading.Lock()                  # protects _cached
        self._cached: UpdateStatus | None = None
        self._install_lock = threading.Lock()          # protects _installing flag + start
        self._installing = False
        self._install_progress: tuple[int, int, str] = (0, 0, "idle")

    # ---- read paths ------------------------------------------------------

    def get_status(self, *, force_check: bool = False) -> UpdateStatus:
        """The current view of the world. Falls back to a disabled status if the user turned
        update checks off. Re-hits GitHub if the cached check is older than the configured
        interval, or whenever ``force_check`` is set."""
        cfg = self._config_holder.current.updates
        if not cfg.enabled:
            return self._disabled_status()
        with self._lock:
            cached = self._cached
        interval_s = cfg.check_interval_hours * 3600
        now = time.time()
        if (
            not force_check
            and cached is not None
            and cached.last_checked_at is not None
            and now - cached.last_checked_at < interval_s
        ):
            return self._apply_state(cached)
        return self._apply_state(self._check_now(cfg.repository))

    @property
    def install_progress(self) -> dict[str, Any]:
        """Live progress for the renderer's progress bar."""
        downloaded, total, message = self._install_progress
        return {
            "downloaded": downloaded, "total": total, "message": message,
            "active": self._installing,
        }

    # ---- write paths -----------------------------------------------------

    def mark_notified(self) -> UpdateStatus:
        """Record that we've shown the modal for the current latest version, so subsequent
        sessions don't auto-pop the same release again -- only the badge does."""
        status = self.get_status()
        if status.latest_version is not None:
            with self._db.transaction() as uow:
                uow.meta.set(_NOTIFIED_KEY, status.latest_version)
        return self.get_status()

    def mark_skipped(self) -> UpdateStatus:
        """Hide the badge for the current latest version (and any older). We re-surface only
        when a strictly newer release ships."""
        status = self.get_status()
        if status.latest_version is not None:
            with self._db.transaction() as uow:
                uow.meta.set(_SKIPPED_KEY, status.latest_version)
        return self.get_status()

    def start_install(self) -> dict[str, Any]:
        """Kick off the download + install asynchronously. Returns immediately; the renderer
        polls ``install_progress`` for the live bar. Raises ``UnsupportedError`` if no update
        is available, no asset matches the current run mode, or we're in dev mode."""
        status = self.get_status()
        if not status.has_update:
            raise UnsupportedError("no update is available")
        if status.mode == "installed":
            asset, kind = status.setup_asset, "Setup"
        elif status.mode == "portable":
            asset, kind = status.portable_asset, "Portable"
        else:
            raise UnsupportedError(
                "automatic updates aren't supported in dev mode -- run from source instead"
            )
        if asset is None or not asset.url:
            raise UnsupportedError(f"the latest release does not include a {kind}.exe asset")
        with self._install_lock:
            if self._installing:
                return {"already_running": True, "version": status.latest_version}
            self._installing = True
        thread = threading.Thread(
            target=self._run_install, args=(asset, status.mode), name="clippycap-update",
            daemon=True,
        )
        thread.start()
        return {"started": True, "version": status.latest_version, "mode": status.mode}

    # ---- internals -------------------------------------------------------

    def _disabled_status(self) -> UpdateStatus:
        return UpdateStatus(current_version=APP_VERSION, mode=detect_mode(), enabled=False)

    def _apply_state(self, base: UpdateStatus) -> UpdateStatus:
        """Overlay the persisted notification + skip state on a freshly-built or cached status."""
        notified, skipped = self._read_state()
        return UpdateStatus(
            current_version=base.current_version,
            mode=base.mode,
            enabled=base.enabled,
            latest_version=base.latest_version,
            release_url=base.release_url,
            release_notes_chain=base.release_notes_chain,
            setup_asset=base.setup_asset,
            portable_asset=base.portable_asset,
            notified_version=notified,
            skipped_version=skipped,
            last_checked_at=base.last_checked_at,
            error=base.error,
        )

    def _read_state(self) -> tuple[str | None, str | None]:
        with self._db.transaction() as uow:
            return uow.meta.get(_NOTIFIED_KEY), uow.meta.get(_SKIPPED_KEY)

    def _check_now(self, repository: str) -> UpdateStatus:
        status = UpdateStatus(
            current_version=APP_VERSION, mode=detect_mode(),
            enabled=True, last_checked_at=time.time(),
        )
        try:
            releases = _fetch_releases(repository)
        except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError) as exc:
            status.error = f"could not check for updates: {exc}"
            _log.info(status.error)
            with self._lock:
                self._cached = status
            return status

        eligible = [
            r for r in releases
            if not bool(r.get("draft")) and not bool(r.get("prerelease"))
        ]
        if not eligible:
            with self._lock:
                self._cached = status
            return status

        latest = eligible[0]
        latest_tag = str(latest.get("tag_name") or "").lstrip("vV").strip()
        if not latest_tag:
            with self._lock:
                self._cached = status
            return status
        status.latest_version = latest_tag
        release_url = latest.get("html_url")
        status.release_url = str(release_url) if isinstance(release_url, str) else None

        # Cumulative changelog: every release strictly newer than the running version, sorted
        # newest-first so the user sees the most recent changes at the top of the modal.
        chain: list[ReleaseNote] = []
        for r in eligible:
            tag = str(r.get("tag_name") or "").lstrip("vV").strip()
            if not tag or not _is_newer(tag, APP_VERSION):
                continue
            chain.append(ReleaseNote(
                version=tag,
                name=str(r.get("name") or ""),
                published_at=str(r.get("published_at") or ""),
                body=str(r.get("body") or "").strip(),
            ))
        try:
            chain.sort(key=lambda n: _parse_version(n.version), reverse=True)
        except ValueError:
            # If even one tag in the chain is unparseable, keep the API order rather than crash.
            _log.info("could not sort release-notes chain by version; keeping API order")
        status.release_notes_chain = chain

        # Asset URLs come from the latest release only -- that's what we install.
        for asset in latest.get("assets") or []:
            if not isinstance(asset, dict):
                continue
            name = str(asset.get("name") or "")
            url = str(asset.get("browser_download_url") or "")
            size = int(asset.get("size") or 0)
            if not url:
                continue
            ra = ReleaseAsset(name=name, url=url, size=size)
            if name.endswith("Setup.exe"):
                status.setup_asset = ra
            elif name.endswith("Portable.exe"):
                status.portable_asset = ra

        with self._lock:
            self._cached = status
        return status

    def _run_install(self, asset: ReleaseAsset, mode: str) -> None:
        target = self._data_dir / "updates" / asset.name
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            self._install_progress = (0, asset.size, f"downloading {asset.name}")
            _download_to(asset.url, target, lambda d: self._set_progress(d, asset.size))
            self._install_progress = (asset.size, asset.size, "launching installer")
            if mode == "installed":
                _spawn_installed_setup(target)
                # Inno's CloseApplications takes over -- we wait for it to send WM_CLOSE.
            else:
                _swap_portable(target)
                # _swap_portable spawns the new exe and arms an os._exit timer; nothing left here.
        except UnsupportedError as exc:
            _log.warning("update install: %s", exc)
            self._install_progress = (0, 0, f"failed: {exc}")
        except Exception as exc:
            _log.exception("update install failed unexpectedly")
            self._install_progress = (0, 0, f"failed: {exc}")
        finally:
            with self._install_lock:
                self._installing = False

    def _set_progress(self, downloaded: int, total: int) -> None:
        self._install_progress = (downloaded, total, "downloading")
