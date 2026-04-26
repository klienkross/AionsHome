import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

def test_parse_atomic_cards():
    """Test parsing of Agent A output format"""
    from digest_v2 import _parse_atomic_cards

    raw = json.dumps([
        {"content": "去了咖啡店", "type": "event", "keywords": ["咖啡店"], "importance": 0.5},
        {"content": "喜欢拿铁", "type": "preference", "keywords": ["拿铁"], "importance": 0.6},
        {"content": "计划下周再去", "type": "plan", "keywords": ["咖啡店"], "importance": 0.3},
    ])
    cards = _parse_atomic_cards(raw)
    assert len(cards) == 3
    assert cards[0]["type"] == "event"
    assert cards[1]["type"] == "preference"
    assert cards[2]["type"] == "plan"

    # Malformed input returns empty
    assert _parse_atomic_cards("not json") == []
    assert _parse_atomic_cards("{}") == []
    assert _parse_atomic_cards('[{"no_content": true}]') == []

def test_compute_intensity():
    from digest_v2 import compute_intensity

    # Fast, long messages, many turns → high intensity
    msgs_fast = [
        {"created_at": 1000.0 + i * 10, "content": "x" * 200, "role": "user" if i % 2 == 0 else "assistant"}
        for i in range(20)
    ]
    score_fast = compute_intensity(msgs_fast)
    assert 0.7 < score_fast <= 1.0, f"Expected high intensity, got {score_fast}"

    # Slow, short messages, few turns → low intensity
    msgs_slow = [
        {"created_at": 1000.0 + i * 600, "content": "ok", "role": "user" if i % 2 == 0 else "assistant"}
        for i in range(3)
    ]
    score_slow = compute_intensity(msgs_slow)
    assert score_slow < 0.4, f"Expected low intensity, got {score_slow}"

if __name__ == "__main__":
    test_parse_atomic_cards()
    print("PASS: test_parse_atomic_cards")
    test_compute_intensity()
    print("PASS: test_compute_intensity")
