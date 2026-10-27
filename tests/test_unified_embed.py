"""Fast, dependency-light contracts for the optional EmbeddingGemma 2 encoder."""

from pathlib import Path

import numpy as np
import pytest

from keeps.ai import models
from keeps.ai.unified_embed import UnifiedEmbedder, image_patch_inputs


@pytest.mark.parametrize("height,width", [(224, 224), (768, 1366), (1600, 600), (80, 1200)])
def test_image_patch_grid_stays_within_budget(height, width):
    import cv2

    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:, :, 1] = 180
    valid, content = cv2.imencode(".png", image)
    assert valid

    patches, positions, tokens = image_patch_inputs(content.tobytes(), 70)

    assert patches.shape == (1, 630, 768)
    assert positions.shape == (1, 630, 2)
    assert 0 < tokens <= 70
    real = positions[0][(positions[0] >= 0).all(axis=-1)]
    assert len(real) == tokens * 9
    assert np.all(positions[0][len(real):] == -1)
    assert np.all(patches[0][len(real):] == 0)


def test_broken_image_rejected():
    with pytest.raises(ValueError, match="decoded"):
        image_patch_inputs(b"broken-image")


def test_no_unexpected_token_budget():
    with pytest.raises(ValueError, match="unsupported"):
        image_patch_inputs(b"", 12)


def test_query_and_document_use_different_training_prompts():
    embed = UnifiedEmbedder(Path("text"), Path("vision"), Path("tokenizer"))
    embed._encode = lambda text: text

    assert embed.encode_query("кошка") == "task: search result | query: кошка"
    assert embed.encode_text("кошка") == "title: none | text: кошка"


def test_unified_artifacts_are_shared_and_not_duplicated():
    spec = models.UNIFIED_EMBED
    paths = [models.file_dest(spec, f) for f in spec.files]
    assert len(set(paths)) == 5
    assert all(p.parent == paths[0].parent for p in paths)
    assert models.UNIFIED_TEXT_KEY != models.UNIFIED_IMAGE_KEY
    assert 300_000_000 < spec.total_size_bytes < 320_000_000
