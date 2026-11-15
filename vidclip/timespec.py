"""Parse time and range strings and name output files - pure, no ffmpeg."""

from __future__ import annotations

import re
from pathlib import Path

from vidclip import ClipError

_PLAIN = re.compile(r"^\d*\.?\d+$")
_COLON_FIELD = re.compile(r"^\d+(\.\d+)?$")
_UNIT_TOKEN = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")
_UNIT_RANK = {"h": 0, "m": 1, "s": 2, "ms": 3}
_UNIT_SECONDS = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}


def _fail(text: object, reason: str) -> None:
    raise ClipError(f"cannot read {text!r} as a time ({reason})")


def _parse_plain(text: str) -> float:
    value = float(text)
    if value < 0:
        _fail(text, "a time cannot be negative")
    return value


def _parse_units(text: str) -> float:
    text = text.lower()
    tokens = _UNIT_TOKEN.findall(text)
    if not tokens or "".join(num + unit for num, unit in tokens) != text:
        _fail(text, "expected a number with h, m, s or ms units in descending order")
    total = 0.0
    last_rank = -1
    for num, unit in tokens:
        rank = _UNIT_RANK[unit]
        if rank <= last_rank:
            _fail(text, f"units must be in descending order and each at most once (saw {unit!r})")
        last_rank = rank
        total += float(num) * _UNIT_SECONDS[unit]
    return total


def _parse_colon(text: str) -> float:
    fields = text.split(":")
    if len(fields) not in (2, 3):
        _fail(text, "colon form needs two or three fields")
    for i, field in enumerate(fields):
        if not _COLON_FIELD.match(field):
            _fail(text, "each field must be a non-negative number, and only the last may have a decimal point")
        if i < len(fields) - 1 and "." in field:
            _fail(text, "only the last field may have a decimal point")
        value = float(field)
        if i > 0 and value >= 60:
            _fail(text, "minutes and seconds must be under 60")
    if len(fields) == 2:
        return float(fields[0]) * 60.0 + float(fields[1])
    return float(fields[0]) * 3600.0 + float(fields[1]) * 60.0 + float(fields[2])


def parse_time(text: str) -> float:
    """Turn a typed time like '3:49', '1:02:03.5', '90s' or '2m30s' into seconds."""
    if not isinstance(text, str):
        raise ClipError(f"cannot read {text!r} as a time (expected a string)")
    s = text.strip()
    if not s:
        _fail(text, "empty string")
    if ":" in s:
        return _parse_colon(s)
    if _PLAIN.match(s):
        return _parse_plain(s)
    return _parse_units(s)


# Relative range forms, resolved against a known media duration:
#   "first 10%"      -> 0.0 to duration*0.10
#   "first 30s"      -> 0.0 to 30s
#   "last 20%"       -> duration*0.80 to duration
#   "last 45s"       -> duration-45 to duration
#   "around 3:49"    -> [t-5, t+5] clamped to [0, duration]
#   "around 3:49 +10s" / "around 3:49 +10s-15s"
_REL_FIRST = re.compile(r"^first\s+(\d*\.?\d+(?:%|[hms]|ms))$", re.IGNORECASE)
_REL_LAST = re.compile(r"^last\s+(\d*\.?\d+(?:%|[hms]|ms))$", re.IGNORECASE)
_REL_AROUND = re.compile(
    r"^around\s+(.+?)"
    r"(?:\s+\+([\d.]+)(?:%|[hms]|ms)(?:-([\d.]+)(?:%|[hms]|ms))?)?$",
    re.IGNORECASE,
)
_AROUND_DEFAULT_HALF = 5.0


def _as_fraction(token: str, duration: float) -> float:
    """A number optionally followed by '%' or a time unit, to seconds."""
    if token.endswith("%"):
        return float(token[:-1]) / 100.0 * duration
    return parse_time(token)


def resolve_relative(text: str, duration: float) -> tuple[float, float | None]:
    """Resolve a duration-relative spec like 'first 10%', 'last 45s',
    'around 3:49' to a concrete (start, end) pair.

    Pure and duration-aware; it does not touch parse_time/parse_range, whose
    absolute-string contracts stay intact. Raises ClipError on anything it
    cannot resolve, so callers can fall back to parse_range."""
    if not isinstance(text, str):
        raise ClipError(f"cannot read {text!r} as a relative range (expected a string)")
    if duration <= 0:
        raise ClipError("cannot resolve a relative range for a file with no duration")
    s = text.strip()
    if not s:
        raise ClipError("cannot read an empty string as a relative range")

    m = _REL_FIRST.match(s)
    if m:
        end = min(_as_fraction(m.group(1), duration), duration)
        return 0.0, end if end > 0 else None

    m = _REL_LAST.match(s)
    if m:
        span = _as_fraction(m.group(1), duration)
        start = max(0.0, duration - span)
        return start, duration

    m = _REL_AROUND.match(s)
    if m:
        center = parse_time(m.group(1))
        half_after = _as_fraction(m.group(2), duration) if m.group(2) else _AROUND_DEFAULT_HALF
        half_before = _as_fraction(m.group(3), duration) if m.group(3) else half_after
        start = max(0.0, center - half_before)
        end = min(duration, center + half_after)
        if end <= start:
            raise ClipError(
                f"cannot read {text!r} as a relative range "
                f"(the window around {format_time(center)} is empty at this length)"
            )
        return start, end

    raise ClipError(f"cannot read {text!r} as a relative range")


def parse_range(text: str) -> tuple[float, float | None]:
    """Split a range like '3:49-4:30' into (start, end); end is None for 'to the end'."""
    if not isinstance(text, str):
        raise ClipError(f"cannot read {text!r} as a range (expected a string)")
    s = text.strip()
    if not s:
        raise ClipError(f"cannot read {text!r} as a range (empty string)")
    if "-" not in s:
        return parse_time(s), None
    start_text, end_text = s.split("-", 1)
    start_text = start_text.strip()
    if not start_text:
        raise ClipError(f"cannot read {text!r} as a range (a range needs a start time)")
    start = parse_time(start_text)
    end_text = end_text.strip()
    if end_text.lower() in ("", "end", "eof"):
        return start, None
    end = parse_time(end_text)
    if end <= start:
        raise ClipError(
            f"cannot read {text!r} as a range (end {end_text!r} must be after start {start_text!r})"
        )
    return start, end


def format_time(seconds: float) -> str:
    """Format seconds for humans, always with milliseconds: '3:49.000' or '1:02:03.500'."""
    if not isinstance(seconds, (int, float)):
        raise ClipError(f"cannot format {seconds!r} as a time (expected a number of seconds)")
    if seconds < 0:
        raise ClipError(f"cannot format {seconds!r} as a time (a time cannot be negative)")
    ms = int(round(seconds * 1000))
    total_seconds, frac = divmod(ms, 1000)
    minutes, secs = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}.{frac:03d}"
    return f"{minutes}:{secs:02d}.{frac:03d}"


def slug_time(seconds: float) -> str:
    """The same number, safe for a Windows filename: '3-49', '3-49.5', '1-02-03'."""
    if not isinstance(seconds, (int, float)):
        raise ClipError(f"cannot slug {seconds!r} as a time (expected a number of seconds)")
    if seconds < 0:
        raise ClipError(f"cannot slug {seconds!r} as a time (a time cannot be negative)")
    ms = int(round(seconds * 1000))
    total_seconds, frac = divmod(ms, 1000)
    minutes, secs = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        base = f"{hours}-{minutes:02d}-{secs:02d}"
    else:
        base = f"{minutes}-{secs:02d}"
    if frac:
        base += "." + str(frac).rstrip("0")
    return base


def output_name(src: Path, start: float, end: float | None) -> Path:
    """A sibling of src with the range in the stem: 'raid_3-49_to_4-30.mp4'."""
    if end is None:
        range_part = f"{slug_time(start)}_to_end"
    else:
        range_part = f"{slug_time(start)}_to_{slug_time(end)}"
    return src.with_name(f"{src.stem}_{range_part}{src.suffix}")


def output_name_tagged(src: Path, start: float | None, end: float | None, tag: str) -> Path:
    """Like output_name, with _<tag> appended to the stem: 'raid_3-49_to_4-19_shorts.mp4'.

    When both start and end are None the range part is omitted entirely:
    'raid_1080p.mp4'."""
    if start is None and end is None:
        return src.with_name(f"{src.stem}_{tag}{src.suffix}")
    if end is None:
        range_part = f"{slug_time(start)}_to_end"
    else:
        range_part = f"{slug_time(start)}_to_{slug_time(end)}"
    return src.with_name(f"{src.stem}_{range_part}_{tag}{src.suffix}")


def unique_name(path: Path) -> Path:
    """Return path if free, else insert _2, _3, ... before the suffix until free."""
    if not path.exists():
        return path
    for i in range(2, 1000):
        candidate = path.with_name(f"{path.stem}_{i}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise ClipError(f"no free name for {path} (tried up to _999)")
