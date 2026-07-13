"""Declarative, user-configurable popup key bindings (PLAN.md Ф22)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PopupKeyBinding:
    action: str
    label: str
    default: str


POPUP_KEY_BINDINGS: tuple[PopupKeyBinding, ...] = (
    PopupKeyBinding("navigate_up", "Navigate up", "Up"),
    PopupKeyBinding("navigate_down", "Navigate down", "Down"),
    PopupKeyBinding("navigate_page_up", "Navigate page up", "PgUp"),
    PopupKeyBinding("navigate_page_down", "Navigate page down", "PgDown"),
    PopupKeyBinding("navigate_home", "Navigate to first", "Home"),
    PopupKeyBinding("navigate_end", "Navigate to last", "End"),
    PopupKeyBinding("next_tab", "Next tab", "Ctrl+Tab"),
    PopupKeyBinding("previous_tab", "Previous tab", "Ctrl+Shift+Tab"),
    PopupKeyBinding("select_all", "Select all visible", "Ctrl+A"),
    PopupKeyBinding("hide", "Hide popup", "Esc"),
    PopupKeyBinding("properties", "Properties", "Alt+Enter"),
    PopupKeyBinding("paste", "Paste", "Return"),
    PopupKeyBinding("paste_plain", "Paste as plain text", "Shift+Return"),
    PopupKeyBinding("copy", "Copy", "Ctrl+C"),
    PopupKeyBinding("delete", "Delete", "Del"),
    PopupKeyBinding("edit_external", "Edit externally", "Ctrl+E"),
    PopupKeyBinding("view", "View", "F3"),
    PopupKeyBinding("edit", "Edit", "F2"),
    PopupKeyBinding("pin", "Pin/unpin", "Ctrl+P"),
    PopupKeyBinding("search_mode", "Cycle search mode", "Ctrl+M"),
    PopupKeyBinding("scale_up", "Increase UI scale", "Ctrl++"),
    PopupKeyBinding("scale_down", "Decrease UI scale", "Ctrl+-"),
    *(
        PopupKeyBinding(f"paste_{number}", f"Paste item {number}", f"Ctrl+{number}")
        for number in range(1, 10)
    ),
)

DEFAULT_POPUP_KEYBINDINGS = {binding.action: binding.default for binding in POPUP_KEY_BINDINGS}

# Qt distinguishes keypad Enter/Plus from Return/Equal. These aliases preserve
# the old §6 defaults only until the user explicitly rebinds an action.
DEFAULT_KEY_ALIASES = {
    "previous_tab": ("Ctrl+Backtab",),
    "properties": ("Alt+Return",),
    "paste": ("Enter",),
    "paste_plain": ("Shift+Enter",),
    "scale_up": ("Ctrl+=",),
}


def setting_key(action: str) -> str:
    return f"keys/{action}"


def active_sequences(action: str, sequence: str) -> tuple[str, ...]:
    """Sequence texts currently accepted for one action, including default aliases."""
    if sequence == DEFAULT_POPUP_KEYBINDINGS[action]:
        return (sequence, *DEFAULT_KEY_ALIASES.get(action, ()))
    return (sequence,)
