import numpy as np
import pytest

from keeps.ai.ocr import (
    _postprocess_detection,
    _resize_for_detection,
    ctc_collapse,
    ctc_greedy_decode,
    decode_indices,
    load_char_list,
    order_points_clockwise,
    unclip_box,
)

CHARS = ("<blank>", "a", "b", "c", " ")  # tiny synthetic dict for decode tests

CTC_COLLAPSE_CASES = [
    ([1, 1, 0, 1, 2, 2], [1, 1, 2]),  # dedup consecutive repeats, then drop blanks
    ([0, 0, 0], []),  # all blank
    ([1, 2, 3], [1, 2, 3]),  # no repeats, nothing to collapse
    ([1, 1, 1], [1]),  # single run collapses to one
    ([], []),
    ([1, 0, 1], [1, 1]),  # a blank between two same-char runs must NOT merge them
]


@pytest.mark.parametrize("class_ids,expected", CTC_COLLAPSE_CASES)
def test_ctc_collapse(class_ids, expected):
    assert ctc_collapse(class_ids) == expected


def test_decode_indices_maps_to_chars():
    assert decode_indices([1, 2, 3], CHARS) == "abc"


def test_ctc_greedy_decode_end_to_end():
    # "aabbb_c" (blank=0) -> collapse repeats -> [1,2,3] -> drop blanks (none left) -> "abc"
    class_ids = [1, 1, 2, 2, 2, 0, 3]
    assert ctc_greedy_decode(class_ids, CHARS) == "abc"


def test_ctc_greedy_decode_with_space_token():
    # index 4 is the trailing space in CHARS
    class_ids = [1, 4, 2]
    assert ctc_greedy_decode(class_ids, CHARS) == "a b"


def test_load_char_list_shape_and_boundaries():
    chars = load_char_list()
    assert len(chars) == 519  # blank + 517-entry eslav dict + trailing space
    assert chars[0] == "<blank>"
    assert chars[-1] == " "
    assert "А" in chars  # Cyrillic present
    assert "a" in chars  # Latin present


def test_resize_for_detection_keeps_aspect_and_multiple_of_32():
    new_h, new_w = _resize_for_detection(1080, 1920)
    assert new_h % 32 == 0
    assert new_w % 32 == 0
    assert max(new_h, new_w) <= 960
    # aspect ratio roughly preserved
    assert abs((new_w / new_h) - (1920 / 1080)) < 0.05


def test_resize_for_detection_never_goes_below_32():
    new_h, new_w = _resize_for_detection(10, 5)
    assert new_h >= 32
    assert new_w >= 32


def test_unclip_box_expands_area():
    square = np.array([[0, 0], [10, 0], [10, 10], [0, 10]], dtype=np.float32)
    expanded = unclip_box(square, ratio=1.5)

    import cv2

    original_area = cv2.contourArea(square)
    expanded_area = cv2.contourArea(expanded.astype(np.float32))
    assert expanded_area > original_area


def test_unclip_box_is_deterministic():
    square = np.array([[0, 0], [10, 0], [10, 10], [0, 10]], dtype=np.float32)
    first = unclip_box(square, ratio=1.5)
    second = unclip_box(square, ratio=1.5)
    np.testing.assert_array_equal(first, second)


def test_unclip_box_larger_ratio_expands_more():
    square = np.array([[0, 0], [10, 0], [10, 10], [0, 10]], dtype=np.float32)
    small = unclip_box(square, ratio=1.1)
    large = unclip_box(square, ratio=2.0)

    import cv2

    assert cv2.contourArea(large.astype(np.float32)) > cv2.contourArea(small.astype(np.float32))


def test_postprocess_detection_finds_synthetic_text_blob():
    # A 64x64 probability map with one confident rectangular blob standing in
    # for a text line -- no real image or model involved.
    prob_map = np.zeros((1, 1, 64, 64), dtype=np.float32)
    prob_map[0, 0, 20:30, 10:50] = 0.9  # a wide, short rectangle (line-shaped)

    boxes = _postprocess_detection(prob_map, orig_shape=(64, 64), resized_shape=(64, 64))

    assert len(boxes) == 1
    box = boxes[0]
    xs, ys = box[:, 0], box[:, 1]
    # unclip expands it, so just check it's roughly centered where the blob is
    assert 0 <= xs.min() < 15
    assert 45 < xs.max() <= 64
    assert 10 <= ys.min() < 25
    assert 25 < ys.max() <= 40


def test_postprocess_detection_ignores_tiny_noise_below_min_area():
    prob_map = np.zeros((1, 1, 64, 64), dtype=np.float32)
    prob_map[0, 0, 5:8, 5:8] = 0.9  # 3x3=9px, above DET_THRESH but below DET_MIN_BOX_AREA=16
    boxes = _postprocess_detection(prob_map, orig_shape=(64, 64), resized_shape=(64, 64))
    assert boxes == []


# Regression test for a real bug: cv2.boxPoints(rect) returns the 4 corners
# starting from whichever one happens to be first for that rectangle's
# rotation angle, not always top-left. Feeding an unordered quad into
# cv2.getPerspectiveTransform's dst=[top-left, top-right, bottom-right,
# bottom-left] mapping silently rotated/mirrored every recognition crop --
# caught by a live OCR smoke test on a synthetic image (garbage output),
# not by the unit tests, because none of them exercised a box whose
# starting corner wasn't already top-left.
ORDER_POINTS_CASES = [
    # (unordered input corners, expected [TL, TR, BR, BL] order)
    (
        [(0, 0), (10, 0), (10, 10), (0, 10)],  # already TL,TR,BR,BL
        [(0, 0), (10, 0), (10, 10), (0, 10)],
    ),
    (
        [(10, 10), (0, 10), (0, 0), (10, 0)],  # starts at BR (rotated cv2.boxPoints order)
        [(0, 0), (10, 0), (10, 10), (0, 10)],
    ),
    (
        [(0, 10), (0, 0), (10, 0), (10, 10)],  # starts at BL
        [(0, 0), (10, 0), (10, 10), (0, 10)],
    ),
    (
        [(10, 0), (10, 10), (0, 10), (0, 0)],  # starts at TR
        [(0, 0), (10, 0), (10, 10), (0, 10)],
    ),
]


@pytest.mark.parametrize("scrambled,expected", ORDER_POINTS_CASES)
def test_order_points_clockwise_normalizes_any_starting_corner(scrambled, expected):
    ordered = order_points_clockwise(scrambled)
    np.testing.assert_array_equal(ordered, np.array(expected, dtype=np.float32))
