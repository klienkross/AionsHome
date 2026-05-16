"""测试 Ebbinghaus 衰减引擎 compute_vitality 纯函数"""
import pytest
from decay_engine import compute_vitality


def test_fresh_card_defaults():
    v = compute_vitality(0.3, 1, 1000000, 0, 0, 0, now=1000000)
    assert 0.2 < v < 0.5


def test_decay_over_days():
    now = 1000000
    v_fresh = compute_vitality(0.3, 1, now, 0, 0, 0, now=now)
    v_30d = compute_vitality(0.3, 1, now, 0, 0, 0, now=now + 30 * 86400)
    assert v_30d < v_fresh * 0.5


def test_activation_count_boosts():
    now = 1000000
    v1 = compute_vitality(0.3, 1, now, 0, 0, 0, now=now)
    v10 = compute_vitality(0.3, 10, now, 0, 0, 0, now=now)
    assert v10 > v1
    assert v10 < v1 * 3


def test_importance_matters():
    now = 1000000
    v_low = compute_vitality(0.1, 1, now, 0, 0, 0, now=now)
    v_high = compute_vitality(0.9, 1, now, 0, 0, 0, now=now)
    assert v_high > v_low * 2


def test_negative_valence_boosts():
    now = 1000000
    v_neutral = compute_vitality(0.3, 1, now, 0, 0, 0, now=now)
    v_negative = compute_vitality(0.3, 1, now, -0.9, 0, 0, now=now)
    assert v_negative > v_neutral


def test_high_arousal_boosts():
    now = 1000000
    v_low = compute_vitality(0.3, 1, now, 0, 0, 0, now=now)
    v_high = compute_vitality(0.3, 1, now, 0, 0.9, 0, now=now)
    assert v_high > v_low


def test_zero_importance_clamped():
    v = compute_vitality(0, 1, 1000000, 0, 0, 0, now=1000000)
    assert v > 0


def test_zero_activation_clamped():
    v = compute_vitality(0.3, 0, 1000000, 0, 0, 0, now=1000000)
    assert v > 0


def test_very_old_card_approaches_zero():
    now = 1000000
    v = compute_vitality(0.3, 1, now, 0, 0, 0, now=now + 1000 * 86400)
    assert v < 0.01


def test_last_activated_none_defaults_to_now():
    v = compute_vitality(0.3, 1, 0, 0, 0, 0)
    assert v > 0


def test_identical_params_deterministic():
    now = 1000000
    v1 = compute_vitality(0.3, 5, 900000, 0.5, 0.3, 0, now=now)
    v2 = compute_vitality(0.3, 5, 900000, 0.5, 0.3, 0, now=now)
    assert v1 == v2
