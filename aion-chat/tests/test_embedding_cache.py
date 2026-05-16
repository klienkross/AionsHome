"""测试 embedding_cache numpy 向量缓存"""
import pytest
import numpy as np
import embedding_cache


@pytest.fixture(autouse=True)
def reset_cache():
    embedding_cache._matrix = None
    embedding_cache._card_ids = []
    embedding_cache._id_to_idx = {}
    embedding_cache._dirty = False


def test_initial_empty():
    assert not embedding_cache.is_loaded()
    assert embedding_cache.count() == 0
    assert embedding_cache.batch_cosine([0.1, 0.2, 0.3]) == []


def test_add_and_query_single():
    embedding_cache.add("card_1", [1.0, 0.0, 0.0])
    assert embedding_cache.is_loaded()
    assert embedding_cache.count() == 1

    results = embedding_cache.batch_cosine([1.0, 0.0, 0.0])
    assert len(results) == 1
    assert results[0][0] == "card_1"
    assert abs(results[0][1] - 1.0) < 0.001


def test_add_and_query_multiple():
    embedding_cache.add("card_1", [1.0, 0.0])
    embedding_cache.add("card_2", [0.0, 1.0])
    assert embedding_cache.count() == 2

    results = embedding_cache.batch_cosine([0.9, 0.1])
    scores = dict(results)
    assert scores["card_1"] > scores["card_2"]


def test_zero_vector_query():
    embedding_cache.add("card_1", [1.0, 0.0])
    results = embedding_cache.batch_cosine([0.0, 0.0])
    assert results[0][1] == 0.0


def test_invalidate_zeros_vector():
    embedding_cache.add("card_1", [1.0, 0.0])
    embedding_cache.invalidate("card_1")
    assert embedding_cache._dirty is True

    results = embedding_cache.batch_cosine([1.0, 0.0])
    assert results[0][1] == 0.0


def test_invalidate_nonexistent_no_error():
    embedding_cache.invalidate("不存在")
    assert not embedding_cache._dirty


def test_compact_removes_invalidated():
    embedding_cache.add("keep", [1.0, 0.0])
    embedding_cache.add("drop", [0.0, 1.0])
    embedding_cache.invalidate("drop")
    embedding_cache.compact()

    assert embedding_cache.count() == 1
    assert "keep" in embedding_cache._id_to_idx
    assert "drop" not in embedding_cache._id_to_idx

    results = embedding_cache.batch_cosine([1.0, 0.0])
    assert len(results) == 1
    assert results[0][0] == "keep"


def test_compact_no_dirty_noop():
    embedding_cache.add("card_1", [1.0, 0.0])
    embedding_cache.compact()
    assert embedding_cache.count() == 1
    assert embedding_cache._dirty is False


def test_batch_cosine_filtered():
    embedding_cache.add("a", [1.0, 0.0, 0.0])
    embedding_cache.add("b", [0.0, 1.0, 0.0])
    embedding_cache.add("c", [0.0, 0.0, 1.0])

    results = embedding_cache.batch_cosine_filtered([1.0, 0.0, 0.0], {"a", "c"})
    ids = {r[0] for r in results}
    assert ids == {"a", "c"}


def test_batch_cosine_filtered_empty_set():
    embedding_cache.add("a", [1.0, 0.0])
    assert embedding_cache.batch_cosine_filtered([1.0, 0.0], set()) == []


def test_batch_cosine_filtered_nonexistent_ids():
    embedding_cache.add("a", [1.0, 0.0])
    assert embedding_cache.batch_cosine_filtered([1.0, 0.0], {"x", "y"}) == []


def test_normalization_after_add():
    """add 时自动 L2 归一化：[3,4] → [0.6, 0.4]，与 [1,0] 的余弦为 0.6"""
    embedding_cache.add("card_1", [3.0, 4.0])
    results = embedding_cache.batch_cosine([1.0, 0.0])
    assert abs(results[0][1] - 0.6) < 0.001


def test_high_dimensional():
    rng = np.random.RandomState(42)
    dim = 768
    vec_a = rng.randn(dim).astype(np.float32).tolist()
    vec_b = rng.randn(dim).astype(np.float32).tolist()
    vec_q = rng.randn(dim).astype(np.float32).tolist()

    embedding_cache.add("a", vec_a)
    embedding_cache.add("b", vec_b)

    results = embedding_cache.batch_cosine(vec_q)
    assert len(results) == 2
    assert all(abs(s) <= 1.0 for _, s in results)
