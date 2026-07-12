"""Pure multi-selection paste composition (PLAN.md Ф13)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CombinedPaste:
    text: str
    clip_ids: tuple[int, ...]
    skipped_count: int


def combine_plain_text(
    selected: list[tuple[int, dict[str, bytes]]],
    separator: str,
    reverse: bool = False,
) -> CombinedPaste:
    ordered = reversed(selected) if reverse else selected
    included = [
        (clip_id, mime_data["text/plain"].decode("utf-8", errors="replace"))
        for clip_id, mime_data in ordered
        if "text/plain" in mime_data
    ]
    return CombinedPaste(
        text=separator.join(text for _clip_id, text in included),
        clip_ids=tuple(clip_id for clip_id, _text in included),
        skipped_count=len(selected) - len(included),
    )


def separator_to_display(separator: str) -> str:
    return separator.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t")


def separator_from_display(display: str) -> str:
    result = []
    index = 0
    while index < len(display):
        if display[index] == "\\" and index + 1 < len(display):
            escaped = display[index + 1]
            if escaped == "n":
                result.append("\n")
                index += 2
                continue
            if escaped == "t":
                result.append("\t")
                index += 2
                continue
            if escaped == "\\":
                result.append("\\")
                index += 2
                continue
        result.append(display[index])
        index += 1
    return "".join(result)
