"""Pure text transforms for the popup's Special Paste submenu (no Qt imports).

Ditto-inspired paste-time-only transforms. The popup applies one to a clip's
plain text right before pasting, without mutating the stored clip.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from datetime import datetime


def to_upper(text: str) -> str:
    return text.upper()


def to_lower(text: str) -> str:
    return text.lower()


def capitalize(text: str) -> str:
    """Uppercase the first character, leave the rest untouched.

    Deliberately not Python's str.capitalize(), which also lowercases the
    rest of the string (turning "keeps API" into "Keeps api") -- Ditto's
    Special Paste "Capitalize" only touches the first letter.
    """
    if not text:
        return text
    return text[0].upper() + text[1:]


def trim_whitespace(text: str) -> str:
    return text.strip()


def remove_line_feeds(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def sentence_case(text: str) -> str:
    lowered = text.lower()
    for index, character in enumerate(lowered):
        if character.isalpha():
            return lowered[:index] + character.upper() + lowered[index + 1 :]
    return lowered


def invert_case(text: str) -> str:
    return text.swapcase()


def append_timestamp(text: str, now: datetime | None = None) -> str:
    stamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M")
    return f"{text} {stamp}" if text else stamp


_RU_TO_LAT = str.maketrans(
    {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e",
        "ё": "yo", "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k",
        "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
        "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
        "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "",
        "э": "e", "ю": "yu", "я": "ya",
    }
)


def slugify(text: str) -> str:
    transliterated = text.casefold().translate(_RU_TO_LAT)
    return re.sub(r"[^a-z0-9]+", "-", transliterated).strip("-")


def new_guid(_text: str, factory: Callable[[], uuid.UUID] = uuid.uuid4) -> str:
    return str(factory())


def _reject_json_constant(value: str):
    raise ValueError(f"non-standard JSON constant: {value}")


def _pretty_json_or_none(text: str) -> str | None:
    try:
        value = json.loads(text, parse_constant=_reject_json_constant)
        return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError):
        return None


def is_valid_json(text: str) -> bool:
    return _pretty_json_or_none(text) is not None


def pretty_json(text: str) -> str:
    return _pretty_json_or_none(text) or text


def camel_case(text: str) -> str:
    words = re.findall(r"[^\W_]+", text, flags=re.UNICODE)
    if not words:
        return ""
    first, *rest = (word.casefold() for word in words)
    return first + "".join(word[:1].upper() + word[1:] for word in rest)


# Display label -> transform function, in the order shown in the Special
# Paste submenu (popup.py).
TRANSFORMS: dict[str, Callable[[str], str]] = {
    "UPPERCASE": to_upper,
    "lowercase": to_lower,
    "Capitalize": capitalize,
    "Trim whitespace": trim_whitespace,
    "Remove line feeds": remove_line_feeds,
    "Sentence case": sentence_case,
    "Invert case": invert_case,
    "Paste + timestamp": append_timestamp,
    "Slugify": slugify,
    "New GUID": new_guid,
    "JSON pretty-print": pretty_json,
    "CamelCase": camel_case,
}
