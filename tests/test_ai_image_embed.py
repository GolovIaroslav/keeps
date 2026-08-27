import cv2
import numpy as np
import pytest

from keeps.ai.image_embed import (
    IMAGE_SIZE,
    l2_normalize,
    preprocess_image,
    visual_query_text,
)


def test_l2_normalize_returns_unit_float32_vector():
    result = l2_normalize([3.0, 4.0])

    assert result.dtype == np.float32
    assert result.tolist() == pytest.approx([0.6, 0.8])


def test_visual_query_matches_siglip2_lowercase_training_preprocessing():
    assert visual_query_text("  Лошадь Качок  ") == "лошадь качок"
    assert visual_query_text("HORSE") == visual_query_text("horse")


def test_siglip_image_preprocessing_is_rgb_chw_and_normalized():
    # OpenCV input is BGR: blue=255, green=128, red=0.
    source = np.zeros((10, 20, 3), dtype=np.uint8)
    source[:, :] = [255, 128, 0]
    ok, encoded = cv2.imencode(".png", source)
    assert ok

    result = preprocess_image(encoded.tobytes())

    assert result.shape == (1, 3, IMAGE_SIZE, IMAGE_SIZE)
    assert result.dtype == np.float32
    assert result[0, 0, 0, 0] == pytest.approx(-1.0)
    assert result[0, 1, 0, 0] == pytest.approx(128 / 127.5 - 1.0, abs=1e-6)
    assert result[0, 2, 0, 0] == pytest.approx(1.0)


def test_siglip_image_preprocessing_rejects_invalid_bytes():
    with pytest.raises(ValueError, match="could not be decoded"):
        preprocess_image(b"not an image")
