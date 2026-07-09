"""Text embeddings: IBM Granite Embedding 97M Multilingual R2, CLS-pooled (PLAN.md §9).

onnxruntime/tokenizers/numpy are imported lazily inside TextEmbedder methods, not
at module level, so importing keeps.ai.text_embed stays free when RAG is off.
"""

from __future__ import annotations

from pathlib import Path

# Verified against the model's own config.json/1_Pooling config.json (PLAN.md §9).
EMBEDDING_DIM = 384
PAD_TOKEN_ID = 179935
DEFAULT_MAX_LENGTH = 384


def cls_pool_normalize(last_hidden_state) -> object:
    """CLS-pool (index 0 of the sequence dim) + L2-normalize a batch of hidden states.

    `last_hidden_state` has shape (batch, seq_len, hidden). Pure numpy math,
    factored out so it's testable without a real ONNX session (inject a fake
    array standing in for a model's output).
    """
    import numpy as np

    cls = last_hidden_state[:, 0, :]
    norms = np.linalg.norm(cls, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1.0, norms)
    return cls / norms


class TextEmbedder:
    """Lazy wrapper around the ONNX text-embedding session + tokenizer.

    Construction does no I/O; the ONNX session and tokenizer are loaded on
    first `encode()` call (ai/runtime.py treats a live `_session` as "loaded
    into RAM" for the Model management status).
    """

    def __init__(
        self,
        weights_path: Path,
        tokenizer_path: Path,
        max_length: int = DEFAULT_MAX_LENGTH,
    ) -> None:
        self._weights_path = weights_path
        self._tokenizer_path = tokenizer_path
        self._max_length = max_length
        self._session = None
        self._tokenizer = None

    @property
    def is_loaded(self) -> bool:
        return self._session is not None

    def load(self) -> None:
        if self._session is not None:
            return
        import onnxruntime as ort
        from tokenizers import Tokenizer

        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._session = ort.InferenceSession(
            str(self._weights_path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self._tokenizer = Tokenizer.from_file(str(self._tokenizer_path))
        self._tokenizer.enable_truncation(max_length=self._max_length)
        self._tokenizer.enable_padding(pad_id=PAD_TOKEN_ID, pad_token="<|endoftext|>")

    def unload(self) -> None:
        self._session = None
        self._tokenizer = None

    def encode(self, text: str):
        """Return an L2-normalized (384,) float32 embedding for `text`."""
        import numpy as np

        self.load()
        encoding = self._tokenizer.encode(text)
        input_ids = np.array([encoding.ids], dtype=np.int64)
        attention_mask = np.array([encoding.attention_mask], dtype=np.int64)
        # Input names confirmed live against the real ONNX graph (session.get_inputs()):
        # input_ids/attention_mask only, no token_type_ids (PLAN.md §9).
        outputs = self._session.run(
            None, {"input_ids": input_ids, "attention_mask": attention_mask}
        )
        pooled = cls_pool_normalize(outputs[0])
        return pooled[0]
