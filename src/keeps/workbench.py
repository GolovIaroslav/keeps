"""Pure state and composition helpers for the Clipboard Workbench (F26)."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

TextTransform = Callable[[str], str]


@dataclass(frozen=True)
class WorkbenchItem:
    """One history clip staged in the workbench."""

    clip_id: int
    kind: str
    mime_data: dict[str, bytes]
    transform: str | None = None


@dataclass(frozen=True)
class WorkbenchResult:
    """The clipboard payload produced by the current workbench contents."""

    kind: str
    mime_data: dict[str, bytes]
    included_ids: tuple[int, ...]
    skipped_ids: tuple[int, ...]
    plain_only: bool


def move_item(
    items: Sequence[WorkbenchItem], index: int, direction: int
) -> tuple[WorkbenchItem, ...]:
    """Move one item by one position, clamping at either end."""
    if not 0 <= index < len(items):
        raise IndexError("workbench item index out of range")
    if direction not in (-1, 1):
        raise ValueError("workbench direction must be -1 or 1")
    result = list(items)
    target = max(0, min(len(result) - 1, index + direction))
    result[index], result[target] = result[target], result[index]
    return tuple(result)


def remove_item(items: Sequence[WorkbenchItem], index: int) -> tuple[WorkbenchItem, ...]:
    """Return the staged items without one entry."""
    if not 0 <= index < len(items):
        raise IndexError("workbench item index out of range")
    return tuple(item for position, item in enumerate(items) if position != index)


def set_transform(
    items: Sequence[WorkbenchItem], index: int, transform: str | None
) -> tuple[WorkbenchItem, ...]:
    """Set or clear the named transform for one staged item."""
    if not 0 <= index < len(items):
        raise IndexError("workbench item index out of range")
    if transform and "text/plain" not in items[index].mime_data:
        raise ValueError("only clips with plain text can be transformed")
    result = list(items)
    result[index] = replace(result[index], transform=transform)
    return tuple(result)


def effective_mime_data(
    item: WorkbenchItem, transforms: Mapping[str, TextTransform]
) -> dict[str, bytes]:
    """Materialize one item, applying its transform without mutating history."""
    if item.transform is None:
        return dict(item.mime_data)
    transform = transforms.get(item.transform)
    if transform is None:
        raise ValueError(f"unknown workbench transform: {item.transform}")
    plain = item.mime_data.get("text/plain")
    if plain is None:
        raise ValueError("a transformed workbench item must have plain text")
    return {"text/plain": transform(plain.decode("utf-8", errors="replace")).encode("utf-8")}


def compose(
    items: Sequence[WorkbenchItem],
    separator: str,
    transforms: Mapping[str, TextTransform],
) -> WorkbenchResult | None:
    """Build the payload for one workbench paste/save action.

    A lone untouched clip keeps all of its MIME formats. Any composition,
    including a transformed lone clip, deliberately produces plain text: it
    is the only representation whose concatenation is unambiguous. Items
    without plain text are skipped when composing multiple entries.
    """
    if not items:
        return None

    materialized = [effective_mime_data(item, transforms) for item in items]
    if len(items) == 1 and items[0].transform is None:
        return WorkbenchResult(
            items[0].kind,
            materialized[0],
            (items[0].clip_id,),
            (),
            False,
        )

    included: list[tuple[int, str]] = []
    skipped: list[int] = []
    for item, mime_data in zip(items, materialized, strict=True):
        plain = mime_data.get("text/plain")
        if plain is None:
            skipped.append(item.clip_id)
            continue
        included.append((item.clip_id, plain.decode("utf-8", errors="replace")))
    if not included:
        return None
    return WorkbenchResult(
        "text",
        {"text/plain": separator.join(text for _clip_id, text in included).encode("utf-8")},
        tuple(clip_id for clip_id, _text in included),
        tuple(skipped),
        True,
    )
