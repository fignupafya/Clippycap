"""Video editing (cutting / concatenating) with ffmpeg.

:class:`FfmpegVideoEditor` keeps or removes a time range of a file. By default it stream-copies
(``-c copy``) -- instant and lossless, but the cut snaps to a keyframe; with ``reencode=True`` it
re-encodes for frame-accurate cuts (slower, a small quality cost). :class:`UnavailableVideoEditor`
(``available is False``) is used when no ffmpeg binary is configured; its methods just return
``False`` -- the application layer must check ``available`` before offering the operation.
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from pathlib import Path

_log = logging.getLogger(__name__)
_EDIT_TIMEOUT = 600  # seconds -- re-encoding a long clip can take a while
_TRIM_START_BELOW = 0.05  # "remove" with a head shorter than this is treated as "trim the start"


class FfmpegVideoEditor:
    available = True

    def __init__(self, ffmpeg_path: Path, *, reencode: bool, crf: int, preset: str) -> None:
        self._ffmpeg = ffmpeg_path
        self._reencode = reencode
        self._crf = crf
        self._preset = preset

    _MP4_LIKE = (".mp4", ".m4v", ".mov", ".m4a")

    def _codec_args(self) -> list[str]:
        if self._reencode:
            return ["-c:v", "libx264", "-crf", str(self._crf), "-preset", self._preset, "-c:a", "aac"]
        return ["-c", "copy"]

    def _seek_in(self, seconds: float) -> list[str]:
        # On a stream copy, "inaccurate" seeking snaps to a keyframe instead of leaving an mp4 edit
        # list behind -- browsers (unlike VLC/mpv) won't play a clip that has one.
        return ["-ss", f"{seconds:.3f}", *([] if self._reencode else ["-noaccurate_seek"])]

    def _web_flags(self, out_path: Path) -> list[str]:
        flags = ["-avoid_negative_ts", "make_zero"]                 # output timestamps start at 0
        if out_path.suffix.lower() in self._MP4_LIKE:
            flags += ["-movflags", "+faststart"]                    # moov atom up front -> the browser plays it at once
        return flags

    def _run(self, args: list[str]) -> bool:
        try:
            done = subprocess.run(
                [str(self._ffmpeg), "-y", "-loglevel", "error", *args],
                capture_output=True, timeout=_EDIT_TIMEOUT, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            _log.warning("ffmpeg edit failed to run: %s", exc)
            return False
        if done.returncode != 0:
            _log.warning("ffmpeg edit exited %d: %s", done.returncode,
                         done.stderr.decode("utf-8", "replace")[-400:])
            return False
        return True

    def keep_range(self, source: Path, out_path: Path, *, start_ms: int, end_ms: int) -> bool:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.unlink(missing_ok=True)
        ss = max(0.0, start_ms / 1000.0)
        dur = (end_ms - start_ms) / 1000.0
        if dur <= 0:
            return False
        ok = self._run([*self._seek_in(ss), "-i", str(source), "-t", f"{dur:.3f}",
                        *self._codec_args(), *self._web_flags(out_path), str(out_path)])
        return ok and out_path.is_file() and out_path.stat().st_size > 0

    def remove_range(self, source: Path, out_path: Path, *, start_ms: int, end_ms: int) -> bool:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.unlink(missing_ok=True)
        head_end = max(0.0, start_ms / 1000.0)
        tail_start = max(head_end, end_ms / 1000.0)
        if head_end <= _TRIM_START_BELOW:    # "remove from the very start" is just "trim the start"
            ok = self._run([*self._seek_in(tail_start), "-i", str(source), *self._codec_args(),
                            *self._web_flags(out_path), str(out_path)])
            return ok and out_path.is_file() and out_path.stat().st_size > 0
        ext = source.suffix or ".mp4"
        with tempfile.TemporaryDirectory(prefix="clippycap-edit-") as tmp:
            tmp_dir = Path(tmp)
            head, tail = tmp_dir / f"head{ext}", tmp_dir / f"tail{ext}"
            if not self._run(["-i", str(source), "-t", f"{head_end:.3f}", *self._codec_args(), str(head)]):
                return False
            tail_ok = self._run([*self._seek_in(tail_start), "-i", str(source), *self._codec_args(), str(tail)])
            parts = [head]
            if tail_ok and tail.is_file() and tail.stat().st_size > 0:
                parts.append(tail)
            if len(parts) == 1:                              # nothing after the cut -> result is just the head
                self._run(["-i", str(head), "-c", "copy", *self._web_flags(out_path), str(out_path)])
                return out_path.is_file() and out_path.stat().st_size > 0
            listfile = tmp_dir / "concat.txt"
            listfile.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
            ok = self._run(["-f", "concat", "-safe", "0", "-i", str(listfile), "-c", "copy",
                            *self._web_flags(out_path), str(out_path)])
            return ok and out_path.is_file() and out_path.stat().st_size > 0

    def resolve_cut_start(self, source: Path, requested_ms: int) -> int:
        # Ask ffmpeg what the *first frame* its seek to requested_ms lands on is (re-encode: the first
        # frame >= requested_ms; stream copy: the keyframe <= requested_ms) -- showinfo prints its pts.
        req_s = max(0.0, requested_ms / 1000.0)
        args = ["-ss", f"{req_s:.3f}", *([] if self._reencode else ["-noaccurate_seek"]),
                "-i", str(source), "-vf", "showinfo", "-frames:v", "1", "-f", "null", "-"]
        try:
            done = subprocess.run([str(self._ffmpeg), "-hide_banner", "-loglevel", "info", *args],
                                  capture_output=True, timeout=60, check=False)
        except (OSError, subprocess.SubprocessError):
            return requested_ms
        match = re.search(r"pts_time:([\d.]+)", done.stderr.decode("utf-8", "replace"))
        if match is None:
            return requested_ms
        try:
            return round(float(match.group(1)) * 1000.0)
        except ValueError:
            return requested_ms


class UnavailableVideoEditor:
    available = False

    def keep_range(self, source: Path, out_path: Path, *, start_ms: int, end_ms: int) -> bool:
        _ = (source, out_path, start_ms, end_ms)
        return False

    def remove_range(self, source: Path, out_path: Path, *, start_ms: int, end_ms: int) -> bool:
        _ = (source, out_path, start_ms, end_ms)
        return False

    def resolve_cut_start(self, source: Path, requested_ms: int) -> int:
        _ = source
        return requested_ms
