"""Pure display-formatting helpers for the UI layer (no Qt imports)."""

from __future__ import annotations

from datetime import datetime


def relative_time(timestamp_ms: int, now_ms: int) -> str:
    """Human-readable age, e.g. 'just now', '5m ago', '2h ago', or a date."""
    delta_s = max(0, (now_ms - timestamp_ms) // 1000)
    if delta_s < 5:
        return "just now"
    if delta_s < 60:
        return f"{delta_s}s ago"
    if delta_s < 3600:
        return f"{delta_s // 60}m ago"
    if delta_s < 86400:
        return f"{delta_s // 3600}h ago"
    if delta_s < 7 * 86400:
        return f"{delta_s // 86400}d ago"
    return datetime.fromtimestamp(timestamp_ms / 1000).strftime("%Y-%m-%d")
