import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

def test_parse_lifecycle_judgment():
    from digest_v2 import _parse_lifecycle_judgment

    raw = json.dumps({
        "should_close": True,
        "confidence": 0.9,
        "relation": "follow_up",
    })
    result = _parse_lifecycle_judgment(raw)
    assert result["should_close"] is True
    assert result["confidence"] == 0.9
    assert result["relation"] == "follow_up"

    # Invalid input
    result = _parse_lifecycle_judgment("not json")
    assert result["should_close"] is False

if __name__ == "__main__":
    test_parse_lifecycle_judgment()
    print("PASS: test_parse_lifecycle_judgment")
