"""Building and running the ffmpeg command for a cut."""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from lightbox import ClipError
from lightbox.media import MediaInfo, encoders_available, keyframe_at_or_before
from lightbox.timespec import format_time

_FASTSTART_SUFFIXES = {".mp4", ".m4v", ".mov"}

# on_progress is called once per progress frame from ffmpeg. It receives a
# plain dict with: elapsed (float seconds so far), out_seconds (media time
# produced, or None before the first frame), speed (realtime multiplier, or
# None when ffmpeg has not reported one), and done (True on the final frame).
ProgressCallback = Callable[[dict], None]


@dataclass(frozen=True)
class CutPlan:
    src: Path
    dst: Path
    asked_start: float
    start: float            # where the cut really begins
    end: float | None       # where it really ends; None = end of file
    drift: float            # asked_start - start, always >= 0, 0.0 when re-encoding
    lossless: bool
    encoder: str | None     # None when lossless
    command: list[str]      # argv for ffmpeg, ready to run

    @property
    def length(self) -> float | None:
        return None if self.end is None else self.end - self.start


def _pick_encoder(info: MediaInfo, encoder: str) -> str:
    available = encoders_available()
    if encoder != "auto":
        if encoder not in available:
            raise ClipError(
                f"encoder {encoder!r} is not available; available: "
                + ", ".join(sorted(available))
            )
        return encoder
    codec = info.video.codec if info.video else ""
    if codec == "hevc" and "hevc_nvenc" in available:
        return "hevc_nvenc"
    if codec == "h264" and "h264_nvenc" in available:
        return "h264_nvenc"
    return "libx265"


def _codec_args(encoder: str, info: MediaInfo, quality: int, mute: bool) -> list[str]:
    pix_fmt: list[str] = []
    if info.video is not None and info.video.pix_fmt:
        pix_fmt = ["-pix_fmt", info.video.pix_fmt]
    audio: list[str] = ["-an"] if mute else ["-c:a", "copy"]
    if encoder == "hevc_nvenc":
        return ["-c:v", "hevc_nvenc", "-preset", "p5", "-tune", "hq",
                "-rc", "vbr", "-cq", str(quality), "-b:v", "0", *pix_fmt, *audio]
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq",
                "-rc", "vbr", "-cq", str(quality), "-b:v", "0", *pix_fmt, *audio]
    if encoder in ("libx265", "libx264"):
        return ["-c:v", encoder, "-preset", "medium", "-crf", str(quality),
                *pix_fmt, *audio]
    raise ClipError(f"unknown encoder {encoder!r}")


def plan_cut(
    info: MediaInfo,
    dst: Path,
    start: float,
    end: float | None = None,
    *,
    lossless: bool = True,
    encoder: str = "auto",
    quality: int | None = None,
    mute: bool = False,
) -> CutPlan:
    """Validate a cut against the file and build the ffmpeg command."""
    if start >= info.duration:
        raise ClipError(
            f"start {format_time(start)} is at or past the end of the file "
            f"({format_time(info.duration)})"
        )
    if end is not None and end > info.duration:
        end = info.duration

    if end is not None and end <= start:
        raise ClipError(
            f"end {format_time(end)} must be after start {format_time(start)}"
        )

    asked_start = start
    if lossless:
        start = keyframe_at_or_before(info.path, start)
        drift = asked_start - start
        chosen: str | None = None
    else:
        drift = 0.0
        chosen = _pick_encoder(info, encoder)
        if quality is None:
            quality = 19 if chosen.endswith("_nvenc") else 18

    if end is not None and end <= start:
        raise ClipError(
            f"end {format_time(end)} is not after the snapped start "
            f"{format_time(start)}; this should not happen - please report it"
        )

    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-ss", f"{start:.3f}", "-i", str(info.path),
    ]
    if end is not None:
        command += ["-t", f"{end - start:.3f}"]
    command += ["-map", "0"]
    if lossless:
        command += ["-c", "copy"]
        if mute:
            command += ["-an"]
        command += ["-avoid_negative_ts", "make_zero"]
    else:
        command += _codec_args(chosen, info, quality, mute)
    if Path(dst).suffix.lower() in _FASTSTART_SUFFIXES:
        command += ["-movflags", "+faststart"]
    command.append(str(dst))

    return CutPlan(
        src=Path(info.path),
        dst=Path(dst),
        asked_start=asked_start,
        start=start,
        end=end,
        drift=drift,
        lossless=lossless,
        encoder=chosen,
        command=command,
    )


def _fail_from_stderr(stderr: str) -> ClipError:
    lines = [line for line in stderr.splitlines() if line.strip()]
    tail = "\n".join(lines[-3:]) if lines else "unknown error"
    return ClipError(f"ffmpeg failed: {tail}")


def run_cut(plan: CutPlan, *, on_progress: ProgressCallback | None = None) -> float:
    """Run the plan's ffmpeg command; return wall-clock seconds.

    With no callback this blocks on the command and streams its output to
    pipes, exactly as before. With an ``on_progress`` callback the command is
    run with ``-progress pipe:1`` and a stats dict is delivered once per
    progress frame, so callers can render live percent/speed/ETA.
    """
    plan.dst.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which("ffmpeg") is None:
        raise ClipError("ffmpeg is not on PATH - install it and try again")

    if on_progress is None:
        t0 = time.monotonic()
        proc = subprocess.run(plan.command, capture_output=True, text=True)
        elapsed = time.monotonic() - t0
        if proc.returncode != 0:
            raise _fail_from_stderr(proc.stderr)
        return elapsed

    # Progress path: parse key=value frames from stdout while draining
    # stderr on a side thread so neither pipe can deadlock.
    command = plan.command[:-1] + ["-progress", "pipe:1", "-nostats"] + [plan.command[-1]]
    t0 = time.monotonic()
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stderr_chunks: list[str] = []
    err_thread = threading.Thread(
        target=lambda: stderr_chunks.append(proc.stderr.read()), daemon=True
    )
    err_thread.start()

    stats: dict[str, str] = {}
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, value = line.split("=", 1)
            stats[key] = value
            if key == "progress":
                out_time_us = stats.get("out_time_us")
                speed = stats.get("speed")
                if speed:
                    speed = float(speed.rstrip("x"))
                else:
                    speed = None
                on_progress({
                    "elapsed": time.monotonic() - t0,
                    "out_seconds": float(out_time_us) / 1e6 if out_time_us else None,
                    "speed": speed,
                    "done": value == "end",
                })
    finally:
        proc.wait()
        err_thread.join()
        stderr = "".join(stderr_chunks)
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()
    if proc.returncode != 0:
        raise _fail_from_stderr(stderr)
    return time.monotonic() - t0
