"""Pure window-geometry helpers for the UI layer (no Qt imports)."""

from __future__ import annotations

UI_SCALE_MIN = 0.7
UI_SCALE_MAX = 2.0
UI_SCALE_STEP = 0.1

RESIZE_MARGIN = 6


def next_ui_scale(current: float, direction: int) -> float:
    """Clamped next UI scale step; direction > 0 zooms in, < 0 zooms out."""
    step = UI_SCALE_STEP if direction > 0 else -UI_SCALE_STEP
    return round(min(UI_SCALE_MAX, max(UI_SCALE_MIN, current + step)), 2)


def resize_edges(
    x: int, y: int, width: int, height: int, margin: int = RESIZE_MARGIN
) -> frozenset[str]:
    """Which window edge(s) a point near the border belongs to, for drag-resize."""
    edges = set()
    if x <= margin:
        edges.add("left")
    elif x >= width - margin:
        edges.add("right")
    if y <= margin:
        edges.add("top")
    elif y >= height - margin:
        edges.add("bottom")
    return frozenset(edges)
