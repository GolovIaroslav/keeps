import pytest

from keeps.hotkey.portal_format import xdg_trigger


@pytest.mark.parametrize(
    ("sequence", "expected"),
    [
        ("Ctrl+`", "CTRL+grave"),
        ("Ctrl+Shift+V", "CTRL+SHIFT+v"),
        ("Alt+Return", "ALT+Return"),
        ("Meta+Space", "LOGO+space"),
    ],
)
def test_xdg_trigger_uses_xkb_key_names(sequence, expected):
    assert xdg_trigger(sequence) == expected


def test_xdg_trigger_rejects_empty_sequence():
    with pytest.raises(ValueError):
        xdg_trigger("+")


def test_xdg_trigger_rejects_unknown_modifier():
    with pytest.raises(KeyError):
        xdg_trigger("Hyper+V")
