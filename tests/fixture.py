"""Test video fixtures for the vidclip test suite.

Claude-authored infrastructure. Qwen jobs must NOT edit this file; they only call
`ensure_sample()` / `ffmpeg_available()` from their tests.

Builds tiny, deterministic mp4 files with ffmpeg so the tests can perform real cuts
and assert real durations instead of mocking ffmpeg.

  ensure_sample("dense")  -> 10.0 s, keyframe every 0.5 s (frames 0, 15, 30, ...)
  ensure_sample("sparse") -> 10.0 s, keyframe every 2.0 s (frames 0, 60, 120, ...)
  ensure_sample("silent") -> 10.0 s, dense keyframes, no audio stream
  ensure_sample("hdr")    ->  4.0 s, real HDR10: 10-bit, bt2020 / smpte2084 (PQ)

The first three are 640x360 / 30 fps / h264 + aac SDR. "hdr" is 640x360 / 30 fps /
hevc Main 10 + aac and is the only fixture that exercises a tone-mapping code path -
use it for anything that branches on is_hdr(). Files are cached in tests/_fixtures/
and rebuilt only if missing, so the suite stays fast.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent / "_fixtures"

DURATION = 10.0
WIDTH = 640
HEIGHT = 360
FPS = 30

# name -> (gop size in frames, has_audio)
_SPECS = {
    "dense": (15, True),
    "sparse": (60, True),
    "silent": (15, False),
}

HDR_DURATION = 4.0  # shorter: an HEVC 10-bit encode is slower to build than h264


def ffmpeg_available() -> bool:
    """True if both ffmpeg and ffprobe are on PATH."""
    return bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe"))


def ensure_sample(kind: str = "dense") -> Path:
    """Return the path to the named sample video, building it if needed.

    Raises RuntimeError if ffmpeg is unavailable, KeyError for an unknown kind.
    """
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg/ffprobe not found on PATH")
    if kind == "hdr":
        return _ensure_hdr_sample()

    gop, has_audio = _SPECS[kind]

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    out = FIXTURE_DIR / f"{kind}.mp4"
    if out.exists() and out.stat().st_size > 0:
        return out

    tmp = out.with_suffix(".building.mp4")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi",
        "-i", f"testsrc2=size={WIDTH}x{HEIGHT}:rate={FPS}:duration={DURATION}",
    ]
    if has_audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={DURATION}"]
    cmd += [
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-g", str(gop),
        "-keyint_min", str(gop),
        "-sc_threshold", "0",
        "-pix_fmt", "yuv420p",
    ]
    if has_audio:
        cmd += ["-c:a", "aac", "-b:a", "64k"]
    else:
        cmd += ["-an"]
    cmd += ["-shortest", str(tmp)]

    subprocess.run(cmd, check=True, capture_output=True)
    tmp.replace(out)
    return out


def _ensure_hdr_sample() -> Path:
    """A genuine HDR10 clip: 10-bit HEVC tagged bt2020 / smpte2084 (PQ).

    This is the only fixture whose colour metadata makes is_hdr() true, so it is
    the one that actually runs a tone-mapping chain end to end.
    """
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    out = FIXTURE_DIR / "hdr.mp4"
    if out.exists() and out.stat().st_size > 0:
        return out

    tmp = out.with_suffix(".building.mp4")
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi",
            "-i", f"testsrc2=size={WIDTH}x{HEIGHT}:rate={FPS}:duration={HDR_DURATION}",
            "-f", "lavfi",
            "-i", f"sine=frequency=440:duration={HDR_DURATION}",
            "-c:v", "libx265",
            "-preset", "ultrafast",
            "-pix_fmt", "yuv420p10le",
            "-x265-params",
            "colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:log-level=none",
            "-colorspace", "bt2020nc",
            "-color_trc", "smpte2084",
            "-color_primaries", "bt2020",
            "-c:a", "aac", "-b:a", "64k",
            "-shortest", str(tmp),
        ],
        check=True, capture_output=True,
    )
    tmp.replace(out)
    return out


def probe_duration(path: str | Path) -> float:
    """Container duration in seconds, via ffprobe."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout
    return float(json.loads(out)["format"]["duration"])


def probe_video_codec(path: str | Path) -> str:
    """Video codec name of the first video stream, via ffprobe."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout
    return json.loads(out)["streams"][0]["codec_name"]


def keyframe_times(path: str | Path) -> list[float]:
    """Presentation timestamps of every keyframe in the first video stream."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey",
         "-show_entries", "frame=pts_time", "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout
    times = []
    for line in out.splitlines():
        value = line.strip().rstrip(",").strip()
        if value:
            times.append(float(value))
    return times


if __name__ == "__main__":
    for name in (*_SPECS, "hdr"):
        p = ensure_sample(name)
        print(f"{name:7s} {p}  {probe_duration(p):.3f}s  "
              f"{len(keyframe_times(p))} keyframes")
