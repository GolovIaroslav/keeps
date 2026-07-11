"""Pure text transforms for the popup's Special Paste submenu (no Qt imports).

Ditto-inspired but a minimal subset (PLAN.md §13 Ф9.1): UPPERCASE, lowercase,
Capitalize, and Trim whitespace. These are paste-time-only transforms -- the
popup applies one to a clip's plain text right before pasting, without ever
mutating the stored clip (see popup.py's Special Paste menu handling).
"""

from __future__ import annotations

from collections.abc import Callable


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


# Display label -> transform function, in the order shown in the Special
# Paste submenu (popup.py).
TRANSFORMS: dict[str, Callable[[str], str]] = {
    "UPPERCASE": to_upper,
    "lowercase": to_lower,
    "Capitalize": capitalize,
    "Trim whitespace": trim_whitespace,
}
