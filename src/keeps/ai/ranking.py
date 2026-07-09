"""Blend substring search results with semantic (cosine) scores (PLAN.md §9).

Pure logic, no onnxruntime/Qt: callers hand in already-computed cosine scores.
This is the module that determinism/ranking tests target directly, with fake
score dictionaries standing in for a real embedder.
"""

from __future__ import annotations

from enum import Enum

from keeps.store import Clip

DEFAULT_THRESHOLD = 0.35
DEFAULT_TOP_N = 20


class SearchMode(Enum):
    BLENDED = "blended"
    KEYWORD = "keyword"
    SEMANTIC = "semantic"

    def next(self) -> SearchMode:
        """Cycle order for the Ctrl+M toggle in the popup."""
        order = [SearchMode.BLENDED, SearchMode.KEYWORD, SearchMode.SEMANTIC]
        return order[(order.index(self) + 1) % len(order)]


def _semantic_candidates(
    semantic_scores: dict[int, float],
    clips_by_id: dict[int, Clip],
    threshold: float,
    top_n: int,
) -> list[Clip]:
    ranked_ids = sorted(
        (clip_id for clip_id, score in semantic_scores.items() if score >= threshold),
        key=lambda clip_id: semantic_scores[clip_id],
        reverse=True,
    )
    return [clips_by_id[clip_id] for clip_id in ranked_ids[:top_n] if clip_id in clips_by_id]


def blend(
    substring_clips: list[Clip],
    semantic_scores: dict[int, float],
    clips_by_id: dict[int, Clip],
    mode: SearchMode = SearchMode.BLENDED,
    threshold: float = DEFAULT_THRESHOLD,
    top_n: int = DEFAULT_TOP_N,
) -> list[Clip]:
    """Combine exact substring hits with semantic hits per the active mode.

    - KEYWORD: substring results only, untouched order (RAG never consulted).
    - SEMANTIC: pure cosine ranking (score >= threshold), substring match
      plays no role -- an item with no substring match can still surface.
    - BLENDED (default): substring hits first in their existing order, then
      semantic hits (score >= threshold, highest first) not already present.
    """
    if mode == SearchMode.KEYWORD:
        return list(substring_clips)

    semantic_ranked = _semantic_candidates(semantic_scores, clips_by_id, threshold, top_n)

    if mode == SearchMode.SEMANTIC:
        return semantic_ranked

    seen_ids = {clip.id for clip in substring_clips}
    extra = [clip for clip in semantic_ranked if clip.id not in seen_ids]
    return list(substring_clips) + extra
