import pytest

from keeps.ui.text_transform import TRANSFORMS, capitalize, to_lower, to_upper, trim_whitespace

UPPER_CASES = [
    ("", ""),
    ("   ", "   "),
    ("hello world", "HELLO WORLD"),
    ("HELLO WORLD", "HELLO WORLD"),
    ("привет мир", "ПРИВЕТ МИР"),
]

LOWER_CASES = [
    ("", ""),
    ("   ", "   "),
    ("HELLO WORLD", "hello world"),
    ("hello world", "hello world"),
    ("ПРИВЕТ МИР", "привет мир"),
]

CAPITALIZE_CASES = [
    ("", ""),
    ("   ", "   "),
    ("hello world", "Hello world"),
    ("keeps API", "Keeps API"),  # rest of the string must stay untouched
    ("Hello world", "Hello world"),
    ("привет мир", "Привет мир"),
]

TRIM_CASES = [
    ("", ""),
    ("   ", ""),
    ("  hello world  ", "hello world"),
    ("\thello\n", "hello"),
    ("hello world", "hello world"),
    ("  привет мир  ", "привет мир"),
]


@pytest.mark.parametrize("text,expected", UPPER_CASES)
def test_to_upper(text, expected):
    assert to_upper(text) == expected


@pytest.mark.parametrize("text,expected", LOWER_CASES)
def test_to_lower(text, expected):
    assert to_lower(text) == expected


@pytest.mark.parametrize("text,expected", CAPITALIZE_CASES)
def test_capitalize(text, expected):
    assert capitalize(text) == expected


@pytest.mark.parametrize("text,expected", TRIM_CASES)
def test_trim_whitespace(text, expected):
    assert trim_whitespace(text) == expected


def test_transforms_registry_matches_functions():
    assert TRANSFORMS["UPPERCASE"] is to_upper
    assert TRANSFORMS["lowercase"] is to_lower
    assert TRANSFORMS["Capitalize"] is capitalize
    assert TRANSFORMS["Trim whitespace"] is trim_whitespace
