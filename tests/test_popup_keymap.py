from types import SimpleNamespace

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent

from keeps.popup_keymap import (
    DEFAULT_POPUP_KEYBINDINGS,
    POPUP_KEY_BINDINGS,
    active_sequences,
    setting_key,
)
from keeps.ui.popup import PopupWindow, _local_hotkey_error, _PasteInjectionTask


def test_popup_keymap_has_one_unique_persistent_key_per_action():
    actions = [binding.action for binding in POPUP_KEY_BINDINGS]

    assert len(actions) == len(set(actions))
    assert set(DEFAULT_POPUP_KEYBINDINGS) == set(actions)
    assert all(setting_key(action).startswith("keys/") for action in actions)


def test_popup_keymap_keeps_the_normative_defaults_for_delete_and_paste():
    assert DEFAULT_POPUP_KEYBINDINGS["delete"] == "Del"
    assert DEFAULT_POPUP_KEYBINDINGS["paste"] == "Return"
    assert DEFAULT_POPUP_KEYBINDINGS["paste_plain"] == "Shift+Return"


def test_popup_matches_the_configured_key_instead_of_a_hardcoded_key():
    popup = SimpleNamespace(_keybinding_text=lambda _action: "Ctrl+K")
    event = QKeyEvent(
        QEvent.Type.KeyPress,
        Qt.Key.Key_K,
        Qt.KeyboardModifier.ControlModifier,
    )

    assert PopupWindow._matches_keybinding(popup, event, "delete")


def test_popup_does_not_treat_the_first_stroke_of_a_chord_as_a_shortcut():
    popup = SimpleNamespace(_keybinding_text=lambda _action: "Ctrl+K, Ctrl+X")
    event = QKeyEvent(
        QEvent.Type.KeyPress,
        Qt.Key.Key_K,
        Qt.KeyboardModifier.ControlModifier,
    )

    assert not PopupWindow._matches_keybinding(popup, event, "delete")


def test_default_aliases_are_reserved_from_local_clip_hotkeys():
    popup = SimpleNamespace(
        _keybinding_text=lambda action: DEFAULT_POPUP_KEYBINDINGS[action]
    )

    reserved = PopupWindow._reserved_popup_hotkeys(popup)
    assert _local_hotkey_error("Alt+Return", reserved) == "reserved"


def test_default_aliases_are_part_of_conflict_detection_but_not_after_rebinding():
    assert active_sequences("paste", "Return") == ("Return", "Enter")
    assert active_sequences("paste", "Ctrl+K") == ("Ctrl+K",)


def test_paste_worker_notifies_persistent_popup_completion(monkeypatch):
    completed = []
    completion = SimpleNamespace(finished=SimpleNamespace(emit=lambda: completed.append(True)))
    monkeypatch.setattr("keeps.ui.popup.paste.inject_paste", lambda *_args: True)

    _PasteInjectionTask("wayland", "ctrl+v", completion).run()

    assert completed == [True]
