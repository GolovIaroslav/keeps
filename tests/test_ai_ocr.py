import numpy as np
import pytest

from keeps.ai.ocr import (
    _best_recognition,
    _postprocess_detection,
    _resize_for_detection,
    ctc_collapse,
    ctc_greedy_decode,
    ctc_greedy_decode_with_confidence,
    decode_indices,
    dict_path_for,
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
    chars = load_char_list(dict_path_for("eslav"))
    assert len(chars) == 519  # blank + 517-entry eslav dict + trailing space
    assert chars[0] == "<blank>"
    assert chars[-1] == " "
    assert "А" in chars  # Cyrillic present
    assert "a" in chars  # Latin present


# -- ctc_greedy_decode_with_confidence ---------------------------------------
#
# The real PP-OCRv5 recognizer ONNX output is already a per-timestep softmax
# distribution (verified live against the downloaded eslav weights: every row
# summed to ~1.0), so ctc_greedy_decode_with_confidence does NOT re-normalize
# -- test rows below are plain probabilities (each row summing to 1), fed in
# directly as "logits", matching what the real model actually produces.


def _probs(rows: list[list[float]]) -> np.ndarray:
    return np.array(rows, dtype=np.float64)


def test_ctc_confidence_clean_single_char_high_confidence():
    # One timestep, "a" (index 1) dominant at 0.996.
    logits = _probs([[0.001, 0.996, 0.001, 0.001, 0.001]])
    text, confidence = ctc_greedy_decode_with_confidence(logits, CHARS)
    assert text == "a"
    assert confidence == pytest.approx(0.996, abs=1e-9)


def test_ctc_confidence_low_confidence_garbled_sequence():
    # One timestep, "a" only narrowly beats "b"/"c" -- a garbled/uncertain pick.
    logits = _probs([[0.05, 0.30, 0.28, 0.27, 0.10]])
    text, confidence = ctc_greedy_decode_with_confidence(logits, CHARS)
    assert text == "a"
    assert confidence == pytest.approx(0.30, abs=1e-9)


def test_ctc_confidence_all_blank_sequence_is_zero_not_nan():
    logits = _probs(
        [
            [0.97, 0.01, 0.01, 0.005, 0.005],
            [0.96, 0.01, 0.01, 0.01, 0.01],
        ]
    )
    text, confidence = ctc_greedy_decode_with_confidence(logits, CHARS)
    assert text == ""
    assert confidence == 0.0


def test_ctc_confidence_duplicate_then_blank_averages_only_surviving_timesteps():
    # t0: "a" @0.90, t1: "a" @0.50 (same class as t0 -> collapsed, its own
    # lower prob must NOT be counted), t2: blank @0.90 (dropped entirely),
    # t3: "b" @0.80. If confidence wrongly averaged over all 4 raw timesteps
    # (or the duplicate's own prob) instead of just the 2 surviving picks,
    # this would not equal mean(0.90, 0.80).
    logits = _probs(
        [
            [0.02, 0.90, 0.03, 0.03, 0.02],  # a, kept (first of the "a" run)
            [0.10, 0.50, 0.20, 0.10, 0.10],  # a again, collapsed (dup)
            [0.90, 0.03, 0.03, 0.02, 0.02],  # blank, dropped
            [0.03, 0.03, 0.80, 0.07, 0.07],  # b, kept
        ]
    )
    text, confidence = ctc_greedy_decode_with_confidence(logits, CHARS)
    assert text == "ab"
    assert confidence == pytest.approx((0.90 + 0.80) / 2, abs=1e-9)


def test_ctc_confidence_text_matches_plain_decode_for_single_recognizer():
    # Regression guard for the multi-recognizer refactor: argmax (and
    # therefore decoded text) must be identical to the pre-confidence-scoring
    # ctc_greedy_decode, for arbitrary input -- only the confidence number is
    # new, the text output must not change.
    rng = np.random.default_rng(0)
    logits = rng.normal(size=(12, len(CHARS)))
    class_ids = np.argmax(logits, axis=-1).tolist()
    expected_text = ctc_greedy_decode(class_ids, CHARS)

    text, _confidence = ctc_greedy_decode_with_confidence(logits, CHARS)
    assert text == expected_text


# -- _best_recognition (multi-recognizer merge logic) ------------------------

BEST_RECOGNITION_CASES = [
    ([("hello", 0.5)], ("hello", 0.5)),  # single candidate: returned unchanged
    ([("a", 0.2), ("b", 0.9), ("c", 0.5)], ("b", 0.9)),  # highest confidence wins
    ([("x", 0.7), ("y", 0.7)], ("x", 0.7)),  # tie: first candidate wins
    ([("x", 0.9), ("y", 0.95), ("z", 0.1)], ("y", 0.95)),  # winner not first or last
]


@pytest.mark.parametrize("candidates,expected", BEST_RECOGNITION_CASES)
def test_best_recognition(candidates, expected):
    assert _best_recognition(candidates) == expected


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
