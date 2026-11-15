# vclip

`vclip` cuts a piece out of a video file with one short command. By default it does a
**lossless stream copy**: the clip is bit-for-bit the same quality as the source, and a
4 GB cut takes under a second because nothing is re-encoded. It also converts clips -
downscaling 4K to 1080p, tone-mapping HDR to SDR - and prepares them for social
platforms like YouTube Shorts, Instagram Reels, and TikTok. It snaps the start to the
nearest keyframe and tells you exactly where the cut landed and how far that is from the
time you asked for.

## Install

It is one folder, no dependencies - just Python 3.12 and ffmpeg on PATH (you already have
both). The one optional extra is `textual`, needed only for the `--tui` editor; the CLI
works without it. To make `vclip` work from anywhere, the wrapper is `vclip.cmd` in
`C:\Users\mehul\.local\bin` (already on PATH):

    @echo off
    rem vclip - lossless video trimming. Source: C:\Users\mehul\chats\vidclip
    "C:\Program Files\Python312\python.exe" "C:\Users\mehul\chats\vidclip\clip.py" %*

The name is `vclip` rather than `clip` because Windows ships its own `clip.exe` for the
clipboard and that one wins on PATH.

Claude set that up for you.

## Use

    vclip video.mp4 3:49
    Cut from 3:49 to the end of the file. The output lands beside the input, named
    video_3-49_to_end.mp4. This is the ShadowPlay case: read the time off the player,
    type it, done.

    vclip video.mp4 3:49-4:30
    Cut a specific window. The output is video_3-49_to_4-30.mp4.

    vclip video.mp4 1:00-1:30 5:00-5:45 9:12
    Several ranges in one command: two clips and one "to the end" clip, written in the
    order given.

    vclip video.mp4 "first 20%"
    vclip video.mp4 "last 45s"
    vclip video.mp4 "around 3:49"
    Relative ranges, resolved against the file's own length: "first N%" / "first Ns"
    from the top, "last N%" / "last Ns" up to the end, or "around T" for a window
    centred on T (5 s either side by default). Size the window explicitly with
    "around 3:49 +10s" (10 s each way) or "around 3:49 +10s-15s" (10 s before, 15 s
    after). Both % and h/m/s/ms units are understood, and everything is clamped to the
    file, so "last 9999s" on a 2 m clip just gives you the whole tail.

    vclip video.mp4 3:49 -O D:\clips
    Write the output into D:\clips instead of beside the input. Handy when the
    recording lives on a drive you do not want to fill up.

    vclip video.mp4 --info
    What the file is: resolution, fps, codecs, bitrates, size, and how far apart the
    keyframes are. No cutting.

    vclip video.mp4 3:49 --dry-run
    Print the exact ffmpeg command that would run, without running it. Read it, copy
    it, or check the tool is doing what you expect.

    vclip video.mp4 3:49 --exact --encoder hevc_nvenc -q 19
    Re-encode so the cut starts exactly at 3:49.000 instead of snapping to a keyframe.
    Costs quality and time; see below for when it is worth it.

Other options: `-o PATH` for an exact destination name (one range only), `--mute` to
drop the audio, `--overwrite` to replace an existing output, `--quiet` for just the
output path.

## Convert

`--to` re-encodes the clip at a lower resolution. It only goes down - asking to go up
raises an error - and the width follows the source's aspect ratio, so an ultrawide file
stays ultrawide.

    --to TARGET     2160p, 1440p, 1080p, 720p, 480p, 360p, 240p
    --hdr MODE      auto (default) | keep | sdr      (only with --to)
    --fps N         output frame rate                (only with --to)

    vclip raid.mp4 3:49-4:19 --to 1080p
    Downscale a 4K clip to 1080p. `--hdr auto` (the default) keeps HDR when the source
    is HDR; here it would, so the output stays HDR10.

    vclip raid.mp4 3:49-4:19 --to 1080p --hdr sdr
    Same downscale, but tone-mapped down to SDR. Use this when the destination cannot
    show HDR.

    vclip raid.mp4 3:49-4:19 --to 1080p --hdr keep
    Keep HDR, and fail on an SDR source rather than inventing HDR. `auto` is the
    sensible default; `keep` is for when you want the failure.

    vclip raid.mp4 3:49-4:19 --to 720p --fps 60
    Downscale and cap the frame rate at 60. Your captures are 120 fps; most players
    and platforms do not need that.

`--to` cannot be combined with `--for` (see below) or with `--exact`, because a
conversion is already a re-encode. It accepts a RANGE, so it converts only that part of
the file, in one ffmpeg pass with no temporary file.

## Post it somewhere

`--for` renders a clip in a platform's shape and codec, ready to upload. Every preset
outputs SDR H.264 + AAC with `+faststart`, and caps the frame rate at 60 - your
captures are 120 fps. The duration limits are what the platforms allowed in November
2026; they change, and going over prints a warning rather than failing.

    --for PRESET    shorts | reels | tiktok | x | square
    --fit MODE      pad (default) | crop              (only with --for)

| Preset | Platform            | Size      | Warns over |
|--------|---------------------|-----------|------------|
| shorts | YouTube Shorts      | 1080x1920 | 180 s      |
| reels  | Instagram Reels     | 1080x1920 | 90 s       |
| tiktok | TikTok              | 1080x1920 | 600 s      |
| x      | X (Twitter)         | 1920x1080 | 140 s      |
| square | Square (feed posts) | 1080x1080 | 60 s       |

Aliases: `youtube-shorts`, `yt-shorts`, `instagram`, `reels`, `twitter`.

`pad` puts the whole frame on a blurred copy of itself, so nothing is lost but the
picture sits in the middle third of a phone screen. `crop` fills the screen and throws
away the left and right thirds, including any HUD out there. `pad` is the default
because it never silently discards picture.

    vclip raid.mp4 3:49-4:19 --for shorts --fit crop
    A 30-second vertical clip from the 3.68 GB capture: 132 MB, 1080x1920, and it took
    28 seconds to render. A lossless cut of the same window is still under a second.

`--for` cannot be combined with `--to` or with `--exact`, for the same reason as
above. It accepts a RANGE, so it converts only that part of the file.

## HDR

Your ShadowPlay captures are HDR10: bt2020 colour, PQ curve, 10-bit. A naive downscale
makes them grey and washed out, because the PQ curve gets read as ordinary gamma. So
the tool tone-maps when it converts: with libplacebo when it is available, and a zscale
chain otherwise. The social presets are always SDR, because the platforms are. `--to`
keeps HDR by default (`--hdr auto`), so a plain downscale of an HDR clip stays HDR
unless you ask for `--hdr sdr`.

## Why the start time moves

A stream copy cannot start on an arbitrary frame: it can only start on a keyframe,
because the frames between keyframes are stored as differences from the last keyframe.
So when you ask for 3:49, the tool finds the last keyframe at or before 3:49 and starts
there. Your ShadowPlay recordings have keyframes every 0.25 s, so a cut can land at
most 0.25 s early - you will see a note like `(nearest keyframe, 0.177s earlier)`.
Other sources can have keyframes 2-10 s apart, where the drift is much bigger, which is
why the tool always prints the real start time and the drift instead of pretending the
cut is where you asked.

## When to use --exact

`--exact` re-encodes the clip, which is the only way to start exactly on the frame you
asked for. On a 4K120 HEVC file it costs real time (a minute of footage takes a few
minutes even on an RTX 5090) and loses a little quality, however high the bitrate. For
almost everything - trimming gameplay, sharing a moment, cleaning up a recording - the
default lossless copy is better: the drift is a quarter of a second at most, and the
quality is untouched.

While it runs, `--exact` reports progress on stderr, one line per update:
`clip: 42.3%  3.71x  eta 18s` — percent of the clip encoded, current speed relative to
real time, and the estimated seconds left. It ends with `clip: done`. The lossless
copy is too fast to bother with, so this only appears when you actually re-encode (and
is silenced by `--quiet`).

## TUI

The interactive editor is the new way in:

    vclip video.mp4 --tui

It opens a single-clip editor built on Textual. There is a file panel, a Range panel
with Start and End, an Output panel with mode, encoder, quality and mute, and a
"What will happen" panel that names the keyframe the cut will snap to and the output
file. `Cut (F5)` runs it and fills a progress bar; `Preview (F2)` toggles the exact
ffmpeg command. Keys: `F5` cut, `F2` toggle the ffmpeg command, `F3` reset the inputs,
`Ctrl+Q` quit.

`python clip_tui.py VIDEO` still works if you prefer it. It still needs `textual`,
which stays an optional dependency - the CLI works without it:

    pip install textual

The TUI edits one clip losslessly; `--to` and `--for` are command-line only for now.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | everything written |
| 2 | a usage error or a failed cut (one line on stderr) |
| 1 | anything else (ffmpeg missing, unexpected failure) |
