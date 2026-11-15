"""Social-media presets: Shorts, Reels, TikTok, X and Square feed posts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tstudio import ClipError
from tstudio.convert import ConvertPlan, resolve_range, tonemap_filter
from tstudio.media import MediaInfo, encoders_available, filters_available, is_hdr


@dataclass(frozen=True)
class Preset:
    name: str
    label: str          # "YouTube Shorts"
    width: int
    height: int
    max_seconds: int | None
    fps_cap: int | None
    audio_bitrate: str  # "192k"
    note: str           # one short line shown to the user


# Duration limits are what the platforms allowed in November 2026. They
# change, and they are warnings rather than errors - posting is still
# possible past them, just with a heads-up.
PRESETS: dict[str, Preset] = {
    "shorts": Preset(
        "shorts", "YouTube Shorts", 1080, 1920, 180, 60, "192k",
        "9:16 vertical, up to 3 minutes",
    ),
    "reels": Preset(
        "reels", "Instagram Reels", 1080, 1920, 90, 60, "192k",
        "9:16 vertical, up to 90 seconds",
    ),
    "tiktok": Preset(
        "tiktok", "TikTok", 1080, 1920, 600, 60, "192k",
        "9:16 vertical, up to 10 minutes",
    ),
    "x": Preset(
        "x", "X (Twitter)", 1920, 1080, 140, 60, "128k",
        "16:9 landscape, up to 2 minutes 20 seconds",
    ),
    "square": Preset(
        "square", "Square (feed posts)", 1080, 1080, 60, 60, "192k",
        "1:1 square, up to 60 seconds",
    ),
}

_ALIASES: dict[str, str] = {
    "youtube-shorts": "shorts",
    "yt-shorts": "shorts",
    "instagram": "reels",
    "reels": "reels",
    "twitter": "x",
}


def get_preset(name: str) -> Preset:
    """Resolve a preset name or alias to its Preset; ClipError if unknown."""
    key = name.strip().lower()
    if key in PRESETS:
        return PRESETS[key]
    if key in _ALIASES:
        return PRESETS[_ALIASES[key]]
    raise ClipError(
        f"unknown preset {name!r}; valid presets: " + ", ".join(PRESETS)
    )


def _ratios_match(src_w: int, src_h: int, dst_w: int, dst_h: int) -> bool:
    """True when the two aspect ratios agree within 1%."""
    src = src_w / src_h
    dst = dst_w / dst_h
    return abs(src - dst) / dst < 0.01


def _social_chain(info: MediaInfo, width: int, height: int, fit: str) -> str:
    """The -vf chain for a social preset: optional tone-map, then pad/crop."""
    video = info.video
    if _ratios_match(video.width, video.height, width, height):
        chain = f"scale={width}:{height}:flags=lanczos"
    elif fit == "pad":
        chain = (
            f"split[a][b];"
            f"[a]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},gblur=sigma=20[bg];"
            f"[b]scale={width}:-2[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p"
        )
    else:  # crop
        chain = f"scale=-2:{height},crop={width}:{height},format=yuv420p"

    if is_hdr(info.video):
        chain = tonemap_filter(info) + "," + chain
    return chain


def _video_args(encoder: str, quality: int) -> list[str]:
    if encoder in ("hevc_nvenc", "h264_nvenc", "av1_nvenc"):
        return ["-c:v", encoder, "-preset", "p5", "-tune", "hq",
                "-rc", "vbr", "-cq", str(quality), "-b:v", "0"]
    if encoder in ("libx265", "libx264"):
        return ["-c:v", encoder, "-preset", "medium", "-crf", str(quality)]
    raise ClipError(f"unknown encoder {encoder!r}")


def plan_social(
    info: MediaInfo,
    dst: Path,
    preset: Preset | str,
    *,
    fit: str = "pad",
    encoder: str = "h264_nvenc",
    quality: int | None = None,
    mute: bool = False,
    start: float | None = None,
    end: float | None = None,
) -> ConvertPlan:
    """Build the ffmpeg command for a social-media preset.

    Always SDR H.264 by default: tone-maps HDR sources, pads or crops to
    the preset's frame, caps the frame rate, and tags bt709/yuv420p.
    """
    if isinstance(preset, str):
        preset = get_preset(preset)
    video = info.video
    if video is None:
        raise ClipError(f"no video stream in {info.path}")
    if fit not in ("pad", "crop"):
        raise ClipError(f"unknown fit {fit!r}; use 'pad' or 'crop'")

    available = encoders_available()
    if encoder == "h264_nvenc" and "h264_nvenc" not in available:
        encoder = "libx264"
    if encoder not in available:
        raise ClipError(
            f"encoder {encoder!r} is not available; available: "
            + ", ".join(sorted(available))
        )
    if quality is None:
        quality = 21 if encoder.endswith("_nvenc") else 20

    chain = _social_chain(info, preset.width, preset.height, fit)

    start, end = resolve_range(info, start, end)

    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    if start is not None:
        command += ["-ss", f"{start:.3f}"]
    command += ["-i", str(info.path)]
    if end is not None:
        command += ["-t", f"{end - (start or 0.0):.3f}"]
    command += ["-vf", chain]
    if preset.fps_cap is not None and video.fps > preset.fps_cap:
        command += ["-r", str(preset.fps_cap)]

    # Social output is always SDR bt709, whatever the source was.
    command += ["-pix_fmt", "yuv420p",
                "-colorspace", "bt709", "-color_trc", "bt709",
                "-color_primaries", "bt709", "-color_range", "tv"]

    command += _video_args(encoder, quality)
    command += ["-an"] if mute else ["-c:a", "aac", "-b:a", preset.audio_bitrate]
    command += ["-movflags", "+faststart"]
    command.append(str(dst))

    tonemapped = is_hdr(info.video)
    tonemapper: str | None = None
    if tonemapped:
        filters = filters_available()
        if "libplacebo" in filters:
            tonemapper = "libplacebo"
        elif "zscale" in filters:
            tonemapper = "zscale"

    return ConvertPlan(
        src=Path(info.path),
        dst=Path(dst),
        width=preset.width,
        height=preset.height,
        hdr_in=tonemapped,
        hdr_out=False,
        tonemapped=tonemapped,
        tonemapper=tonemapper,
        encoder=encoder,
        quality=quality,
        command=command,
        start=start,
        end=end,
    )


def duration_warning(preset: Preset, seconds: float) -> str | None:
    """One human sentence when the clip exceeds the preset's limit, else None."""
    if preset.max_seconds is None or seconds <= preset.max_seconds:
        return None
    return (
        f"{preset.label} clips are limited to {preset.max_seconds} seconds, "
        f"but this one is {seconds:g} seconds long"
    )
