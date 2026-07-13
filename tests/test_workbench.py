import pytest

from keeps.workbench import (
    WorkbenchItem,
    compose,
    effective_mime_data,
    move_item,
    remove_item,
    set_transform,
)

TRANSFORMS = {"upper": str.upper, "trim": str.strip}


def item(clip_id: int, text: str, *, transform: str | None = None) -> WorkbenchItem:
    return WorkbenchItem(
        clip_id,
        "text",
        {"text/plain": text.encode("utf-8")},
        transform=transform,
    )


def test_workbench_reorders_removes_and_sets_transforms_without_mutating_items():
    original = (item(1, "one"), item(2, "two"), item(3, "three"))

    reordered = move_item(original, 1, 1)
    transformed = set_transform(reordered, 0, "upper")

    assert [entry.clip_id for entry in transformed] == [1, 3, 2]
    assert transformed[0].transform == "upper"
    assert original[1].transform is None
    assert [entry.clip_id for entry in remove_item(transformed, 1)] == [1, 2]


def test_move_item_clamps_at_edges_and_rejects_invalid_direction():
    entries = (item(1, "one"), item(2, "two"))

    assert move_item(entries, 0, -1) == entries
    assert move_item(entries, 1, 1) == entries
    with pytest.raises(ValueError):
        move_item(entries, 0, 2)


def test_compose_applies_per_item_transform_and_reports_non_text_skips():
    entries = (
        item(1, " first ", transform="trim"),
        WorkbenchItem(2, "image", {"image/png": b"png"}),
        item(3, "third", transform="upper"),
    )

    result = compose(entries, " | ", TRANSFORMS)

    assert result is not None
    assert result.kind == "text"
    assert result.mime_data == {"text/plain": b"first | THIRD"}
    assert result.included_ids == (1, 3)
    assert result.skipped_ids == (2,)
    assert result.plain_only is True


def test_single_untouched_clip_keeps_all_mime_formats():
    entry = WorkbenchItem(
        7,
        "html",
        {"text/plain": b"hello", "text/html": b"<b>hello</b>", "x-source": b"raw"},
    )

    result = compose((entry,), "\n", TRANSFORMS)

    assert result is not None
    assert result.kind == "html"
    assert result.mime_data == entry.mime_data
    assert result.plain_only is False


def test_transformed_single_clip_becomes_plain_only():
    entry = set_transform((item(1, "hello"),), 0, "upper")[0]

    assert effective_mime_data(entry, TRANSFORMS) == {"text/plain": b"HELLO"}
    result = compose((entry,), "\n", TRANSFORMS)
    assert result is not None
    assert result.mime_data == {"text/plain": b"HELLO"}
    assert result.plain_only is True


def test_transforming_non_text_clip_is_rejected():
    entry = WorkbenchItem(1, "image", {"image/png": b"png"})

    with pytest.raises(ValueError):
        set_transform((entry,), 0, "upper")
