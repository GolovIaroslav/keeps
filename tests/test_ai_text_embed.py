import numpy as np
import pytest

from keeps.ai.text_embed import EMBEDDING_DIM, TextEmbedder, cls_pool_normalize


def test_cls_pool_normalize_picks_first_token_and_unit_norm():
    # batch=2, seq_len=3, hidden=4. CLS pooling must take index 0 of the
    # sequence dim, not e.g. the mean over all tokens.
    hidden = np.array(
        [
            [[3.0, 4.0, 0.0, 0.0], [100.0, 100.0, 100.0, 100.0], [0.0, 0.0, 0.0, 0.0]],
            [[0.0, 0.0, 6.0, 8.0], [1.0, 1.0, 1.0, 1.0], [2.0, 2.0, 2.0, 2.0]],
        ]
    )
    result = cls_pool_normalize(hidden)

    np.testing.assert_allclose(result[0], [0.6, 0.8, 0.0, 0.0])
    np.testing.assert_allclose(result[1], [0.0, 0.0, 0.6, 0.8])
    np.testing.assert_allclose(np.linalg.norm(result, axis=1), [1.0, 1.0])


def test_cls_pool_normalize_handles_zero_vector_without_dividing_by_zero():
    hidden = np.zeros((1, 2, 4))
    result = cls_pool_normalize(hidden)
    np.testing.assert_allclose(result[0], [0.0, 0.0, 0.0, 0.0])


class _FakeEncoding:
    def __init__(self, ids, attention_mask):
        self.ids = ids
        self.attention_mask = attention_mask


class _FakeTokenizer:
    def encode(self, text: str) -> _FakeEncoding:
        # deterministic stand-in: token id = char code, all attended
        ids = [ord(c) for c in text[:8]]
        return _FakeEncoding(ids, [1] * len(ids))


class _FakeSession:
    def __init__(self, hidden_state):
        self._hidden_state = hidden_state
        self.calls = []

    def run(self, output_names, inputs):
        self.calls.append(inputs)
        return [self._hidden_state]


def _embedder_with_fakes(hidden_state) -> TextEmbedder:
    embedder = TextEmbedder(weights_path="unused", tokenizer_path="unused")
    embedder._session = _FakeSession(hidden_state)
    embedder._tokenizer = _FakeTokenizer()
    return embedder


def test_encode_returns_unit_vector_of_expected_dim():
    hidden_state = np.random.default_rng(0).normal(size=(1, 5, EMBEDDING_DIM))
    embedder = _embedder_with_fakes(hidden_state)

    vec = embedder.encode("hello world")

    assert vec.shape == (EMBEDDING_DIM,)
    assert pytest.approx(np.linalg.norm(vec), abs=1e-6) == 1.0


def test_encode_is_deterministic_for_same_input():
    hidden_state = np.random.default_rng(1).normal(size=(1, 4, EMBEDDING_DIM))
    embedder = _embedder_with_fakes(hidden_state)

    first = embedder.encode("repeatable query")
    second = embedder.encode("repeatable query")

    np.testing.assert_array_equal(first, second)


def test_encode_uses_cls_token_not_mean_of_sequence():
    hidden_state = np.zeros((1, 3, EMBEDDING_DIM))
    hidden_state[0, 0, 0] = 1.0  # CLS token has a distinct signal
    hidden_state[0, 1:, 0] = 999.0  # other tokens would dominate a mean-pool
    embedder = _embedder_with_fakes(hidden_state)

    vec = embedder.encode("x")

    assert vec[0] == pytest.approx(1.0)


def test_load_is_a_noop_once_session_already_present():
    embedder = _embedder_with_fakes(np.zeros((1, 1, EMBEDDING_DIM)))
    fake_session = embedder._session
    embedder.load()
    assert embedder._session is fake_session, "load() must not replace an already-live session"


def test_is_loaded_reflects_session_state():
    embedder = TextEmbedder(weights_path="unused", tokenizer_path="unused")
    assert embedder.is_loaded is False
    embedder._session = object()
    assert embedder.is_loaded is True
    embedder.unload()
    assert embedder.is_loaded is False
