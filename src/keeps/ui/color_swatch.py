"""Parse standalone CSS-like color clips for Ф18 list swatches."""

from __future__ import annotations

import colorsys
import math
import re

RGB = tuple[int, int, int]


def parse_color(text: str) -> RGB | None:
    value = text.strip().casefold()
    if match := re.fullmatch(r"#([0-9a-f]{3}|[0-9a-f]{6})", value):
        digits = match.group(1)
        if len(digits) == 3:
            digits = "".join(character * 2 for character in digits)
        return tuple(int(digits[index : index + 2], 16) for index in (0, 2, 4))

    if match := re.fullmatch(r"rgb\(([^)]+)\)", value):
        parts = [part.strip() for part in match.group(1).split(",")]
        if len(parts) != 3:
            return None
        channels = [_parse_rgb_channel(part) for part in parts]
        return tuple(channels) if all(channel is not None for channel in channels) else None

    if match := re.fullmatch(r"hsl\(([^)]+)\)", value):
        parts = [part.strip() for part in match.group(1).split(",")]
        if len(parts) != 3 or not parts[1].endswith("%") or not parts[2].endswith("%"):
            return None
        try:
            raw_hue = float(parts[0])
            saturation = float(parts[1][:-1]) / 100
            lightness = float(parts[2][:-1]) / 100
        except ValueError:
            return None
        if not all(math.isfinite(value) for value in (raw_hue, saturation, lightness)):
            return None
        hue = raw_hue % 360 / 360
        if not 0 <= saturation <= 1 or not 0 <= lightness <= 1:
            return None
        red, green, blue = colorsys.hls_to_rgb(hue, lightness, saturation)
        return tuple(round(channel * 255) for channel in (red, green, blue))
    return None


def _parse_rgb_channel(value: str) -> int | None:
    try:
        if value.endswith("%"):
            percentage = float(value[:-1])
            if not math.isfinite(percentage):
                return None
            channel = round(percentage * 255 / 100)
        else:
            channel = int(value)
    except (OverflowError, ValueError):
        return None
    return channel if 0 <= channel <= 255 else None
