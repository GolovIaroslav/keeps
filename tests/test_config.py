import pytest

from keeps import config

PARSE_CASES = [
    ("", []),
    ("eslav", ["eslav"]),
    ("eslav,latin,ch", ["eslav", "latin", "ch"]),
    (" eslav , latin ", ["eslav", "latin"]),
    ("eslav,eslav,latin", ["eslav", "latin"]),
    (",eslav,latin,", ["eslav", "latin"]),
    ("eslav,,latin", ["eslav", "latin"]),
    (",,", []),
]


@pytest.mark.parametrize("value,expected", PARSE_CASES)
def test_parse_ocr_languages(value, expected):
    assert config.parse_ocr_languages(value) == expected


FORMAT_CASES = [
    ([], ""),
    (["eslav"], "eslav"),
    (["eslav", "latin", "ch"], "eslav,latin,ch"),
]


@pytest.mark.parametrize("codes,expected", FORMAT_CASES)
def test_format_ocr_languages(codes, expected):
    assert config.format_ocr_languages(codes) == expected


ROUND_TRIP_CASES = [
    [],
    ["eslav"],
    ["eslav", "latin", "ch"],
    ["a", "b", "c", "d", "eslav"],
]


@pytest.mark.parametrize("codes", ROUND_TRIP_CASES)
def test_round_trip(codes):
    assert config.parse_ocr_languages(config.format_ocr_languages(codes)) == codes
