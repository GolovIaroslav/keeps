from keeps.ai.ranking import KEYWORD_RRF_WEIGHT, RRF_K, SearchMode, blend, reciprocal_rank_fusion
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


def test_blended_mode_boosts_items_supported_by_both_search_methods():
    substring = [_clip(2), _clip(1)]  # recent id 2, but id 1 is also semantic rank 1
    scores = {1: 0.9, 3: 0.8, 4: 0.2}
    clips_by_id = {1: _clip(1), 2: _clip(2), 3: _clip(3), 4: _clip(4)}

    result = blend(substring, scores, clips_by_id, mode=SearchMode.BLENDED, threshold=0.35)

    assert [c.id for c in result] == [1, 2, 3], "overlapping signals should raise id 1"


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
        ],
        thresholds=[0.35, 0.20],
    )

    assert fused[1] == fused[3]
    assert fused[2] == fused[4]


def test_rank_fusion_boosts_an_image_found_by_visual_and_ocr_embeddings():
    fused = reciprocal_rank_fusion(
        [
            {1: 0.9, 2: 0.8},
            {3: 0.3, 2: 0.2},
        ],
        thresholds=[0.35, 0.15],
    )

    assert fused[2] > fused[1]
    assert fused[2] > fused[3]


def test_rank_fusion_rejects_best_available_scores_below_native_threshold():
    assert reciprocal_rank_fusion(
        [{1: -0.4, 2: -0.8}], thresholds=[0.35]
    ) == {}


def test_rank_fusion_does_not_truncate_before_later_content_filtering():
    scores = {clip_id: 1.0 - clip_id / 1000 for clip_id in range(1, 26)}

    fused = reciprocal_rank_fusion([scores], thresholds=[0.0])

    assert 25 in fused


def test_blend_applies_allowed_clip_set_before_top_n_cutoff():
    # Simulates the popup Text filter: the first 20 semantic ids were images
    # and therefore are absent from clips_by_id; text id 21 must still surface.
    scores = {clip_id: 1.0 - clip_id / 1000 for clip_id in range(1, 22)}
    clips_by_id = {21: _clip(21)}

    result = blend(
        [], scores, clips_by_id, mode=SearchMode.SEMANTIC, threshold=0.0, top_n=20
    )

    assert [clip.id for clip in result] == [21]


def test_hybrid_rrf_surfaces_semantic_extras_among_many_keyword_hits():
    keyword = [_clip(i) for i in range(1, 101)]
    semantic_scores = {101 + i: 1.0 - i * 0.01 for i in range(20)}
    clips_by_id = {i: _clip(i) for i in semantic_scores}

    results = blend(keyword, semantic_scores, clips_by_id, mode=SearchMode.BLENDED)

    assert results[0].id == 1
    assert any(c.id > 100 for c in results[:20])
    assert len(results) == 120
    assert len({c.id for c in results}) == len(results)


def test_hybrid_rrf_promotes_distant_keyword_hit_with_strong_semantic_match():
    keyword = [_clip(i) for i in range(1, 101)]
    semantic_scores = {100: 0.99, 101: 0.98}
    clips_by_id = {i: _clip(i) for i in semantic_scores}

    results = blend(keyword, semantic_scores, clips_by_id, mode=SearchMode.BLENDED)

    assert results[0].id == 100
    assert results[1].id == 1
    assert 101 in [c.id for c in results[:20]]


def test_hybrid_without_semantic_results_preserves_keyword_order():
    keyword = [_clip(i) for i in (13, 4, 28)]
    assert blend(keyword, {}, {}, mode=SearchMode.BLENDED) == keyword

def test_bounded_hybrid_rrf_matches_full_reference_ranking():
    keyword = [_clip(i) for i in range(1400, 0, -1)]
    semantic_ids = [1, 350, 999, 1400, *range(1401, 1417)]
    semantic_scores = {
        clip_id: 0.99 - rank * 0.01
        for rank, clip_id in enumerate(semantic_ids)
    }
    clips_by_id = {clip_id: _clip(clip_id) for clip_id in semantic_ids}
    actual = blend(keyword, semantic_scores, clips_by_id, mode=SearchMode.BLENDED)

    literal_rank = {clip.id: i for i, clip in enumerate(keyword, start=1)}
    meaning_rank = {clip_id: i for i, clip_id in enumerate(semantic_ids, start=1)}
    candidates = {clip.id: clip for clip in keyword}
    candidates.update(clips_by_id)
    expected = sorted(
        candidates,
        key=lambda clip_id: (
            -(
                (KEYWORD_RRF_WEIGHT / (RRF_K + literal_rank[clip_id])
                 if clip_id in literal_rank else 0)
                + (1 / (RRF_K + meaning_rank[clip_id])
                   if clip_id in meaning_rank else 0)
            ),
            literal_rank.get(clip_id, float("inf")),
            meaning_rank.get(clip_id, float("inf")),
            -candidates[clip_id].last_used_at,
            -clip_id,
        ),
    )
    assert [clip.id for clip in actual] == expected
