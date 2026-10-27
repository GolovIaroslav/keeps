"""EmbeddingGemma 2 Q4 text/vision embeddings using ONNX Runtime.

The unified 768-d space shares a quantized text backbone across both
modalities. No audio encoder or PyTorch runtime is needed.
"""

from __future__ import annotations

import math
import threading
from functools import lru_cache
from pathlib import Path

EMBEDDING_DIM = 768
IMAGE_SOFT_TOKENS = 70
IMAGE_PATCH_SIZE = 16
VISION_POOL_SIZE = 3
EMPTY_FEATURE_DIM = 512

# https://ai.google.dev/gemma/docs/embeddinggemma/model_card_2
QUERY_PREFIX = "task: search result | query: "
DOCUMENT_PREFIX = "title: none | text: "


def image_patch_inputs(image_bytes: bytes, soft_tokens: int = IMAGE_SOFT_TOKENS):
    """Return [1, 9*budget, 768] patches and 2D positions.

    The resize and patch ordering match Gemma4ImageProcessor without torch.
    """
    import cv2
    import numpy as np

    if soft_tokens not in (70, 140, 280):
        raise ValueError("unsupported image token budget")
    image = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("image could not be decoded")
    image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    height, width = image.shape[:2]
    max_patches = soft_tokens * VISION_POOL_SIZE**2
    target_pixels = max_patches * IMAGE_PATCH_SIZE**2
    factor = math.sqrt(target_pixels / (height * width))
    side_multiple = IMAGE_PATCH_SIZE * VISION_POOL_SIZE
    new_height = int(factor * height // side_multiple) * side_multiple
    new_width = int(factor * width // side_multiple) * side_multiple
    if not new_height:
        new_height = side_multiple
        new_width = min(
            int(width / height) * side_multiple,
            soft_tokens * side_multiple,
        )
    if not new_width:
        new_width = side_multiple
        new_height = min(
            int(height / width) * side_multiple,
            soft_tokens * side_multiple,
        )
    if min(new_height, new_width) <= 0 or new_height * new_width > target_pixels:
        raise ValueError("image aspect ratio exceeds patch budget")
    resized = cv2.resize(
        image,
        (new_width, new_height),
        interpolation=(
            cv2.INTER_AREA
            if height >= new_height and width >= new_width
            else cv2.INTER_LINEAR
        ),
    ).astype(np.float32) / 255.0
    grid_height, grid_width = new_height // IMAGE_PATCH_SIZE, new_width // IMAGE_PATCH_SIZE
    patches = resized.reshape(
        grid_height, IMAGE_PATCH_SIZE, grid_width, IMAGE_PATCH_SIZE, 3
    ).transpose(0, 2, 1, 3, 4).reshape(-1, IMAGE_PATCH_SIZE**2 * 3)
    x, y = np.meshgrid(np.arange(grid_width), np.arange(grid_height), indexing="xy")
    positions = np.stack((x.ravel(), y.ravel()), axis=-1)
    padded = np.zeros((1, max_patches, IMAGE_PATCH_SIZE**2 * 3), dtype=np.float32)
    ids = np.full((1, max_patches, 2), -1, dtype=np.int64)
    padded[0, :len(patches)] = patches
    ids[0, :len(positions)] = positions
    return padded, ids, len(patches) // VISION_POOL_SIZE**2


class UnifiedEmbedder:
    def __init__(self, text_path: Path, vision_path: Path, tokenizer_path: Path) -> None:
        self._text_path = text_path
        self._vision_path = vision_path
        self._tokenizer_path = tokenizer_path
        self._text_session = None
        self._vision_session = None
        self._tokenizer = None
        self._state_lock = threading.RLock()

    @property
    def is_loaded(self) -> bool:
        return self._text_session is not None or self._vision_session is not None

    @staticmethod
    def _open(path: Path):
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        return ort.InferenceSession(
            str(path), sess_options=options, providers=["CPUExecutionProvider"]
        )

    def load(self) -> None:
        with self._state_lock:
            if self._text_session is None:
                self._text_session = self._open(self._text_path)
            if self._tokenizer is None:
                from tokenizers import Tokenizer

                self._tokenizer = Tokenizer.from_file(str(self._tokenizer_path))
                self._tokenizer.enable_truncation(max_length=384)

    def unload(self) -> None:
        with self._state_lock:
            self._text_session = None
            self._vision_session = None
            self._tokenizer = None
            self.encode_query.cache_clear()

    def _encode(self, text: str):
        with self._state_lock:
            self.load()
            encoding = self._tokenizer.encode(text)
            return self._inference(encoding.ids, encoding.attention_mask)

    def _inference(self, token_ids, attention_mask, image_features=None):
        import numpy as np

        if image_features is None:
            image_features = np.empty((0, EMPTY_FEATURE_DIM), dtype=np.float32)
        empty = np.empty((0, EMPTY_FEATURE_DIM), dtype=np.float32)
        inputs = {
            "input_ids": np.asarray([token_ids], dtype=np.int64),
            "attention_mask": np.asarray([attention_mask], dtype=np.int64),
            "image_features": image_features,
            "video_features": empty,
            "audio_features": empty,
        }
        result = self._text_session.run(["sentence_embedding"], inputs)[0][0]
        length = np.linalg.norm(result)
        return (result / length if length else result).astype(np.float32)

    @lru_cache(maxsize=8)
    def encode_query(self, text: str):
        return self._encode(QUERY_PREFIX + text)

    def encode_text(self, text: str):
        return self._encode(DOCUMENT_PREFIX + text)

    def encode_image(self, content: bytes):
        with self._state_lock:
            return self._encode_image_locked(content)

    def _encode_image_locked(self, content: bytes):
        import numpy as np

        self.load()
        if self._vision_session is None:
            self._vision_session = self._open(self._vision_path)
        patches, positions, count = image_patch_inputs(content)
        features = self._vision_session.run(
            ["image_features"],
            {"pixel_values": patches, "pixel_position_ids": positions},
        )[0]
        if features.ndim != 2 or features.shape[0] < count:
            raise ValueError("unexpected vision encoder output")
        image_ids = [2, 255999] + [258880] * count + [258882, 1]
        return self._inference(
            image_ids,
            [1] * len(image_ids),
            np.asarray(features[:count], dtype=np.float32),
        )
