"""Command-shape tests for tstudio.recipe: a preset is only a starting point."""

from __future__ import annotations

import dataclasses
import unittest
from pathlib import Path
from unittest import mock

from tstudio import ClipError
from tstudio.media import MediaInfo, VideoStream
from tstudio.presets import get_preset, plan_social
from tstudio.recipe import (
    SHAPES,
    Recipe,
    describe,
    is_lossless,
    plan_from_recipe,
    recipe_for_preset,
    target_size,
)

_ENCODERS = frozenset({
    "h264_nvenc", "hevc_nvenc", "av1_nvenc", "libx264", "libx265", "aac",
})


def _hdr_info(path: Path = Path("in.mp4")) -> MediaInfo:
    """A 4K 120fps HDR10 source, 60 s long so a 10-15 s range is in range."""
    return MediaInfo(
        path=path,
        duration=60.0,
        size=10_000_000,
        bit_rate=105_000_000,
        video=VideoStream(
            index=0,
            codec="hevc",
            width=3840,
            height=2160,
            fps=120.0,
            pix_fmt="yuv420p10le",
            bit_rate=105_000_000,
            color_primaries="bt2020",
            color_transfer="smpte2084",
            color_space="bt2020nc",
            color_range="pc",
        ),
        audio=(),
        stream_count=1,
    )


def _sdr_info(path: Path = Path("in.mp4")) -> MediaInfo:
    """A 1920x1080 30fps SDR source, 60 s long."""
    return MediaInfo(
        path=path,
        duration=60.0,
        size=1_000_000,
        bit_rate=8_000_000,
        video=VideoStream(
            index=0,
            codec="h264",
            width=1920,
            height=1080,
            fps=30.0,
            pix_fmt="yuv420p",
            bit_rate=8_000_000,
            color_primaries="bt709",
            color_transfer="bt709",
            color_space="bt709",
            color_range="tv",
        ),
        audio=(),
        stream_count=1,
    )


class LosslessTests(unittest.TestCase):
    def test_defaults_are_lossless(self):
        self.assertTrue(is_lossless(Recipe()))

    def test_any_picture_change_is_not_lossless(self):
        self.assertFalse(is_lossless(Recipe(size="1080p")))
        self.assertFalse(is_lossless(Recipe(shape="9:16")))
        self.assertFalse(is_lossless(Recipe(colour="sdr")))

    def test_dropping_audio_is_not_lossless(self):
        self.assertFalse(is_lossless(Recipe(audio="drop")))

    def test_fps_and_encoder_alone_are_ignored(self):
        # Documented choice: irrelevant when nothing else changes.
        self.assertTrue(is_lossless(Recipe(fps_cap=30)))
        self.assertTrue(is_lossless(Recipe(encoder="libx264", quality=18)))


class RecipeForPresetTests(unittest.TestCase):
    def test_shorts(self):
        recipe = recipe_for_preset("shorts")
        self.assertEqual(recipe.size, "1080p")
        self.assertEqual(recipe.shape, "9:16")
        self.assertEqual(recipe.colour, "sdr")
        self.assertEqual(recipe.fps_cap, 60)
        self.assertEqual(recipe.fit, "pad")
        self.assertEqual(recipe.audio, "keep")

    def test_x_is_landscape(self):
        recipe = recipe_for_preset("x")
        self.assertEqual(recipe.size, "1080p")
        self.assertEqual(recipe.shape, "16:9")
        self.assertEqual(recipe.colour, "sdr")
        self.assertEqual(recipe.fps_cap, 60)

    def test_square(self):
        self.assertEqual(recipe_for_preset("square").shape, "1:1")

    def test_alias_still_works(self):
        self.assertEqual(
            recipe_for_preset("youtube-shorts"), recipe_for_preset("shorts")
        )

    def test_unknown_name_raises(self):
        with self.assertRaises(ClipError) as ctx:
            recipe_for_preset("instagrarn")
        self.assertIn("shorts", str(ctx.exception))

    def test_every_preset_round_trips(self):
        for name in ("shorts", "reels", "tiktok", "x", "square"):
            recipe = recipe_for_preset(name)
            preset = get_preset(name)
            with self.subTest(preset=name):
                self.assertEqual(
                    target_size(recipe, _sdr_info()),
                    (preset.width, preset.height),
                )


class TargetSizeTests(unittest.TestCase):
    def test_shapes_table(self):
        self.assertEqual(SHAPES["keep"], None)
        self.assertEqual(SHAPES["9:16"], (9, 16))
        self.assertEqual(SHAPES["1:1"], (1, 1))
        self.assertEqual(SHAPES["16:9"], (16, 9))

    def test_all_keep_has_no_size(self):
        self.assertIsNone(target_size(Recipe(), _hdr_info()))

    def test_size_only_follows_the_source_ratio(self):
        self.assertEqual(
            target_size(Recipe(size="1080p"), _hdr_info()), (1920, 1080)
        )

    def test_size_with_a_shape(self):
        info = _hdr_info()
        self.assertEqual(
            target_size(Recipe(size="1080p", shape="9:16"), info), (1080, 1920)
        )
        self.assertEqual(
            target_size(Recipe(size="1080p", shape="1:1"), info), (1080, 1080)
        )
        self.assertEqual(
            target_size(Recipe(size="1080p", shape="16:9"), info), (1920, 1080)
        )

    def test_shape_only_uses_the_sources_longer_side(self):
        self.assertEqual(
            target_size(Recipe(shape="9:16"), _hdr_info()), (2160, 3840)
        )

    def test_every_dimension_is_even(self):
        info = _hdr_info()
        for recipe in (
            Recipe(size="1080p"),
            Recipe(size="720p"),
            Recipe(size="1080p", shape="9:16"),
            Recipe(size="720p", shape="1:1"),
            Recipe(shape="16:9"),
            Recipe(shape="9:16"),
        ):
            width, height = target_size(recipe, info)
            with self.subTest(recipe=recipe):
                self.assertEqual(width % 2, 0)
                self.assertEqual(height % 2, 0)

    def test_bad_fields_raise(self):
        info = _hdr_info()
        for recipe in (
            Recipe(size="4320p"),
            Recipe(shape="4:3"),
            Recipe(shape="9:16", fit="squash"),
            Recipe(colour="hdr"),
            Recipe(audio="mono"),
        ):
            with self.subTest(recipe=recipe), self.assertRaises(ClipError):
                target_size(recipe, info)


class PlanShapeTests(unittest.TestCase):
    """No ffmpeg: the encoder/filter probes are stubbed out."""

    def _plan(self, info, recipe, *, encoder="libx264", filters=("libplacebo",), **kw):
        with mock.patch("tstudio.convert._pick_encoder", return_value=encoder), \
             mock.patch("tstudio.presets.encoders_available", return_value=_ENCODERS), \
             mock.patch(
                 "tstudio.convert.filters_available",
                 return_value=frozenset(filters),
             ), \
             mock.patch(
                 "tstudio.presets.filters_available",
                 return_value=frozenset(filters),
             ):
            return plan_from_recipe(info, Path("out.mp4"), recipe, **kw)

    # 3. The point of the whole module: override one field of a preset.
    def test_a_preset_is_only_a_starting_point(self):
        recipe = dataclasses.replace(recipe_for_preset("shorts"), colour="keep")
        plan = self._plan(_hdr_info(), recipe, encoder="hevc_nvenc")

        # Still vertical: keeping HDR must not cancel the reframe.
        self.assertEqual((plan.width, plan.height), (1080, 1920))
        self.assertEqual(target_size(recipe, _hdr_info()), (1080, 1920))

        command = " ".join(plan.command)
        self.assertIn("smpte2084", command)
        self.assertIn("p010le", command)          # 10-bit
        self.assertIn("main10", command)
        self.assertNotIn("bt709", command)        # no SDR tag anywhere
        self.assertNotIn("libplacebo", command)   # not tone-mapped
        self.assertNotIn("tonemap", command)
        self.assertNotIn("format=yuv420p", command)
        self.assertTrue(plan.hdr_out)
        self.assertFalse(plan.tonemapped)
        self.assertIsNone(plan.tonemapper)

        # And it really is the pad chain doing the reframing.
        chain = plan.command[plan.command.index("-vf") + 1]
        self.assertIn("gblur", chain)
        self.assertIn("overlay", chain)

    def test_hdr_reframe_crops_without_flattening(self):
        recipe = dataclasses.replace(
            recipe_for_preset("shorts"), colour="keep", fit="crop"
        )
        plan = self._plan(_hdr_info(), recipe, encoder="hevc_nvenc")
        chain = plan.command[plan.command.index("-vf") + 1]
        self.assertIn("crop=1080:1920", chain)
        self.assertNotIn("format=yuv420p", chain)
        self.assertNotIn("libplacebo", chain)
        self.assertEqual((plan.width, plan.height), (1080, 1920))

    def test_hdr_reframe_refuses_h264(self):
        recipe = dataclasses.replace(
            recipe_for_preset("shorts"), colour="keep", encoder="libx264"
        )
        with self.assertRaises(ClipError) as ctx:
            self._plan(_hdr_info(), recipe)
        message = str(ctx.exception)
        self.assertIn("HDR", message)
        self.assertIn("hevc_nvenc", message)

    def test_shorts_at_720p_stays_sdr(self):
        recipe = dataclasses.replace(recipe_for_preset("shorts"), size="720p")
        plan = self._plan(_hdr_info(), recipe)
        self.assertEqual((plan.width, plan.height), (720, 1280))
        command = " ".join(plan.command)
        self.assertIn("bt709", command)
        self.assertIn("yuv420p", command)
        self.assertNotIn("smpte2084", command)
        self.assertNotIn("p010le", command)
        self.assertTrue(plan.tonemapped)

    def test_shorts_with_audio_dropped(self):
        recipe = dataclasses.replace(recipe_for_preset("shorts"), audio="drop")
        plan = self._plan(_hdr_info(), recipe)
        self.assertIn("-an", plan.command)
        self.assertNotIn("-c:a", plan.command)
        self.assertEqual((plan.width, plan.height), (1080, 1920))

    def test_default_recipe_is_a_stream_copy(self):
        plan = self._plan(_sdr_info(), Recipe())
        command = plan.command
        self.assertIn("-c copy", " ".join(command))
        self.assertNotIn("-c:v", command)
        self.assertNotIn("-crf", command)
        self.assertNotIn("-cq", command)

    def test_dropping_audio_mutes_the_output(self):
        plan = self._plan(_sdr_info(), Recipe(audio="drop"))
        self.assertIn("-an", plan.command)

    def test_shape_change_pads_or_crops(self):
        info = _sdr_info()
        padded = " ".join(
            self._plan(info, Recipe(size="1080p", shape="9:16", fit="pad")).command
        )
        self.assertIn("gblur", padded)
        self.assertIn("overlay", padded)

        cropped = " ".join(
            self._plan(info, Recipe(size="1080p", shape="9:16", fit="crop")).command
        )
        self.assertIn("crop=1080:1920", cropped)
        self.assertNotIn("gblur", cropped)
        self.assertNotIn("overlay", cropped)

    def test_size_only_is_a_plain_scale(self):
        command = " ".join(self._plan(_sdr_info(), Recipe(size="720p")).command)
        self.assertIn("scale=1280:720", command)
        self.assertNotIn("gblur", command)
        self.assertNotIn("overlay", command)
        self.assertNotIn("crop=", command)

    def test_the_range_reaches_both_branches(self):
        with mock.patch("tstudio.cut.keyframe_at_or_before", return_value=10.0):
            lossless = self._plan(
                _sdr_info(), Recipe(), start=10.0, end=15.0
            ).command
        self.assertEqual(lossless[lossless.index("-ss") + 1], "10.000")
        self.assertEqual(lossless[lossless.index("-t") + 1], "5.000")

        reencoded = self._plan(
            _sdr_info(), Recipe(size="720p"), start=10.0, end=15.0
        ).command
        self.assertEqual(reencoded[reencoded.index("-ss") + 1], "10.000")
        self.assertEqual(reencoded[reencoded.index("-t") + 1], "5.000")
        self.assertLess(reencoded.index("-ss"), reencoded.index("-i"))

    def test_a_preset_recipe_reproduces_plan_social(self):
        info = _sdr_info()
        mine = self._plan(info, recipe_for_preset("shorts"))
        with mock.patch("tstudio.presets.encoders_available", return_value=_ENCODERS):
            theirs = plan_social(info, Path("out.mp4"), get_preset("shorts"))
        self.assertEqual(mine.command, theirs.command)
        self.assertEqual((mine.width, mine.height), (theirs.width, theirs.height))

    def test_shorts_but_1080p_overrides_the_presets_own_frame(self):
        recipe = dataclasses.replace(recipe_for_preset("shorts"), size="720p")
        plan = self._plan(_sdr_info(), recipe)
        self.assertEqual((plan.width, plan.height), (720, 1280))

    def test_encoder_and_quality_pass_through(self):
        plan = self._plan(
            _sdr_info(), Recipe(size="720p", encoder="libx264", quality=27)
        )
        self.assertEqual(plan.quality, 27)
        self.assertIn("27", plan.command)

        social = self._plan(
            _sdr_info(), Recipe(shape="1:1", encoder="libx264", quality=29)
        )
        self.assertEqual(social.quality, 29)
        self.assertEqual(social.encoder, "libx264")

    def test_fps_cap_reaches_the_command(self):
        plan = self._plan(_sdr_info(), Recipe(size="720p", fps_cap=24))
        command = plan.command
        self.assertEqual(command[command.index("-r") + 1], "24")

    def test_planner_errors_propagate(self):
        with self.assertRaises(ClipError) as ctx:
            self._plan(_sdr_info(), Recipe(size="2160p"))
        self.assertIn("upscale", str(ctx.exception))


class DescribeTests(unittest.TestCase):
    def test_lossless_lines(self):
        lines = describe(Recipe(), _hdr_info())
        self.assertEqual(lines[0], "lossless stream copy")
        self.assertEqual(lines[1], "nothing about the picture changes")
        self.assertEqual(len(lines), 2)

    def test_reason_names_only_what_changes(self):
        lines = describe(Recipe(colour="sdr"), _hdr_info())
        self.assertIn("colour", lines[1])
        self.assertNotIn("size", lines[1])
        self.assertNotIn("shape", lines[1])

    def test_full_social_recipe(self):
        lines = describe(recipe_for_preset("shorts"), _hdr_info())
        self.assertTrue(lines[0].startswith("re-encode"))
        self.assertIn("1080p", lines[0])
        self.assertIn("9:16 pad", lines[0])
        self.assertIn("HDR10 → SDR", lines[0])
        self.assertEqual(lines[1], "the size, shape and colour all change")
        self.assertLessEqual(len(lines), 3)

    def test_crop_is_named(self):
        recipe = dataclasses.replace(recipe_for_preset("shorts"), fit="crop")
        self.assertIn("9:16 crop", describe(recipe, _hdr_info())[0])

    def test_keeping_hdr_still_names_the_shape_change(self):
        recipe = dataclasses.replace(recipe_for_preset("shorts"), colour="keep")
        lines = describe(recipe, _hdr_info())
        self.assertEqual(len(lines), 2)
        self.assertIn("9:16 pad", lines[0])
        self.assertNotIn("SDR", lines[0])
        self.assertEqual(lines[1], "the size and shape change")

    def test_describe_does_not_touch_the_disk(self):
        # Pure function: no ffmpeg/ffprobe probes, so nothing to stub.
        info = _sdr_info(Path("nowhere/at/all.mp4"))
        for recipe in (Recipe(), Recipe(size="720p"), Recipe(audio="drop"),
                       Recipe(size="1080p", shape="9:16")):
            with self.subTest(recipe=recipe):
                self.assertGreaterEqual(len(describe(recipe, info)), 2)


if __name__ == "__main__":
    unittest.main()
