"""测试链式哈希：compute_chain_hash + crc32"""
import pytest
from chain_hash import compute_chain_hash, crc32


def test_crc32_empty():
    assert crc32(b"") == 0


def test_crc32_known_value():
    assert crc32(b"hello") == 0x3610A686


def test_crc32_deterministic():
    assert crc32(b"test") == crc32(b"test")


def test_crc32_different_input():
    assert crc32(b"hello") != crc32(b"world")


def test_chain_hash_first_message():
    h = compute_chain_hash("00000000", "msg_001", "你好世界", 1000000.0)
    assert len(h) == 8
    assert all(c in "0123456789abcdef" for c in h)


def test_chain_hash_chaining():
    h1 = compute_chain_hash("00000000", "msg_001", "你好", 1000000.0)
    h2 = compute_chain_hash(h1, "msg_002", "世界", 1000060.0)
    assert h1 != h2


def test_chain_hash_deterministic():
    h1 = compute_chain_hash("00000000", "msg_001", "test", 1000000.0)
    h2 = compute_chain_hash("00000000", "msg_001", "test", 1000000.0)
    assert h1 == h2


def test_chain_hash_sensitive_to_content():
    h1 = compute_chain_hash("00000000", "msg_001", "hello", 1000000.0)
    h2 = compute_chain_hash("00000000", "msg_001", "hallo", 1000000.0)
    assert h1 != h2


def test_chain_hash_sensitive_to_prev_hash():
    h1 = compute_chain_hash("00000000", "msg_001", "test", 1000000.0)
    h2 = compute_chain_hash("ffffffff", "msg_001", "test", 1000000.0)
    assert h1 != h2


def test_chain_hash_sensitive_to_msg_id():
    h1 = compute_chain_hash("00000000", "msg_001", "test", 1000000.0)
    h2 = compute_chain_hash("00000000", "msg_002", "test", 1000000.0)
    assert h1 != h2


def test_chain_hash_sensitive_to_timestamp():
    h1 = compute_chain_hash("00000000", "msg_001", "test", 1000000.0)
    h2 = compute_chain_hash("00000000", "msg_001", "test", 1000060.0)
    assert h1 != h2


def test_chain_hash_empty_content():
    h = compute_chain_hash("00000000", "msg_001", "", 1000000.0)
    assert len(h) == 8


def test_chain_integrity_scenario():
    """模拟三条消息的链：第2条被篡改 → 第2条的哈希不匹配"""
    messages = [
        ("msg_1", "今天天气不错", 1000000.0),
        ("msg_2", "是啊很适合出去走走", 1000060.0),
        ("msg_3", "下午一起去公园吧", 1000120.0),
    ]
    hashes = []
    prev = "00000000"
    for mid, content, ts in messages:
        h = compute_chain_hash(prev, mid, content, ts)
        hashes.append(h)
        prev = h

    # 完整重算通过
    prev = "00000000"
    for i, (mid, content, ts) in enumerate(messages):
        h = compute_chain_hash(prev, mid, content, ts)
        assert h == hashes[i]
        prev = h

    # 篡改第二条
    tampered = [messages[0], ("msg_2", "篡改后的内容", 1000060.0), messages[2]]
    prev = "00000000"
    mismatch = None
    for i, (mid, content, ts) in enumerate(tampered):
        h = compute_chain_hash(prev, mid, content, ts)
        if h != hashes[i]:
            mismatch = i
            break
        prev = h
    assert mismatch == 1


def test_timestamp_precision():
    h1 = compute_chain_hash("00000000", "m1", "x", 1000000.123456)
    h2 = compute_chain_hash("00000000", "m1", "x", 1000000.123457)
    assert h1 != h2
