"""Video editing (cutting / re-assembling) with ffmpeg.

:class:`FfmpegVideoEditor` keeps or removes a time range of a file. By default it re-encodes for
frame-accurate cuts (so timestamped notes stay aligned); with ``[editing].reencode = false`` it
stream-copies -- instant and lossless, but the cut START snaps to the nearest keyframe before it.

Every cut method returns the :class:`~clippycap.core.ports.KeptSegment` tuple describing the
result's timeline, or ``None`` on failure. The segment boundaries are **measured** from the files
actually produced -- the cut start with a one-frame ``showinfo`` probe (``-copyts`` keeps that pts
on the original timeline), each segment's length with ``ffprobe`` -- so the application layer can
remap notes and references onto the edited clip frame-exactly instead of estimating.

It reads the ffmpeg path from a shared :class:`FfmpegToolsHolder` and the ``[editing]`` settings
from a :class:`ConfigHolder`, so both an ffmpeg install and a settings change take effect on the
very next call -- no restart. ``available`` is ``False`` while no ffmpeg binary is located.
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from pathlib import Path

from clippycap.core.ports import KeptSegment
from clippycap.infra.config import ConfigHolder
from clippycap.infra.media.ffmpeg import NO_WINDOW, FfmpegToolsHolder

_log = logging.getLogger(__name__)
_EDIT_TIMEOUT = 600     # seconds -- re-encoding a long clip can take a while
_PROBE_TIMEOUT = 60     # seconds -- the showinfo / ffprobe measurement probes
_TRIM_START_BELOW = 0.05  # a "remove" with a head shorter than this is treated as "trim the start"


class FfmpegVideoEditor:
    def __init__(self, *, tools: FfmpegToolsHolder, config_holder: ConfigHolder) -> None:
        self._tools = tools
        self._config_holder = config_holder        # read [editing] live, so PUT /api/config takes effect at once

    @property
    def available(self) -> bool:
        return self._tools.current.ffmpeg_path is not None

    _MP4_LIKE = (".mp4", ".m4v", ".mov", ".m4a")

    def _codec_args(self) -> list[str]:
        e = self._config_holder.current.editing
        if e.reencode:
            # passthrough keeps each frame's source timestamp; ``-bf 0`` disables B-frames so the
            # encoder has no decode-vs-display reorder delay; the ``setts`` bitstream filters then
            # rebase the first kept frame to PTS 0 (no leading mp4 edit-list empty-edit). Together
            # these mean: output's frame 0 IS the first kept source frame, on the byte and the
            # millisecond -- so the timeline a remapped timestamped note seeks into is the same
            # one the trim's measurement returned. Without them, libx264 + ``-avoid_negative_ts``
            # bake in a ~2-frame elst empty edit that shifts notes a couple frames earlier.
            return ["-c:v", "libx264", "-crf", str(e.reencode_crf), "-preset", e.reencode_preset,
                    "-fps_mode:v", "passthrough", "-bf", "0",
                    "-bsf:v", "setts=ts=PTS-STARTPTS:dts=DTS-STARTDTS",
                    "-c:a", "aac",
                    "-bsf:a", "setts=ts=PTS-STARTPTS:dts=DTS-STARTDTS"]
        return ["-c", "copy"]

    def _seek_in(self, seconds: float) -> list[str]:
        # On a stream copy, "inaccurate" seeking snaps to a keyframe instead of leaving an mp4 edit
        # list behind -- browsers (unlike VLC/mpv) won't play a clip that has one.
        return ["-ss", f"{seconds:.3f}",
                *([] if self._config_holder.current.editing.reencode else ["-noaccurate_seek"])]

    def _web_flags(self, out_path: Path) -> list[str]:
        flags = ["-avoid_negative_ts", "make_zero"]                 # output timestamps start at 0
        if out_path.suffix.lower() in self._MP4_LIKE:
            flags += ["-movflags", "+faststart"]                    # moov atom up front -> the browser plays it at once
        return flags

    def _run(self, args: list[str]) -> bool:
        ffmpeg = self._tools.current.ffmpeg_path
        if ffmpeg is None:
            return False
        try:
            done = subprocess.run(
                [str(ffmpeg), "-y", "-loglevel", "error", *args],
                capture_output=True, timeout=_EDIT_TIMEOUT, check=False, creationflags=NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            _log.warning("ffmpeg edit failed to run: %s", exc)
            return False
        if done.returncode != 0:
            _log.warning("ffmpeg edit exited %d: %s", done.returncode,
                         done.stderr.decode("utf-8", "replace")[-400:])
            return False
        return True

    # ------------------------------------------------------------------ measuring the result

    def _duration_ms(self, path: Path) -> int | None:
        """The exact duration of a produced file in ms (ffprobe), or ``None`` if it can't be read."""
        ffprobe = self._tools.current.ffprobe_path
        if ffprobe is None:
            return None
        try:
            done = subprocess.run(
                [str(ffprobe), "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", str(path)],
                capture_output=True, text=True, timeout=_PROBE_TIMEOUT, check=False,
                creationflags=NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        try:
            return round(float((done.stdout or "").strip()) * 1000.0)
        except ValueError:
            return None

    def _cut_start_ms(self, source: Path, requested_ms: int) -> int:
        """The *source* time the output of a seek to ``requested_ms`` actually begins at -- the first
        frame >= requested_ms when re-encoding, the keyframe <= it on a stream copy. ``showinfo``
        prints that frame's pts; ``-copyts`` keeps the pts on the original timeline (without it
        ffmpeg rebases it to ~0). Falls back to ``requested_ms`` if it cannot be determined."""
        ffmpeg = self._tools.current.ffmpeg_path
        if ffmpeg is None:
            return requested_ms
        args = [*self._seek_in(max(0.0, requested_ms / 1000.0)), "-copyts",
                "-i", str(source), "-vf", "showinfo", "-frames:v", "1", "-f", "null", "-"]
        try:
            done = subprocess.run([str(ffmpeg), "-hide_banner", "-loglevel", "info", *args],
                                  capture_output=True, timeout=_PROBE_TIMEOUT, check=False,
                                  creationflags=NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            return requested_ms
        match = re.search(r"pts_time:([\d.]+)", done.stderr.decode("utf-8", "replace"))
        if match is None:
            return requested_ms
        try:
            return max(0, round(float(match.group(1)) * 1000.0))
        except ValueError:
            return requested_ms

    def _single_segment(
        self, source: Path, out_path: Path, requested_start_ms: int
    ) -> tuple[KeptSegment, ...] | None:
        """The one-segment timeline of a keep/trim: source ``[F, F+len]`` -> output ``[0, len]``."""
        out_ms = self._duration_ms(out_path)
        if out_ms is None:
            return None
        src_start = self._cut_start_ms(source, requested_start_ms)
        return (KeptSegment(src_start, src_start + out_ms, 0),)

    # ------------------------------------------------------------------ public cut operations

    def keep_range(
        self, source: Path, out_path: Path, *, start_ms: int, end_ms: int
    ) -> tuple[KeptSegment, ...] | None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.unlink(missing_ok=True)
        duration_s = (end_ms - start_ms) / 1000.0
        if duration_s <= 0:
            return None
        ok = self._run([*self._seek_in(max(0.0, start_ms / 1000.0)), "-i", str(source),
                        "-t", f"{duration_s:.3f}", *self._codec_args(), *self._web_flags(out_path),
                        str(out_path)])
        if not (ok and out_path.is_file() and out_path.stat().st_size > 0):
            return None
        return self._single_segment(source, out_path, start_ms)

    def remove_range(
        self, source: Path, out_path: Path, *, start_ms: int, end_ms: int
    ) -> tuple[KeptSegment, ...] | None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.unlink(missing_ok=True)
        head_end = max(0.0, start_ms / 1000.0)
        tail_start = max(head_end, end_ms / 1000.0)
        if head_end <= _TRIM_START_BELOW:        # "remove from the very start" is just "trim the start"
            ok = self._run([*self._seek_in(tail_start), "-i", str(source), *self._codec_args(),
                            *self._web_flags(out_path), str(out_path)])
            if not (ok and out_path.is_file() and out_path.stat().st_size > 0):
                return None
            return self._single_segment(source, out_path, end_ms)
        return self._cut_middle_out(source, out_path, head_end=head_end, tail_start=tail_start, end_ms=end_ms)

    def _cut_middle_out(
        self, source: Path, out_path: Path, *, head_end: float, tail_start: float, end_ms: int
    ) -> tuple[KeptSegment, ...] | None:
        """Build a "remove" result by re-encoding the head ``[0, head_end]`` and the tail
        ``[tail_start, end]`` separately and concatenating them. Concat appends the tail right after
        the head, so the head's *measured* length is the exact output boundary."""
        ext = source.suffix or ".mp4"
        with tempfile.TemporaryDirectory(prefix="clippycap-edit-") as tmp:
            tmp_dir = Path(tmp)
            head, tail = tmp_dir / f"head{ext}", tmp_dir / f"tail{ext}"
            if not self._run(["-i", str(source), "-t", f"{head_end:.3f}", *self._codec_args(), str(head)]):
                return None
            head_ms = self._duration_ms(head)
            if head_ms is None:
                return None
            tail_ok = self._run([*self._seek_in(tail_start), "-i", str(source), *self._codec_args(), str(tail)])
            if tail_ok and tail.is_file() and tail.stat().st_size > 0:
                tail_ms = self._duration_ms(tail)
                listfile = tmp_dir / "concat.txt"
                listfile.write_text(f"file '{head.as_posix()}'\nfile '{tail.as_posix()}'\n", encoding="utf-8")
                ok = self._run(["-f", "concat", "-safe", "0", "-i", str(listfile), "-c", "copy",
                                *self._web_flags(out_path), str(out_path)])
                if tail_ms is None or not (ok and out_path.is_file() and out_path.stat().st_size > 0):
                    return None
                tail_src = self._cut_start_ms(source, end_ms)
                return (KeptSegment(0, head_ms, 0),
                        KeptSegment(tail_src, tail_src + tail_ms, head_ms))
            # nothing after the cut -> the result is just the head
            ok = self._run(["-i", str(head), "-c", "copy", *self._web_flags(out_path), str(out_path)])
            if not (ok and out_path.is_file() and out_path.stat().st_size > 0):
                return None
            return (KeptSegment(0, head_ms, 0),)
