"""Building and running the ffmpeg command for a resolution/colour conversion."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tstudio import ClipError
from tstudio.cut import CutPlan, _pick_encoder, run_cut
from tstudio.media import MediaInfo, filters_available, is_hdr
from tstudio.timespec import format_time

TARGETS = {
    "2160p": 2160, "1440p": 1440, "1080p": 1080, "720p": 720,
    "480p": 480, "360p": 360, "240p": 240,
}

_FASTSTART_SUFFIXES = {".mp4", ".m4v", ".mov"}


@dataclass(frozen=True)
class ConvertPlan:
    src: Path
    dst: Path
    width: int
    height: int
    hdr_in: bool
    hdr_out: bool
    tonemapped: bool          # True only when hdr_in and not hdr_out
    tonemapper: str | None    # "libplacebo" | "zscale" | None
    encoder: str
    quality: int
    command: list[str]
    start: float | None = None   # resolved (after clamping); None = whole file
    end: float | None = None


def _even_width(src_width: int, src_height: int, target_height: int) -> int:
    """Scale the width by the source's aspect ratio, rounded to an even number."""
    exact = src_width * target_height / src_height
    return 2 * int(round(exact / 2))


def _encoder_args(encoder: str, quality: int, mute: bool) -> list[str]:
    if encoder in ("hevc_nvenc", "h264_nvenc", "av1_nvenc"):
        video = ["-c:v", encoder, "-preset", "p5", "-tune", "hq",
                 "-rc", "vbr", "-cq", str(quality), "-b:v", "0"]
    elif encoder in ("libx265", "libx264"):
        video = ["-c:v", encoder, "-preset", "medium", "-crf", str(quality)]
    else:
        raise ClipError(f"unknown encoder {encoder!r}")
    audio = ["-an"] if mute else ["-c:a", "copy"]
    return [*video, *audio]


def tonemap_filter(info: MediaInfo, *, width: int | None = None,
                   height: int | None = None) -> str | None:
    """The libplacebo or zscale tone-map chain for an HDR source.

    Sized when ``width``/``height`` are given, unsized otherwise (the
    unsized form lets a later filter do the scaling). Returns ``None``
    when the source is not HDR.
    """
    if not is_hdr(info.video):
        return None
    filters = filters_available()
    if "libplacebo" in filters:
        opts = []
        if width is not None and height is not None:
            opts += [f"w={width}", f"h={height}"]
        opts += [
            "colorspace=bt709", "color_primaries=bt709", "color_trc=bt709",
            "range=tv", "tonemapping=auto",
        ]
        return "libplacebo=" + ":".join(opts)
    if "zscale" in filters:
        head = (
            f"zscale=w={width}:h={height}:f=lanczos,"
            if width is not None and height is not None
            else ""
        )
        return (
            head
            + "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
            + "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,"
            + "format=yuv420p"
        )
    raise ClipError(
        "this ffmpeg build cannot tone-map (no libplacebo or zscale filter)"
    )


def colour_flags(hdr_out: bool, info: MediaInfo) -> list[str]:
    """The -pix_fmt/-colorspace/-color_trc/-color_primaries/-color_range flags."""
    hdr_in = is_hdr(info.video)
    if hdr_in and not hdr_out:
        return ["-pix_fmt", "yuv420p",
                "-colorspace", "bt709", "-color_trc", "bt709",
                "-color_primaries", "bt709", "-color_range", "tv"]
    if hdr_in and hdr_out:
        return ["-profile:v", "main10", "-pix_fmt", "p010le",
                "-colorspace", "bt2020nc", "-color_trc", "smpte2084",
                "-color_primaries", "bt2020", "-color_range", "pc"]
    video = info.video
    flags = ["-pix_fmt", "yuv420p"]
    if video is not None:
        if video.color_space:
            flags += ["-colorspace", video.color_space]
        if video.color_transfer:
            flags += ["-color_trc", video.color_transfer]
        if video.color_primaries:
            flags += ["-color_primaries", video.color_primaries]
        if video.color_range:
            flags += ["-color_range", video.color_range]
    return flags


def resolve_range(info: MediaInfo, start: float | None, end: float | None) -> tuple[float | None, float | None]:
    """Validate an optional start/end range against the file's duration.

    Returns the resolved (start, end) pair with ``end`` clamped to the
    duration, mirroring ``plan_cut``. Raises ``ClipError`` for a negative
    time, an ``end`` not after ``start``, or a ``start`` at or past the
    end of the file.
    """
    if start is not None and start < 0:
        raise ClipError(f"start {format_time(start)} cannot be negative")
    if end is not None and end < 0:
        raise ClipError(f"end {format_time(end)} cannot be negative")
    if start is not None and start >= info.duration:
        raise ClipError(
            f"start {format_time(start)} is at or past the end of the file "
            f"({format_time(info.duration)})"
        )
    if end is not None and end > info.duration:
        end = info.duration
    if end is not None and end <= (start or 0.0):
        raise ClipError(
            f"end {format_time(end)} must be after start "
            f"{format_time(start or 0.0)}"
        )
    return start, end


def plan_convert(
    info: MediaInfo,
    dst: Path,
    target: str = "1080p",
    *,
    hdr: str = "auto",
    encoder: str = "auto",
    quality: int | None = None,
    mute: bool = False,
    fps: float | None = None,
    start: float | None = None,
    end: float | None = None,
) -> ConvertPlan:
    """Validate a conversion against the file and build the ffmpeg command."""
    if target not in TARGETS:
        raise ClipError(
            f"unknown target {target!r}; valid targets: " + ", ".join(TARGETS)
        )
    target_height = TARGETS[target]

    video = info.video
    if video is None:
        raise ClipError(f"no video stream in {info.path}")

    if target_height > video.height:
        raise ClipError(
            f"target {target_height}p is above the source's {video.height}p - "
            "refusing to upscale"
        )
    width = _even_width(video.width, video.height, target_height)

    hdr_in = is_hdr(video)
    if hdr == "auto":
        hdr_out = hdr_in
    elif hdr == "keep":
        hdr_out = True
        if not hdr_in:
            raise ClipError("HDR cannot be invented from an SDR source")
    elif hdr == "sdr":
        hdr_out = False
    else:
        raise ClipError(f"unknown hdr mode {hdr!r}; use 'auto', 'keep' or 'sdr'")

    tonemapped = hdr_in and not hdr_out
    tonemapper: str | None = None
    if tonemapped:
        filters = filters_available()
        if "libplacebo" in filters:
            tonemapper = "libplacebo"
        elif "zscale" in filters:
            tonemapper = "zscale"
        else:
            raise ClipError(
                "this ffmpeg build cannot tone-map (no libplacebo or zscale filter)"
            )

    chosen = _pick_encoder(info, encoder)
    if hdr_out and chosen in ("h264_nvenc", "libx264"):
        raise ClipError(
            f"encoder {chosen!r} cannot write HDR; pass encoder='hevc_nvenc' "
            "or encoder='libx265'"
        )
    if quality is None:
        quality = 21 if chosen.endswith("_nvenc") else 20

    start, end = resolve_range(info, start, end)

    if tonemapped:
        # libplacebo scales and tone-maps in one filter - no separate scale.
        chain = tonemap_filter(info, width=width, height=target_height)
    else:
        chain = f"scale={width}:{target_height}:flags=lanczos"

    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    if start is not None:
        command += ["-ss", f"{start:.3f}"]
    command += ["-i", str(info.path)]
    if end is not None:
        command += ["-t", f"{end - (start or 0.0):.3f}"]
    command += ["-vf", chain]
    if fps is not None:
        command += ["-r", str(fps)]

    command += colour_flags(hdr_out, info)

    command += _encoder_args(chosen, quality, mute)
    if Path(dst).suffix.lower() in _FASTSTART_SUFFIXES:
        command += ["-movflags", "+faststart"]
    command.append(str(dst))

    return ConvertPlan(
        src=Path(info.path),
        dst=Path(dst),
        width=width,
        height=target_height,
        hdr_in=hdr_in,
        hdr_out=hdr_out,
        tonemapped=tonemapped,
        tonemapper=tonemapper,
        encoder=chosen,
        quality=quality,
        command=command,
        start=start,
        end=end,
    )


def run_convert(plan: ConvertPlan, *, on_progress=None) -> float:
    """Run the plan's ffmpeg command; return wall-clock seconds.

    Delegates to ``run_cut``, which already creates the destination's parent,
    checks for ffmpeg, and raises ``ClipError`` starting ``ffmpeg failed:``
    with the last 3 non-empty stderr lines on a non-zero exit.
    """
    cut_plan = CutPlan(
        src=plan.src,
        dst=plan.dst,
        asked_start=0.0,
        start=0.0,
        end=None,
        drift=0.0,
        lossless=False,
        encoder=plan.encoder,
        command=plan.command,
    )
    return run_cut(cut_plan, on_progress=on_progress)
