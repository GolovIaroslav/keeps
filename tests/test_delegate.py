import pytest

from keeps.ui.delegate import relative_time

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
