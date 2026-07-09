"""Pure, Qt-free helpers shared by hotkey backends.

Kept separate from kglobalaccel.py/x11.py so pytest can exercise this logic
without dragging Qt/D-Bus/X11 into module import (see MEMORY.md's CI lesson:
a pure-logic helper must not live in a module with top-level Qt imports).
"""

from __future__ import annotations

# X11 keysym names that don't match their Qt QKeySequence spelling.
# Letters, digits, and most named keys (F1, Return, Home, ...) already match
# X11's keysymdef.h names exactly, so only punctuation and a few oddities
# need an override here.
KEY_NAME_OVERRIDES = {
    "`": "grave",
    "-": "minus",
    "=": "equal",
    "[": "bracketleft",
    "]": "bracketright",
    ";": "semicolon",
    "'": "apostrophe",
    ",": "comma",
    ".": "period",
    "/": "slash",
    "\\": "backslash",
    "Space": "space",
}

MODIFIER_MASKS = {
    "ctrl": 1 << 2,  # ControlMask
    "shift": 1 << 0,  # ShiftMask
    "alt": 1 << 3,  # Mod1Mask
    "meta": 1 << 6,  # Mod4Mask
}


def parse_key_sequence(text: str) -> tuple[int, str]:
    """Parses a "Ctrl+`"-style string into (X11 modifier mask, keysym name)."""
    parts = [p.strip() for p in text.split("+") if p.strip()]
    if not parts:
        raise ValueError(f"empty key sequence: {text!r}")
    *modifiers, key = parts
    mask = 0
    for mod in modifiers:
        mask |= MODIFIER_MASKS[mod.lower()]
    return mask, KEY_NAME_OVERRIDES.get(key, key)
