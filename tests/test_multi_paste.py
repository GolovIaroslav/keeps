import pytest

from keeps.multi_paste import (
    combine_plain_text,
    separator_from_display,
    separator_to_display,
)


def test_combines_three_text_clips_in_visible_order():
    selected = [
        (1, {"text/plain": b"first"}),
        (2, {"text/plain": b"second"}),
        (3, {"text/plain": b"third"}),
    ]

    result = combine_plain_text(selected, "\n---\n")

    assert result.text == "first\n---\nsecond\n---\nthird"
    assert result.clip_ids == (1, 2, 3)
    assert result.skipped_count == 0


def test_reverse_order_is_explicit_option():
    selected = [
        (1, {"text/plain": b"first"}),
        (2, {"text/plain": b"second"}),
    ]

    result = combine_plain_text(selected, " | ", reverse=True)

    assert result.text == "second | first"
    assert result.clip_ids == (2, 1)


def test_mixed_selection_skips_items_without_plain_text():
    selected = [
        (1, {"text/plain": b"kept"}),
        (2, {"image/png": b"png"}),
    ]

    result = combine_plain_text(selected, "\n")

    assert result.text == "kept"
    assert result.clip_ids == (1,)
    assert result.skipped_count == 1


@pytest.mark.parametrize("separator", ["\n", "\t", "\\", "\n---\t\\"])
def test_separator_display_roundtrip(separator):
    assert separator_from_display(separator_to_display(separator)) == separator
