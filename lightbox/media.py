"""ffprobe wrappers: stream info, duration, keyframe list, encoder availability."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from lightbox import ClipError


@dataclass(frozen=True)
class VideoStream:
    index: int
    codec: str              # "hevc"
    width: int
    height: int
    fps: float              # 120.0, parsed from "120/1"; 0.0 if unknown or "0/0"
    pix_fmt: str | None
    bit_rate: int | None
    color_primaries: str | None = None
    color_transfer: str | None = None
    color_space: str | None = None
    color_range: str | None = None


@dataclass(frozen=True)
class AudioStream:
    index: int
    codec: str
    channels: int | None
    sample_rate: int | None
    bit_rate: int | None


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    duration: float
    size: int               # bytes on disk
    bit_rate: int | None    # overall
    video: VideoStream | None
    audio: tuple[AudioStream, ...]
    stream_count: int       # every stream in the container, including subtitle/data


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a command, raising ClipError for a missing binary or a non-zero exit."""
    if shutil.which(cmd[0]) is None:
        raise ClipError("ffmpeg is not on PATH - install it and try again")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        lines = [line for line in proc.stderr.splitlines() if line.strip()]
        last = lines[-1] if lines else "unknown error"
        raise ClipError(f"cannot read {cmd[-1]}: {last}")
    return proc


def _color_value(value: str | None) -> str | None:
    """A colour tag from ffprobe, or None for missing, empty, or "unknown"."""
    if value is None:
        return None
    value = value.strip()
    if not value or value.lower() == "unknown":
        return None
    return value


def _fps_from_rate(rate: str | None) -> float:
    if not rate:
        return 0.0
    try:
        num, _, den = rate.partition("/")
        num_f = float(num)
        den_f = float(den) if den else 1.0
        if den_f == 0.0:
            return 0.0
        return num_f / den_f
    except ValueError:
        return 0.0


def probe(path: Path) -> MediaInfo:
    """Read container and stream facts from a media file via ffprobe."""
    path = Path(path)
    if not path.exists() or not path.is_file():
        raise ClipError(f"cannot read {path}: not a file")
    proc = _run([
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ])
    import json

    data = json.loads(proc.stdout)
    fmt = data.get("format") or {}
    streams = data.get("streams") or []

    duration = float(fmt.get("duration") or 0.0)
    size = int(fmt.get("size") or 0)
    bit_rate = fmt.get("bit_rate")
    bit_rate = int(bit_rate) if bit_rate is not None else None

    video: VideoStream | None = None
    audio: list[AudioStream] = []
    for s in streams:
        kind = s.get("codec_type")
        if kind == "video" and video is None:
            video = VideoStream(
                index=int(s.get("index", 0)),
                codec=s.get("codec_name") or "",
                width=int(s.get("width") or 0),
                height=int(s.get("height") or 0),
                fps=_fps_from_rate(s.get("r_frame_rate")),
                pix_fmt=s.get("pix_fmt"),
                bit_rate=int(s["bit_rate"]) if s.get("bit_rate") is not None else None,
                color_primaries=_color_value(s.get("color_primaries")),
                color_transfer=_color_value(s.get("color_transfer")),
                color_space=_color_value(s.get("color_space")),
                color_range=_color_value(s.get("color_range")),
            )
        elif kind == "audio":
            audio.append(AudioStream(
                index=int(s.get("index", 0)),
                codec=s.get("codec_name") or "",
                channels=s.get("channels"),
                sample_rate=int(s["sample_rate"]) if s.get("sample_rate") else None,
                bit_rate=int(s["bit_rate"]) if s.get("bit_rate") is not None else None,
            ))

    return MediaInfo(
        path=path,
        duration=duration,
        size=size,
        bit_rate=bit_rate,
        video=video,
        audio=tuple(audio),
        stream_count=len(streams),
    )


def keyframes_in(path: Path, start: float, length: float) -> list[float]:
    """Keyframe times in [start, start + length), sorted and de-duplicated."""
    if length <= 0:
        return []
    start = max(0.0, start)
    proc = _run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-skip_frame", "nokey", "-show_entries", "frame=pts_time",
        "-read_intervals", f"{start:.3f}%+{length:.3f}",
        "-of", "csv=p=0", str(path),
    ])
    times: set[float] = set()
    for line in proc.stdout.splitlines():
        line = line.strip().rstrip(",").strip()
        if not line:
            continue
        try:
            times.add(float(line))
        except ValueError:
            continue
    return sorted(x for x in times if x >= start - 0.001)


def keyframe_at_or_before(path: Path, t: float) -> float:
    """The greatest keyframe time <= t + 0.001, or 0.0 if there is none."""
    if t <= 0:
        return 0.0
    for window in (2.0, 12.0, 60.0, 0.0):
        if window == 0.0:
            times = keyframes_in(path, 0.0, max(t + 1.0, 1.0))
        else:
            times = keyframes_in(path, max(0.0, t - window), window + 1.0)
        candidates = [x for x in times if x <= t + 0.001]
        if candidates:
            return max(candidates)
    return 0.0


def is_hdr(stream: VideoStream | None) -> bool:
    """True when the stream's transfer function is PQ (smpte2084) or HLG (arib-std-b67)."""
    if stream is None or stream.color_transfer is None:
        return False
    return stream.color_transfer.strip().lower() in ("smpte2084", "arib-std-b67")


@lru_cache(maxsize=1)
def filters_available() -> frozenset[str]:
    """The filter names ffmpeg lists, cached for the life of the process."""
    proc = _run(["ffmpeg", "-hide_banner", "-filters"])
    names: set[str] = set()
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 2 and set(parts[0]) <= set("TSO."):
            names.add(parts[1])
    return frozenset(names)


@lru_cache(maxsize=1)
def encoders_available() -> frozenset[str]:
    """The encoder names ffmpeg lists, cached for the life of the process."""
    proc = _run(["ffmpeg", "-hide_banner", "-encoders"])
    names: set[str] = set()
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and (parts[0].startswith("V") or parts[0].startswith("A")):
            names.add(parts[1])
    return frozenset(names)
