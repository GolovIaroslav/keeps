"""Pure display-formatting helpers for the UI layer (no Qt imports)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from keeps.store import normalize, normalize_with_mapping


@dataclass(frozen=True)
class TextStatistics:
    """Fast, Unicode-aware text counts for the View dialog."""

    words: int
    characters: int
    characters_without_whitespace: int
    lines: int
    paragraphs: int


def text_statistics(text: str) -> TextStatistics:
    """Return useful text counts without parsing or loading any language model."""
    return TextStatistics(
        words=len(re.findall(r"[^\W_]+", text, flags=re.UNICODE)),
        characters=len(text),
        characters_without_whitespace=sum(not character.isspace() for character in text),
        lines=text.count("\n") + 1 if text else 0,
        paragraphs=len([part for part in re.split(r"\n\s*\n", text.strip()) if part]),
    )


def format_byte_size(size: int) -> str:
    """Compact binary size suitable for a metadata line."""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KiB"
    return f"{size / (1024 * 1024):.1f} MiB"


def highlight_ranges(text: str, query: str) -> list[tuple[int, int]]:
    """Original-string ranges for every case-insensitive query-term occurrence."""
    folded_text, original_indexes = normalize_with_mapping(text)

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
