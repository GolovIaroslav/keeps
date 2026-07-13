import pytest

from keeps.ui.color_swatch import parse_color


@pytest.mark.parametrize(
    "text,expected",
    [
        ("#ff6b2b", (255, 107, 43)),
        ("#F0a", (255, 0, 170)),
        ("rgb(255, 107, 43)", (255, 107, 43)),
        ("rgb(100%, 0%, 50%)", (255, 0, 128)),
        ("hsl(0, 100%, 50%)", (255, 0, 0)),
        ("hsl(120, 100%, 25%)", (0, 128, 0)),
    ],
)
def test_parse_color(text, expected):
    assert parse_color(text) == expected


@pytest.mark.parametrize("text", ["", "#12", "rgb(300,0,0)", "hsl(0,20,30)", "hello"])
def test_parse_color_rejects_non_colors(text):
    assert parse_color(text) is None
