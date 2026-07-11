import pytest

from keeps.ui.geometry import next_ui_scale, resize_edges

SCALE_CASES = [
    (1.0, 1, 1.1),
    (1.0, -1, 0.9),
    (2.0, 1, 2.0),  # clamped at max
    (0.7, -1, 0.7),  # clamped at min
    (1.95, 1, 2.0),  # step would overshoot max, clamps instead
]


@pytest.mark.parametrize("current,direction,expected", SCALE_CASES)
def test_next_ui_scale(current, direction, expected):
    assert next_ui_scale(current, direction) == expected


EDGE_CASES = [
    (0, 0, 400, 300, frozenset({"top", "left"})),
    (399, 0, 400, 300, frozenset({"top", "right"})),
    (0, 299, 400, 300, frozenset({"bottom", "left"})),
    (399, 299, 400, 300, frozenset({"bottom", "right"})),
    (3, 150, 400, 300, frozenset({"left"})),
    (200, 0, 400, 300, frozenset({"top"})),
    (200, 150, 400, 300, frozenset()),  # dead center, no edge
]


@pytest.mark.parametrize("x,y,width,height,expected", EDGE_CASES)
def test_resize_edges(x, y, width, height, expected):
    assert resize_edges(x, y, width, height) == expected
