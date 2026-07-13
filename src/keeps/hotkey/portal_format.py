"""Qt-free formatting helpers for the XDG GlobalShortcuts portal."""

from __future__ import annotations


def xdg_trigger(sequence: str) -> str:
    """Convert a one-chord Qt sequence into the XDG shortcuts syntax."""
    parts = [part.strip() for part in sequence.split("+") if part.strip()]
    if not parts:
        raise ValueError("empty shortcut")
    *modifiers, key = parts
    modifier_names = {
        "ctrl": "CTRL",
        "control": "CTRL",
        "alt": "ALT",
        "shift": "SHIFT",
        "meta": "LOGO",
        "super": "LOGO",
    }
    translated = [modifier_names[modifier.casefold()] for modifier in modifiers]
    key_names = {"`": "grave", "Space": "space", "Return": "Return", "Enter": "Return"}
    translated.append(
        key_names.get(key, key.casefold() if len(key) == 1 and key.isalpha() else key)
    )
    return "+".join(translated)
