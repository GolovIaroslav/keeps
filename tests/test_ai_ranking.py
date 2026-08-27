from keeps.ai.ranking import SearchMode, blend, reciprocal_rank_fusion
from keeps.store import Clip


def _clip(clip_id: int, preview: str = "") -> Clip:
    return Clip(
        id=clip_id,
        created_at=0,
        last_used_at=0,
        kind="text",
        preview=preview,
        hash=str(clip_id),
        pinned=False,
        use_count=0,
        ocr_text=None,
    )


def test_keyword_mode_ignores_semantic_scores_entirely():
    substring = [_clip(1), _clip(2)]
    scores = {3: 0.99, 4: 0.9}
    clips_by_id = {1: substring[0], 2: substring[1], 3: _clip(3), 4: _clip(4)}

    result = blend(substring, scores, clips_by_id, mode=SearchMode.KEYWORD)

    assert [c.id for c in result] == [1, 2]


def test_semantic_mode_ranks_by_score_desc_ignoring_substring():
    substring = [_clip(1)]  # a substring hit that scores below threshold
    scores = {1: 0.1, 2: 0.9, 3: 0.5}
    clips_by_id = {1: _clip(1), 2: _clip(2), 3: _clip(3)}

    result = blend(substring, scores, clips_by_id, mode=SearchMode.SEMANTIC, threshold=0.35)

    assert [c.id for c in result] == [2, 3], "clip 1 is below threshold despite substring match"


def test_blended_mode_puts_substring_first_then_semantic_extras():
    substring = [_clip(2), _clip(1)]  # deliberately not id-sorted
    scores = {1: 0.9, 3: 0.8, 4: 0.2}
    clips_by_id = {1: _clip(1), 2: _clip(2), 3: _clip(3), 4: _clip(4)}

    result = blend(substring, scores, clips_by_id, mode=SearchMode.BLENDED, threshold=0.35)

    assert [c.id for c in result] == [2, 1, 3], "id 1 already in substring, id 4 below threshold"


def test_blended_mode_respects_top_n_cap():
    substring: list[Clip] = []
    scores = {i: 1.0 - i * 0.01 for i in range(10)}
    clips_by_id = {i: _clip(i) for i in range(10)}

    result = blend(substring, scores, clips_by_id, mode=SearchMode.BLENDED, threshold=0.0, top_n=3)

    assert [c.id for c in result] == [0, 1, 2]


def test_blend_is_deterministic_across_repeated_calls():
    substring = [_clip(1)]
    scores = {2: 0.7, 3: 0.7, 4: 0.6}  # tie between 2 and 3
    clips_by_id = {1: _clip(1), 2: _clip(2), 3: _clip(3), 4: _clip(4)}

    first = [c.id for c in blend(substring, scores, clips_by_id, mode=SearchMode.BLENDED)]
    second = [c.id for c in blend(substring, scores, clips_by_id, mode=SearchMode.BLENDED)]

    assert first == second


def test_missing_clip_lookup_is_skipped_not_crashed():
    # A semantic score for a clip_id that no longer exists (e.g. deleted
    # between embedding time and search time) must be dropped, not raise.
    substring: list[Clip] = []
    scores = {99: 0.9}
    clips_by_id: dict[int, Clip] = {}

    result = blend(substring, scores, clips_by_id, mode=SearchMode.BLENDED)

    assert result == []


MODE_CYCLE_CASES = [
    (SearchMode.KEYWORD, SearchMode.BLENDED),
    (SearchMode.BLENDED, SearchMode.SEMANTIC),
    (SearchMode.SEMANTIC, SearchMode.KEYWORD),
]


def test_mode_next_cycles_in_fixed_order():
    for current, expected in MODE_CYCLE_CASES:
        assert current.next() == expected


def test_rank_fusion_does_not_compare_raw_scores_from_different_models():
    fused = reciprocal_rank_fusion(
        [
            {1: 0.91, 2: 0.80},
            {3: 0.22, 4: 0.21},
        ]
    )

    assert fused[1] == fused[3]
    assert fused[2] == fused[4]


def test_rank_fusion_boosts_an_image_found_by_visual_and_ocr_embeddings():
    fused = reciprocal_rank_fusion(
        [
            {1: 0.9, 2: 0.8},
            {3: 0.3, 2: 0.2},
        ]
    )

    assert fused[2] > fused[1]
    assert fused[2] > fused[3]
