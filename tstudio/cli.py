"""The `clip` command: cut a piece out of a video, lossless by default."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

from tstudio import ClipError
from tstudio.convert import TARGETS, plan_convert, run_convert
from tstudio.cut import plan_cut, run_cut
from tstudio.media import MediaInfo, encoders_available, keyframe_at_or_before, keyframes_in, probe
from tstudio.presets import duration_warning, get_preset, plan_social
from tstudio.timespec import (
    format_time,
    output_name,
    output_name_tagged,
    parse_range,
    resolve_relative,
    unique_name,
)

_ENCODERS = ("auto", "hevc_nvenc", "h264_nvenc", "libx265", "libx264")


def _fmt_size(nbytes: int) -> str:
    """Bytes as '896.4 MB' or '3.68 GB', 1024-based, one decimal."""
    mb = nbytes / (1024 * 1024)
    if mb >= 1024:
        return f"{mb / 1024:.2f} GB"
    return f"{mb:.1f} MB"


def _fmt_bitrate(bps: int | None) -> str:
    """Bits per second as '105.0 Mbps' or '192 kbps'; '?' when unknown."""
    if bps is None:
        return "?"
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.1f} Mbps"
    return f"{bps // 1000} kbps"


def _header_lines(info: MediaInfo) -> list[str]:
    """The two-line file summary: name, then resolution/codec/bitrate/size."""
    v = info.video
    res = f"{v.width}x{v.height}" if v else "?"
    fps = f" @{v.fps:g}fps" if v and v.fps else ""
    video_part = f"{v.codec} {_fmt_bitrate(v.bit_rate)}" if v else "no video"
    if info.audio:
        a = info.audio[0]
        audio_part = f" + {a.codec} {_fmt_bitrate(a.bit_rate)}"
    else:
        audio_part = " + no audio"
    return [
        info.path.name,
        f"  {format_time(info.duration)}   {res}{fps}   {video_part}{audio_part}   {_fmt_size(info.size)}",
    ]


def _cut_block(info: MediaInfo, plan, elapsed: float | None, *, quiet: bool, dry_run: bool) -> str:
    """The per-range block printed to stdout."""
    if quiet:
        return str(plan.dst)
    lines = _header_lines(info)
    lines.append("")
    end_text = "end" if plan.end is None else format_time(plan.end)
    cut_end_text = format_time(plan.end if plan.end is not None else info.duration)
    lines.append(f"asked    {format_time(plan.asked_start)} -> {end_text}")
    if plan.lossless:
        if plan.drift >= 0.001:
            lines.append(
                f"cutting  {format_time(plan.start)} -> {cut_end_text}   "
                f"(nearest keyframe, {plan.drift:.3f}s earlier)"
            )
        else:
            lines.append(f"cutting  {format_time(plan.start)} -> {cut_end_text}")
        length = info.duration - plan.start if plan.end is None else plan.end - plan.start
        lines.append(f"length   {format_time(length)}   lossless stream copy")
    else:
        lines.append(f"cutting  {format_time(plan.start)} -> {cut_end_text}")
        length = info.duration - plan.start if plan.end is None else plan.end - plan.start
        lines.append(f"length   {format_time(length)}   re-encoded with {plan.encoder} (cq {plan.command[plan.command.index('-cq') + 1] if '-cq' in plan.command else plan.command[plan.command.index('-crf') + 1]})")
    lines.append("")
    if dry_run:
        for item in plan.command:
            lines.append(f"  {item}")
    else:
        lines.append(f"-> {plan.dst}")
        lines.append(f"   {_fmt_size(plan.dst.stat().st_size)} written in {elapsed:.1f}s")
    return "\n".join(lines) + "\n"


def _convert_block(
    info: MediaInfo,
    plan,
    elapsed: float | None,
    *,
    quiet: bool,
    dry_run: bool,
    target_text: str,
    fit: str | None = None,
    audio_bitrate: str | None = None,
) -> str:
    """The per-range block printed to stdout for a --to / --for conversion."""
    if quiet:
        return str(plan.dst)
    lines = _header_lines(info)
    lines.append("")
    if plan.start is None and plan.end is None:
        lines.append(f"range    whole file ({format_time(info.duration)})")
    else:
        start = plan.start if plan.start is not None else 0.0
        end = plan.end if plan.end is not None else info.duration
        lines.append(f"range    {format_time(start)} -> {format_time(end)}   ({format_time(end - start)})")
    target = f"{target_text}   {plan.width}x{plan.height}"
    if fit:
        target += f"   {fit}"
    lines.append(f"target   {target}")
    if plan.tonemapped:
        colour = f"HDR10 -> SDR, tone-mapped with {plan.tonemapper}"
    elif plan.hdr_out:
        colour = "HDR10 kept (bt2020 / PQ, 10-bit)"
    else:
        colour = "SDR"
    lines.append(f"colour   {colour}")
    encode = f"{plan.encoder} cq {plan.quality}"
    if "-r" in plan.command:
        encode += f", {plan.command[plan.command.index('-r') + 1]} fps cap"
    if audio_bitrate:
        encode += f", aac {audio_bitrate}"
    lines.append(f"encode   {encode}")
    lines.append("")
    if dry_run:
        for item in plan.command:
            lines.append(f"  {item}")
    else:
        lines.append(f"-> {plan.dst}")
        try:
            size = _fmt_size(plan.dst.stat().st_size)
        except OSError:
            size = "?"
        lines.append(f"   {size} written in {elapsed:.1f}s")
    return "\n".join(lines) + "\n"


def _run_conversion(args: argparse.Namespace, info: MediaInfo) -> int:
    """The --to / --for path: re-encode the whole file or each range."""
    if args.out and len(args.ranges) > 1:
        raise ClipError("--out only works with exactly one range")

    if args.for_preset is not None:
        preset = get_preset(args.for_preset)
        fit = args.fit or "pad"
        tag = preset.name
        target_text = preset.label
        audio_bitrate = preset.audio_bitrate
    else:
        preset = None
        fit = None
        tag = args.to
        target_text = args.to
        audio_bitrate = None
        hdr = args.hdr or "auto"

    written = 0
    failed = 0
    for text in args.ranges or [None]:
        if text is None:
            start = end = None
        else:
            start, end = _resolve_range(text, info)
        if args.out:
            dst = Path(args.out)
        else:
            dst = output_name_tagged(info.path, start, end, tag)
            if args.out_dir:
                dst = Path(args.out_dir) / dst.name
        if not args.overwrite:
            dst = unique_name(dst)
        try:
            if preset is not None:
                kwargs: dict = {"fit": fit, "quality": args.quality, "mute": args.mute,
                               "start": start, "end": end}
                if args.encoder != "auto":
                    kwargs["encoder"] = args.encoder
                plan = plan_social(info, dst, preset, **kwargs)
            else:
                plan = plan_convert(
                    info, dst, args.to,
                    hdr=hdr,
                    encoder=args.encoder,
                    quality=args.quality,
                    mute=args.mute,
                    fps=args.fps,
                    start=start,
                    end=end,
                )
            if preset is not None:
                length = (end if end is not None else info.duration) - (start or 0.0)
                warning = duration_warning(preset, length)
                if warning and not args.quiet:
                    print(warning)
            if args.dry_run:
                print(_convert_block(info, plan, None, quiet=args.quiet, dry_run=True,
                                     target_text=target_text, fit=fit,
                                     audio_bitrate=audio_bitrate))
            else:
                elapsed = run_convert(plan)
                print(_convert_block(info, plan, elapsed, quiet=args.quiet, dry_run=False,
                                     target_text=target_text, fit=fit,
                                     audio_bitrate=audio_bitrate))
            written += 1
        except ClipError as exc:
            failed += 1
            print(f"tstudio: {exc}", file=sys.stderr)
        except KeyboardInterrupt:
            if dst.exists():
                dst.unlink()
            print(f"tstudio: interrupted, removed {dst}", file=sys.stderr)
            return 130
        except Exception as exc:  # noqa: BLE001 - never show a traceback
            failed += 1
            print(f"tstudio: {exc}", file=sys.stderr)

    if not args.quiet and not args.dry_run:
        total = written + failed
        if total > 1:
            print(f"{written} of {total} written" if failed else f"{written} written")
    return 2 if failed else 0


def _resolve_range(text: str, info: MediaInfo) -> tuple[float, float | None]:
    """Resolve a range against the file: relative first, then absolute."""
    try:
        return resolve_relative(text, info.duration)
    except ClipError:
        return parse_range(text)


def _progress_callback(info: MediaInfo, plan) -> Callable[[dict], None]:
    """Build the stderr percent/speed/ETA printer for a re-encode cut."""
    target = plan.length if plan.end is not None else info.duration - plan.start

    def on_progress(frame: dict) -> None:
        out = frame.get("out_seconds")
        if out is None:
            return
        pct = min(100.0, max(0.0, out / target * 100.0))
        speed = frame.get("speed")
        eta = None
        if speed and speed > 0:
            eta = (target - out) / speed
        parts = [f"\rlightbox: {pct:5.1f}%"]
        if speed:
            parts.append(f"  {speed:.2f}x")
        if eta is not None and not frame.get("done"):
            parts.append(f"  eta {eta:.0f}s")
        if frame.get("done"):
            parts.append("  done")
        sys.stderr.write("".join(parts).ljust(80) + "\n" if frame.get("done") else "".join(parts) + "\r")
        sys.stderr.flush()

    return on_progress


def _info_block(info: MediaInfo) -> str:
    """The --info output: header, stream list, keyframe spacing."""
    lines = _header_lines(info)
    lines.append("")
    if info.video:
        v = info.video
        lines.append(
            f"video    #{v.index}   {v.codec}   {v.width}x{v.height} @ {v.fps:g}fps   "
            f"{v.pix_fmt or '?'}   {_fmt_bitrate(v.bit_rate)}"
        )
    for a in info.audio:
        lines.append(
            f"audio    #{a.index}   {a.codec}   {a.channels or '?'} ch   "
            f"{a.sample_rate or '?'} Hz   {_fmt_bitrate(a.bit_rate)}"
        )
    if info.stream_count > (1 if info.video else 0) + len(info.audio):
        lines.append(f"other    {info.stream_count - (1 if info.video else 0) - len(info.audio)} more stream(s)")
    lines.append("")
    mid = max(0.0, info.duration / 2.0 - 5.0)
    kfs = keyframes_in(info.path, mid, 10.0)
    if len(kfs) >= 2:
        spacing = (kfs[-1] - kfs[0]) / (len(kfs) - 1)
        lines.append(f"keyframes   every {spacing:.2f}s ({len(kfs)} in a 10s sample)")
    else:
        lines.append("keyframes   not enough keyframes in the sample window")
    return "\n".join(lines) + "\n"


def _keyframes_block(info: MediaInfo, t: float) -> str:
    """The --keyframes output: keyframes near t, with the nearest marked."""
    kfs = keyframes_in(info.path, max(0.0, t - 5.0), 10.0)
    nearest = keyframe_at_or_before(info.path, t)
    lines = []
    for kf in kfs:
        mark = "   <-- nearest at or before " + format_time(t) if abs(kf - nearest) < 0.001 else ""
        lines.append(format_time(kf) + mark)
    return "\n".join(lines) + "\n"


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tstudio",
        description="Cut a piece out of a video. Default is a lossless stream copy: "
        "bit-for-bit the same quality as the source, done in under a second.",
    )
    p.add_argument("input", metavar="INPUT", help="the video file to cut")
    p.add_argument("ranges", metavar="RANGE", nargs="*",
                   help="time range(s): '3:49', '3:49-4:30', '3:49-end', '2m30s-3m'; "
                        "each range makes one output file. Relative forms also work: "
                        "'first 20%%', 'last 45s', 'around 3:49', 'around 3:49 +10s-15s' - "
                        "they are resolved against the file's own length, and because they "
                        "contain spaces they must be quoted, e.g. clip video.mp4 'last 45s'")
    p.add_argument("-o", "--out", metavar="PATH",
                   help="destination for the one range given (error if more than one range)")
    p.add_argument("-O", "--out-dir", metavar="DIR",
                   help="write every output into DIR instead of beside the input")
    p.add_argument("--exact", action="store_true",
                   help="re-encode so the cut starts exactly where asked, instead of snapping "
                        "to a keyframe. Costs quality and time; the default is better for "
                        "almost everything")
    p.add_argument("--to", metavar="TARGET", default=None,
                   help="convert to a resolution: 2160p, 1440p, 1080p, 720p, 480p, 360p, 240p")
    p.add_argument("--for", dest="for_preset", metavar="PRESET", default=None,
                   help="social export: shorts, reels, tiktok, x, square (aliases accepted)")
    p.add_argument("--fit", choices=("pad", "crop"), default=None,
                   help="pad keeps the whole frame on a blurred background; "
                        "crop fills the screen but loses the sides (--for only, pad is the default)")
    p.add_argument("--hdr", choices=("auto", "keep", "sdr"), default=None,
                   help="auto keeps HDR if the source has it; sdr tone-maps it down (--to only, auto is the default)")
    p.add_argument("--fps", type=float, default=None, metavar="N",
                   help="output frame rate (--to only)")
    p.add_argument("--encoder", choices=_ENCODERS, default="auto", metavar="NAME",
                   help="encoder for --exact, --to or --for: auto (default) | hevc_nvenc | "
                        "h264_nvenc | libx265 | libx264")
    p.add_argument("-q", "--quality", type=int, default=None, metavar="N",
                   help="crf/cq for --exact, --to or --for; lower is better")
    p.add_argument("--mute", action="store_true", help="drop the audio")
    p.add_argument("--overwrite", action="store_true",
                   help="replace the destination if it exists")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="print the ffmpeg command(s) and exit without running them")
    p.add_argument("--info", action="store_true",
                   help="print what the file is and exit; no RANGE needed")
    p.add_argument("--keyframes", metavar="TIME",
                   help="print the keyframes within 5 s either side of TIME and exit")
    p.add_argument("--quiet", action="store_true",
                   help="only errors, and the output path on success")
    p.add_argument("--tui", action="store_true",
                   help="open the interactive editor (needs the 'textual' package)")
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        return _run(argv)
    except ClipError as exc:
        print(f"tstudio: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - never show a traceback
        print(f"tstudio: {exc}", file=sys.stderr)
        return 1


def _run(argv: list[str] | None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    converting = args.to is not None or args.for_preset is not None

    if args.to is not None and args.to not in TARGETS:
        raise ClipError(f"unknown target {args.to!r}; valid targets: " + ", ".join(TARGETS))

    if args.to is not None and args.for_preset is not None:
        raise ClipError("--to and --for cannot be combined - a preset already fixes the size")

    if args.fit is not None and args.for_preset is None:
        raise ClipError("--fit only applies with --for")
    if args.hdr is not None and args.to is None:
        raise ClipError("--hdr only applies with --to")
    if args.fps is not None and args.to is None:
        raise ClipError("--fps only applies with --to")

    if args.exact and converting:
        raise ClipError("--exact cannot be combined with --to or --for - a conversion is already a re-encode")

    if args.tui and converting:
        raise ClipError("--tui cannot be combined with --to or --for")

    if args.encoder != "auto" or args.quality is not None:
        if not (args.exact or converting):
            raise ClipError("--encoder only applies with --exact, --to or --for")

    if not args.ranges and not converting and not args.info and args.keyframes is None and not args.tui:
        raise ClipError("no RANGE given - try: clip video.mp4 3:49")

    if args.tui:
        for flag, name in ((args.info, "--info"), (args.keyframes is not None, "--keyframes"),
                           (args.dry_run, "--dry-run"), (args.quiet, "--quiet")):
            if flag:
                raise ClipError(f"--tui cannot be combined with {name}")
        if len(args.ranges) > 1:
            raise ClipError("--tui takes at most one RANGE")
        if args.ranges:
            s = args.ranges[0].strip()
            if "-" in s:
                start_text, end_text = s.split("-", 1)
                start_text, end_text = start_text.strip(), end_text.strip()
            else:
                start_text, end_text = s, ""
        else:
            start_text = end_text = ""
        from tstudio.tui import run_editor
        return run_editor(Path(args.input), start_text, end_text,
                          Path(args.out) if args.out else None)

    info = probe(Path(args.input))

    if args.info:
        print(_info_block(info))
        return 0

    if args.keyframes is not None:
        from tstudio.timespec import parse_time

        t = parse_time(args.keyframes)
        print(_keyframes_block(info, t))
        return 0

    if converting:
        return _run_conversion(args, info)

    if args.out and len(args.ranges) > 1:
        raise ClipError("--out only works with exactly one range")

    written = 0
    failed = 0
    for text in args.ranges:
        start, end = _resolve_range(text, info)
        if args.out:
            dst = Path(args.out)
        else:
            dst = output_name(info.path, start, end)
            if args.out_dir:
                dst = Path(args.out_dir) / dst.name
        if not args.overwrite:
            dst = unique_name(dst)
        try:
            plan = plan_cut(
                info, dst, start, end,
                lossless=not args.exact,
                encoder=args.encoder,
                quality=args.quality,
                mute=args.mute,
            )
            if args.dry_run:
                print(_cut_block(info, plan, None, quiet=args.quiet, dry_run=True))
            else:
                on_progress = _progress_callback(info, plan) if (args.exact and not args.quiet) else None
                try:
                    elapsed = run_cut(plan, on_progress=on_progress)
                except BaseException:
                    if plan.dst.exists():
                        plan.dst.unlink()
                    raise
                print(_cut_block(info, plan, elapsed, quiet=args.quiet, dry_run=False))
            written += 1
        except ClipError as exc:
            failed += 1
            print(f"tstudio: {exc}", file=sys.stderr)
        except KeyboardInterrupt:
            if dst.exists():
                dst.unlink()
            print(f"tstudio: interrupted, removed {dst}", file=sys.stderr)
            return 130
        except Exception as exc:  # noqa: BLE001 - never show a traceback
            failed += 1
            print(f"tstudio: {exc}", file=sys.stderr)

    if not args.quiet and not args.dry_run:
        total = written + failed
        if total > 1:
            print(f"{written} of {total} written" if failed else f"{written} written")
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
