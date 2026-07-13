import uuid
from datetime import datetime

import pytest

from keeps.ui.text_transform import (
    TRANSFORMS,
    append_timestamp,
    camel_case,
    capitalize,
    invert_case,
    is_valid_json,
    new_guid,
    pretty_json,
    remove_line_feeds,
    sentence_case,
    slugify,
    to_lower,
    to_upper,
    trim_whitespace,
)

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
    assert list(TRANSFORMS.items()) == [
        ("UPPERCASE", to_upper),
        ("lowercase", to_lower),
        ("Capitalize", capitalize),
        ("Trim whitespace", trim_whitespace),
        ("Remove line feeds", remove_line_feeds),
        ("Sentence case", sentence_case),
        ("Invert case", invert_case),
        ("Paste + timestamp", append_timestamp),
        ("Slugify", slugify),
        ("New GUID", new_guid),
        ("JSON pretty-print", pretty_json),
        ("CamelCase", camel_case),
    ]


@pytest.mark.parametrize(
    "text,expected",
    [("", ""), ("a\n\nb", "a b"), ("  привет\r\n  мир  ", "привет мир")],
)
def test_remove_line_feeds(text, expected):
    assert remove_line_feeds(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [("", ""), ("hELLO WORLD", "Hello world"), ("  пРИВЕТ МИР", "  Привет мир")],
)
def test_sentence_case(text, expected):
    assert sentence_case(text) == expected


def test_invert_case_handles_cyrillic_and_empty():
    assert invert_case("Hello ПРИВЕТ") == "hELLO привет"
    assert invert_case("") == ""


def test_append_timestamp_uses_decided_end_position_and_minute_format():
    now = datetime(2026, 7, 13, 14, 5)
    assert append_timestamp("clip", now) == "clip 2026-07-13 14:05"
    assert append_timestamp("", now) == "2026-07-13 14:05"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Привет, мир!", "privet-mir"),
        ("Ёж и щука", "yozh-i-shchuka"),
        ("Hello  world", "hello-world"),
        ("", ""),
    ],
)
def test_slugify_transliterates_russian(text, expected):
    assert slugify(text) == expected


def test_new_guid_ignores_source_text():
    expected = uuid.UUID("12345678-1234-5678-1234-567812345678")
    assert new_guid("ignored", lambda: expected) == str(expected)


def test_json_pretty_print_and_validation():
    source = '{"message":"привет","items":[1,2]}'
    assert is_valid_json(source) is True
    assert pretty_json(source) == '{\n  "message": "привет",\n  "items": [\n    1,\n    2\n  ]\n}'
    assert is_valid_json("") is False
    assert pretty_json("not json") == "not json"


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_json_pretty_print_rejects_non_standard_constants(constant):
    assert is_valid_json(constant) is False
    assert pretty_json(constant) == constant


def test_json_pretty_print_rejects_unserializably_deep_input():
    source = "[" * 1100 + "0" + "]" * 1100
    assert is_valid_json(source) is False
    assert pretty_json(source) == source


@pytest.mark.parametrize(
    "text,expected",
    [
        ("hello world", "helloWorld"),
        ("Привет мир", "приветМир"),
        ("JSON_value", "jsonValue"),
        ("", ""),
    ],
)
def test_camel_case(text, expected):
    assert camel_case(text) == expected
