import pytest

from keeps.ui.format import format_byte_size, highlight_ranges, relative_time, text_statistics

NOW = 1_000_000_000_000  # arbitrary fixed "now" in unix ms

CASES = [
    (NOW, "just now"),
    (NOW - 3_000, "just now"),
    (NOW - 30_000, "30s ago"),
    (NOW - 90_000, "1m ago"),
    (NOW - 3_600_000, "1h ago"),
    (NOW - 7_200_000, "2h ago"),
    (NOW - 90_000_000, "1d ago"),
]


@pytest.mark.parametrize("timestamp_ms,expected", CASES)
def test_relative_time(timestamp_ms, expected):
    assert relative_time(timestamp_ms, NOW) == expected


def test_relative_time_far_past_is_a_date():
    eight_days_ms = 8 * 86400 * 1000
    result = relative_time(NOW - eight_days_ms, NOW)
    assert result.count("-") == 2  # YYYY-MM-DD, not a relative phrase


@pytest.mark.parametrize(
    ("text", "query", "expected"),
    [
        ("alpha middle beta", "BETA alpha", [(0, 5), (13, 4)]),
        ("Привет, мир", "МИР", [(8, 3)]),
        ("banana", "ana", [(1, 3), (3, 3)]),
        ("no match", "missing", []),
        ("anything", "   ", []),
    ],
)
def test_highlight_ranges(text, query, expected):
    assert highlight_ranges(text, query) == expected


def test_text_statistics_counts_unicode_words_and_visible_structure():
    stats = text_statistics("Привет, world!\n\nЕщё строка.")

    assert stats.words == 4
    assert stats.characters == 27
    assert stats.characters_without_whitespace == 23
    assert stats.lines == 3
    assert stats.paragraphs == 2


def test_text_statistics_treats_empty_text_as_no_lines_or_paragraphs():
    assert text_statistics("").lines == 0
    assert text_statistics("").paragraphs == 0


@pytest.mark.parametrize(
    ("size", "expected"),
    [(0, "0 B"), (999, "999 B"), (1024, "1.0 KiB"), (2 * 1024 * 1024, "2.0 MiB")],
)
def test_format_byte_size(size, expected):
    assert format_byte_size(size) == expected
