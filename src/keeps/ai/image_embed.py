"""SigLIP 2 text/image embeddings for visual clipboard search.

The two INT8 ONNX towers share one 768-dimensional space. Heavy optional
dependencies stay lazy so keyword-only Keeps startup remains lightweight.
"""

from __future__ import annotations

from pathlib import Path

EMBEDDING_DIM = 768
IMAGE_SIZE = 224
TEXT_LENGTH = 64


def visual_query_text(text: str) -> str:
    """Apply SigLIP2's training-time text normalization."""
    return text.strip().lower()


def l2_normalize(vector):
    import numpy as np

    vector = np.asarray(vector, dtype=np.float32)
    norm = np.linalg.norm(vector)
    return vector / norm if norm else vector


def preprocess_image(image_bytes: bytes):
    """Decode an image and apply SigLIP2's fixed 224px RGB preprocessing."""
    import cv2
    import numpy as np

    encoded = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(encoded, cv2.IMREAD_UNCHANGED)
    if image is None:
        raise ValueError("image could not be decoded")
    if image.ndim == 2:
        image = cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)
    elif image.shape[2] == 4:
        image = cv2.cvtColor(image, cv2.COLOR_BGRA2RGB)
    else:
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    image = cv2.resize(image, (IMAGE_SIZE, IMAGE_SIZE), interpolation=cv2.INTER_LINEAR)
    # Official processor config: rescale 1/255, then mean/std 0.5.
    image = image.astype(np.float32) / 127.5 - 1.0
    return np.transpose(image, (2, 0, 1))[None, ...]


class ImageEmbedder:
    """Lazy split SigLIP2 ONNX runtime for query text and clipboard images."""

    def __init__(self, text_path: Path, vision_path: Path, tokenizer_path: Path) -> None:
        self._text_path = text_path
        self._vision_path = vision_path
        self._tokenizer_path = tokenizer_path
        self._text_session = None
        self._vision_session = None
        self._tokenizer = None

    @property
    def is_loaded(self) -> bool:
        return self._text_session is not None or self._vision_session is not None

    def _session(self, path: Path):
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        return ort.InferenceSession(
            str(path), sess_options=options, providers=["CPUExecutionProvider"]
        )

    def load(self) -> None:
        self._load_text()
        if self._vision_session is None:
            self._vision_session = self._session(self._vision_path)

    def _load_text(self) -> None:
        if self._text_session is None:
            self._text_session = self._session(self._text_path)
        if self._tokenizer is None:
            from tokenizers import Tokenizer

            self._tokenizer = Tokenizer.from_file(str(self._tokenizer_path))
            self._tokenizer.enable_truncation(max_length=TEXT_LENGTH)
            self._tokenizer.enable_padding(
                length=TEXT_LENGTH, pad_id=0, pad_token="<pad>"
            )

    def unload(self) -> None:
        self._text_session = None
        self._vision_session = None
        self._tokenizer = None

    def encode_text(self, text: str):
        import numpy as np

        self._load_text()
        encoding = self._tokenizer.encode(visual_query_text(text))
        input_ids = np.asarray([encoding.ids], dtype=np.int64)
        outputs = self._text_session.run(None, {"input_ids": input_ids})
        return l2_normalize(outputs[1][0])

    def encode_image(self, image_bytes: bytes):
        if self._vision_session is None:
            self._vision_session = self._session(self._vision_path)
        outputs = self._vision_session.run(
            None, {"pixel_values": preprocess_image(image_bytes)}
        )
        return l2_normalize(outputs[1][0])
