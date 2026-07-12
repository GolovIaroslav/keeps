"""Pure display-formatting helpers for the UI layer (no Qt imports)."""

from __future__ import annotations

from datetime import datetime

from keeps.store import normalize


def highlight_ranges(text: str, query: str) -> list[tuple[int, int]]:
    """Original-string ranges for every case-insensitive query-term occurrence."""
    folded_parts = []
    original_indexes = []
    for index, character in enumerate(text):
        folded = normalize(character)
        folded_parts.append(folded)
        original_indexes.extend([index] * len(folded))
    folded_text = "".join(folded_parts)

    ranges = set()
    for raw_term in query.split():
        term = normalize(raw_term)
        start = 0
        while term and (position := folded_text.find(term, start)) >= 0:
            original_start = original_indexes[position]
            original_end = original_indexes[position + len(term) - 1] + 1
            ranges.add((original_start, original_end - original_start))
            start = position + 1
    return sorted(ranges)


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
