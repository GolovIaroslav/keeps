import pytest

from keeps.hotkey.base import parse_key_sequence

CASES = [
    ("Ctrl+`", (1 << 2, "grave")),
    ("Ctrl+Shift+V", ((1 << 2) | (1 << 0), "V")),
    ("Alt+F1", (1 << 3, "F1")),
    ("Meta+Space", (1 << 6, "space")),
    ("Ctrl+Alt+Delete", ((1 << 2) | (1 << 3), "Delete")),
]


@pytest.mark.parametrize("text, expected", CASES)
def test_parse_key_sequence(text, expected):
    assert parse_key_sequence(text) == expected


def test_parse_key_sequence_rejects_empty():
    with pytest.raises(ValueError):
        parse_key_sequence("")


def test_parse_key_sequence_rejects_unknown_modifier():
    with pytest.raises(KeyError):
        parse_key_sequence("Hyper+V")
