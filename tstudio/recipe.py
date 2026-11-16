"""A recipe of overridable defaults: what changes about a clip, not a mode."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path

from tstudio import ClipError
from tstudio.convert import TARGETS, ConvertPlan, _even_width, plan_convert
from tstudio.cut import CutPlan, plan_cut
from tstudio.media import MediaInfo, VideoStream, is_hdr
from tstudio.presets import get_preset, plan_social

SHAPES: dict[str, tuple[int, int] | None] = {
    "keep": None,
    "9:16": (9, 16),
    "1:1": (1, 1),
    "16:9": (16, 9),
}

_COLOURS = ("keep", "sdr")
_AUDIOS = ("keep", "drop")
_FITS = ("pad", "crop")

# The preset a reframing recipe borrows its non-geometry settings from (the
# audio bitrate), picked by shape so that a recipe built by
# ``recipe_for_preset`` reproduces that preset's command exactly.
_BASE_PRESETS: dict[str, str] = {"9:16": "shorts", "16:9": "x", "1:1": "square"}

# How a source's transfer function is named in a human line.
_HDR_LABELS = {"smpte2084": "HDR10", "arib-std-b67": "HLG"}


@dataclass(frozen=True)
class Recipe:
    """What the output should differ in from the source; everything else is kept.

    A preset is only a set of starting values for these fields (see
    ``recipe_for_preset``). Every field can then be overridden with
    ``dataclasses.replace``, which is what makes "Shorts, but at 1080p, and
    keep HDR" expressible at all.
    """

    size: str = "keep"        # "keep" or a key of convert.TARGETS
    shape: str = "keep"       # "keep" | "9:16" | "1:1" | "16:9"
    fit: str = "pad"          # "pad" | "crop" - only used when shape changes
    colour: str = "keep"      # "keep" | "sdr"
    audio: str = "keep"       # "keep" | "drop"
    fps_cap: int | None = None
    encoder: str = "auto"
    quality: int | None = None


def _video(info: MediaInfo) -> VideoStream:
    video = info.video
    if video is None:
        raise ClipError(f"no video stream in {info.path}")
    return video


def _validate(recipe: Recipe) -> None:
    """Raise ClipError for any field value this module does not understand."""
    if recipe.size != "keep" and recipe.size not in TARGETS:
        raise ClipError(
            f"unknown size {recipe.size!r}; use 'keep' or one of: "
            + ", ".join(TARGETS)
        )
    if recipe.shape not in SHAPES:
        raise ClipError(
            f"unknown shape {recipe.shape!r}; use one of: " + ", ".join(SHAPES)
        )
    if recipe.fit not in _FITS:
        raise ClipError(f"unknown fit {recipe.fit!r}; use 'pad' or 'crop'")
    if recipe.colour not in _COLOURS:
        raise ClipError(f"unknown colour {recipe.colour!r}; use 'keep' or 'sdr'")
    if recipe.audio not in _AUDIOS:
        raise ClipError(f"unknown audio {recipe.audio!r}; use 'keep' or 'drop'")


def _even(value: float) -> int:
    """Round to the nearest even number, as convert._even_width does."""
    return 2 * int(round(value / 2))


def _ratios_match(src_w: int, src_h: int, dst_w: int, dst_h: int) -> bool:
    """True when the two aspect ratios agree within 1%."""
    src = src_w / src_h
    dst = dst_w / dst_h
    return abs(src - dst) / dst < 0.01


def _shape_of(width: int, height: int) -> str:
    """The SHAPES key a pixel size has; ClipError when it is none of them."""
    for key, ratio in SHAPES.items():
        if ratio is None:
            continue
        if _ratios_match(width, height, *ratio):
            return key
    raise ClipError(
        f"{width}x{height} is not one of the known shapes: "
        + ", ".join(k for k in SHAPES if k != "keep")
    )


def _size_key_for_source(video: VideoStream) -> str:
    """The TARGETS key that means "the size it already is"."""
    for key, height in TARGETS.items():
        if height == video.height:
            return key
    raise ClipError(
        f"the source is {video.height}p, which is not one of the known sizes "
        f"({', '.join(TARGETS)}); pick a size instead of 'keep'"
    )


def recipe_for_preset(name: str) -> Recipe:
    """The Recipe that reproduces a social preset exactly.

    Size and shape are read back off the preset's pixel size: the size key is
    named for the *shorter* side, which is how people say "1080p vertical" for
    a 1080x1920 frame. Every social preset is SDR, and ``fit`` starts at the
    existing ``plan_social`` default of "pad". Names and aliases resolve
    through ``presets.get_preset``, so an unknown name raises its ClipError
    listing the valid presets.
    """
    preset = get_preset(name)
    shape = _shape_of(preset.width, preset.height)
    shorter = min(preset.width, preset.height)
    size = next((key for key, h in TARGETS.items() if h == shorter), None)
    if size is None:
        raise ClipError(
            f"preset {preset.name!r} is {preset.width}x{preset.height}, whose "
            f"shorter side is not one of: " + ", ".join(TARGETS)
        )
    return Recipe(
        size=size,
        shape=shape,
        fit="pad",
        colour="sdr",
        audio="keep",
        fps_cap=preset.fps_cap,
    )


def is_lossless(recipe: Recipe) -> bool:
    """True when nothing about the picture or the sound changes.

    That is the whole definition of "lossless" here: it is not a mode anyone
    picks, it is simply what is left when size, shape and colour are all
    "keep" and the audio is kept, so the streams can be copied.

    ``fps_cap``, ``encoder`` and ``quality`` are deliberately ignored. A frame
    rate cap or an encoder choice would rule out a stream copy on its own, but
    they are also meaningless when nothing else changes - there is no re-encode
    for them to configure - so they are treated as unset rather than as a
    reason to re-encode.
    """
    return (
        recipe.size == "keep"
        and recipe.shape == "keep"
        and recipe.colour == "keep"
        and recipe.audio == "keep"
    )


def target_size(recipe: Recipe, info: MediaInfo) -> tuple[int, int] | None:
    """The output pixel size, or None when size and shape are both "keep".

    With no shape change the height comes from ``convert.TARGETS`` and the
    width follows the source's aspect ratio, rounded even, exactly as
    ``plan_convert`` does it. With a shape, the size key names the shorter
    side of the new frame ("1080p" is 1080x1920 at 9:16 and 1920x1080 at
    16:9); with size "keep" the source's own longer side becomes the longer
    side of the new frame. Both dimensions are always even.
    """
    _validate(recipe)
    if recipe.size == "keep" and recipe.shape == "keep":
        return None

    video = _video(info)
    ratio = SHAPES[recipe.shape]

    if ratio is None:
        height = TARGETS[recipe.size]
        return _even_width(video.width, video.height, height), height

    width_ratio, height_ratio = ratio
    longer_ratio = max(width_ratio, height_ratio)
    shorter_ratio = min(width_ratio, height_ratio)
    if recipe.size == "keep":
        longer = float(max(video.width, video.height))
        shorter = longer * shorter_ratio / longer_ratio
    else:
        shorter = float(TARGETS[recipe.size])
        longer = shorter * longer_ratio / shorter_ratio

    if width_ratio >= height_ratio:
        return _even(longer), _even(shorter)
    return _even(shorter), _even(longer)


def _keeps_hdr(recipe: Recipe, info: MediaInfo) -> bool:
    """True when the output should carry the source's HDR through."""
    return recipe.colour == "keep" and is_hdr(info.video)


def _shape_changes(recipe: Recipe, info: MediaInfo) -> bool:
    ratio = SHAPES[recipe.shape]
    if ratio is None:
        return False
    video = _video(info)
    return not _ratios_match(video.width, video.height, *ratio)


def _size_changes(recipe: Recipe, info: MediaInfo) -> bool:
    if recipe.size == "keep":
        return False
    video = _video(info)
    return TARGETS[recipe.size] != min(video.width, video.height)


def _colour_changes(recipe: Recipe, info: MediaInfo) -> bool:
    return recipe.colour == "sdr" and is_hdr(info.video)


def _hdr_label(info: MediaInfo) -> str:
    video = info.video
    transfer = (video.color_transfer or "").strip().lower() if video else ""
    return _HDR_LABELS.get(transfer, "HDR")


def _joined(names: list[str]) -> str:
    """"the colour changes" / "the size and colour change" / "... all change"."""
    if len(names) == 1:
        return f"the {names[0]} changes"
    if len(names) == 2:
        return f"the {names[0]} and {names[1]} change"
    return "the " + ", ".join(names[:-1]) + f" and {names[-1]} all change"


def describe(recipe: Recipe, info: MediaInfo) -> list[str]:
    """Two or three short lines a UI can show. Pure; no side effects.

    The first line is either "lossless stream copy" or "re-encode" followed by
    what will actually change. The second line is the plain-sentence reason it
    is not a stream copy, naming only what really changes against this source.
    """
    _validate(recipe)
    video = _video(info)

    if is_lossless(recipe):
        return ["lossless stream copy", "nothing about the picture changes"]

    reframes = recipe.shape != "keep"
    size_changes = _size_changes(recipe, info)
    shape_changes = _shape_changes(recipe, info)
    colour_changes = _colour_changes(recipe, info)
    audio_changes = recipe.audio == "drop"
    fps_changes = recipe.fps_cap is not None and video.fps > recipe.fps_cap

    parts = ["re-encode"]
    if size_changes:
        parts.append(recipe.size)
    elif reframes and recipe.size == "keep":
        size = target_size(recipe, info)
        if size is not None:
            parts.append(f"{size[0]}x{size[1]}")
    if shape_changes:
        parts.append(f"{recipe.shape} {recipe.fit}")
    if colour_changes:
        parts.append(f"{_hdr_label(info)} → SDR")
    if fps_changes:
        parts.append(f"{recipe.fps_cap}fps")
    if audio_changes:
        parts.append("no audio")

    names: list[str] = []
    if size_changes:
        names.append("size")
    if shape_changes:
        names.append("shape")
    if colour_changes:
        names.append("colour")
    if audio_changes:
        names.append("audio")

    if names:
        reason = _joined(names)
    elif fps_changes:
        reason = "the frame rate changes"
    else:
        reason = "the picture is re-encoded at the same size, shape and colour"

    return [" · ".join(parts), reason]


def plan_from_recipe(
    info: MediaInfo,
    dst: Path,
    recipe: Recipe,
    *,
    start: float | None = None,
    end: float | None = None,
) -> ConvertPlan | CutPlan:
    """Build the plan for a recipe by dispatching to the existing planners.

    Nothing here assembles an ffmpeg argv of its own. A lossless recipe goes
    to ``cut.plan_cut``; a reframing one to ``presets.plan_social`` with a
    Preset whose width, height and fps cap come from the recipe rather than
    from the preset itself; anything else to ``convert.plan_convert``. The
    encoder, quality, mute flag and the start/end range are passed through in
    every branch, and a ClipError from the planner propagates unchanged.

    A shape change always reframes. Keeping the colour ("keep" on an HDR
    source) only switches ``plan_social`` into its HDR output mode; it never
    cancels the reframe, so "Shorts, but keep HDR" is vertical HDR.
    """
    _validate(recipe)
    video = _video(info)
    mute = recipe.audio == "drop"

    if is_lossless(recipe):
        return plan_cut(
            info,
            dst,
            start or 0.0,
            end,
            lossless=True,
            encoder=recipe.encoder,
            quality=recipe.quality,
            mute=mute,
        )

    if recipe.shape != "keep":
        size = target_size(recipe, info)
        assert size is not None  # shape != "keep", so there is always a size
        width, height = size
        base = get_preset(_BASE_PRESETS[recipe.shape])
        preset = dataclasses.replace(
            base, width=width, height=height, fps_cap=recipe.fps_cap
        )
        return plan_social(
            info,
            dst,
            preset,
            fit=recipe.fit,
            encoder=recipe.encoder,
            quality=recipe.quality,
            mute=mute,
            hdr_out=_keeps_hdr(recipe, info),
            start=start,
            end=end,
        )

    target = recipe.size if recipe.size != "keep" else _size_key_for_source(video)
    return plan_convert(
        info,
        dst,
        target,
        hdr="sdr" if recipe.colour == "sdr" else "auto",
        encoder=recipe.encoder,
        quality=recipe.quality,
        mute=mute,
        fps=recipe.fps_cap,
        start=start,
        end=end,
    )
