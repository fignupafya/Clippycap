"""On-demand ffmpeg: report status, download-and-install a static build, or point at an existing one.

ffmpeg is not bundled with the app (it's large and licence-encumbered). On startup the resolver
locates it if it's around (in ``<data_dir>/bin``, next to the exe, on ``PATH``, ...); if not, the
shell prompts the user once to download a static build. This service backs that flow:

* :meth:`status`              -- what's resolved right now, and whether to show the install prompt.
* :meth:`start_install`       -- kick off a background download into ``<data_dir>/bin`` (returns a
  job id pollable via ``GET /api/jobs/{id}``); on success the shared :class:`FfmpegToolsHolder` is
  swapped so the thumbnailer / metadata extractor / video editor use it immediately.
* :meth:`use_path`            -- validate a user-supplied path to an existing ffmpeg(.exe) / folder,
  persist it to ``local.toml``, and re-resolve.
* :meth:`use_auto`            -- forget the explicit path; go back to auto-detection.
* :meth:`dismiss_install_prompt` -- record that the user declined (so it never re-prompts).
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clippycap.app.config_service import ConfigService
from clippycap.core.errors import InvalidInputError, UnsupportedError
from clippycap.core.ports import JobQueue, ProgressReporter
from clippycap.infra.config import ConfigError, ConfigHolder
from clippycap.infra.media.ffmpeg import FfmpegToolsHolder, probe_ffmpeg_version, resolve_ffmpeg_tools
from clippycap.infra.media.ffmpeg_install import FfmpegInstallError, download_ffmpeg

_log = logging.getLogger(__name__)
_EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""
_BIN_DIRNAME = "bin"
_INSTALL_JOB_NAME = "ffmpeg-install"


@dataclass(frozen=True, slots=True)
class FfmpegStatus:
    available: bool                 # ffmpeg.exe was located and runs
    ffprobe_available: bool         # ffprobe.exe was located and runs
    ffmpeg_path: str | None         # resolved absolute path, if any
    ffprobe_path: str | None
    version: str | None             # first line of `ffmpeg -version`
    enabled: bool                   # [media.ffmpeg].enabled
    configured_path: str | None     # the explicit path set in config (None if "auto" / "@bundled")
    offer_install: bool             # the shell should prompt the user to install ffmpeg
    can_install: bool               # automatic install is possible on this platform (Windows)
    installing: bool                # an install job is running right now
    install_job_id: str | None


class FfmpegService:
    def __init__(
        self,
        *,
        tools_holder: FfmpegToolsHolder,
        config_holder: ConfigHolder,
        config_service: ConfigService,
        jobs: JobQueue,
        data_dir: Path,
        install_dir: Path,
    ) -> None:
        self._tools = tools_holder
        self._config_holder = config_holder
        self._config_service = config_service
        self._jobs = jobs
        self._data_dir = data_dir
        self._install_dir = install_dir
        self._install_job_id: str | None = None

    # ---- queries ---------------------------------------------------------

    def status(self) -> FfmpegStatus:
        tools = self._tools.current
        ffmpeg_cfg = self._config_holder.current.media.ffmpeg
        available = tools.ffmpeg_path is not None
        configured = ffmpeg_cfg.ffmpeg_path not in ("auto", "@bundled")
        installing, job_id = self._install_state()
        return FfmpegStatus(
            available=available,
            ffprobe_available=tools.ffprobe_path is not None,
            ffmpeg_path=str(tools.ffmpeg_path) if tools.ffmpeg_path is not None else None,
            ffprobe_path=str(tools.ffprobe_path) if tools.ffprobe_path is not None else None,
            version=probe_ffmpeg_version(tools.ffmpeg_path) if tools.ffmpeg_path is not None else None,
            enabled=ffmpeg_cfg.enabled,
            configured_path=ffmpeg_cfg.ffmpeg_path if configured else None,
            offer_install=(not available and ffmpeg_cfg.enabled
                           and ffmpeg_cfg.offer_install_if_missing and self._can_install()),
            can_install=self._can_install(),
            installing=installing,
            install_job_id=job_id,
        )

    # ---- actions ---------------------------------------------------------

    def start_install(self) -> str:
        """Start (or rejoin) the background download of a static ffmpeg build. Returns its job id.

        :raises UnsupportedError: automatic install is only available on Windows.
        """
        if not self._can_install():
            raise UnsupportedError(
                "Automatic ffmpeg install is only available on Windows. On Linux/macOS install "
                "ffmpeg with your package manager and reopen the app, or set its path in Settings."
            )
        installing, existing = self._install_state()
        if installing and existing is not None:
            return existing
        self._install_job_id = self._jobs.submit(_INSTALL_JOB_NAME, self._run_install)
        return self._install_job_id

    def use_path(self, raw_path: str) -> FfmpegStatus:
        """Use the ffmpeg(.exe) at ``raw_path`` -- or, if it's a directory, the ``ffmpeg``/``ffprobe``
        inside it -- after checking it runs. Persists the choice to ``local.toml`` and re-resolves.

        :raises InvalidInputError: the path is empty, or there's no working ffmpeg there.
        """
        cleaned = raw_path.strip().strip('"').strip("'")
        if not cleaned:
            raise InvalidInputError("Provide the path to ffmpeg.exe, or to the folder that contains it.")
        candidate = Path(cleaned)
        if candidate.is_dir():
            ffmpeg_path = candidate / f"ffmpeg{_EXE_SUFFIX}"
            ffprobe_path = candidate / f"ffprobe{_EXE_SUFFIX}"
        else:
            ffmpeg_path = candidate
            ffprobe_path = candidate.with_name(f"ffprobe{_EXE_SUFFIX}")
        if probe_ffmpeg_version(ffmpeg_path) is None:
            raise InvalidInputError(
                f"No working ffmpeg at {ffmpeg_path} (running '{ffmpeg_path.name} -version' failed)."
            )
        ffprobe_value = str(ffprobe_path) if probe_ffmpeg_version(ffprobe_path) is not None else "auto"
        self._persist_ffmpeg({"enabled": True, "ffmpeg_path": str(ffmpeg_path), "ffprobe_path": ffprobe_value})
        return self.status()

    def use_auto(self) -> FfmpegStatus:
        """Forget any explicit ffmpeg path -- go back to auto-detection -- and re-resolve."""
        self._persist_ffmpeg({"ffmpeg_path": "auto", "ffprobe_path": "auto"})
        return self.status()

    def dismiss_install_prompt(self) -> None:
        """Record that the user declined the install prompt, so the shell never re-shows it."""
        self._persist_ffmpeg({"offer_install_if_missing": False})

    # ---- internals -------------------------------------------------------

    @staticmethod
    def _can_install() -> bool:
        return sys.platform == "win32"

    def _install_state(self) -> tuple[bool, str | None]:
        job_id = self._install_job_id
        if job_id is None:
            return (False, None)
        handle = self._jobs.get(job_id)
        if handle is None:
            return (False, None)
        return (handle.state in ("pending", "running"), job_id)

    def _run_install(self, reporter: ProgressReporter) -> None:
        # any exception here lands in the job's error message (see ThreadJobQueue) and the UI shows it.
        reporter.update(0, None, "Downloading ffmpeg…")
        download_ffmpeg(
            self._data_dir / _BIN_DIRNAME,
            progress=lambda done, total: reporter.update(done, total or None, "Downloading ffmpeg…"),
        )
        reporter.update(0, None, "Verifying…")
        new_tools = resolve_ffmpeg_tools(self._config_holder.current, self._install_dir)
        self._tools.current = new_tools                  # live: the thumbnailer / editor / extractor use it now
        if new_tools.ffmpeg_path is None:
            raise FfmpegInstallError(
                "Downloaded ffmpeg, but it could not be run afterwards -- it may have been blocked "
                "by antivirus / SmartScreen. Try unblocking it, or install ffmpeg manually."
            )
        reporter.update(0, None, f"ffmpeg installed at {new_tools.ffmpeg_path}")
        _log.info("on-demand ffmpeg install complete: %s", new_tools.ffmpeg_path)

    def _persist_ffmpeg(self, changes: dict[str, Any]) -> None:
        current = self._config_holder.current.media.ffmpeg
        merged = {
            "enabled": current.enabled,
            "ffmpeg_path": current.ffmpeg_path,
            "ffprobe_path": current.ffprobe_path,
            "offer_install_if_missing": current.offer_install_if_missing,
            **changes,
        }
        try:
            self._config_service.update(media={"ffmpeg": merged})
        except ConfigError as exc:
            raise InvalidInputError(f"Could not apply the ffmpeg setting: {exc}") from exc
        self._tools.current = resolve_ffmpeg_tools(self._config_holder.current, self._install_dir)
