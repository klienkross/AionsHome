"""测试 digest_v2 的解析器：_parse_atomic_cards, _parse_emotion_output, _parse_lifecycle_judgment"""
import json
from digest_v2 import _parse_atomic_cards, _parse_emotion_output, _parse_lifecycle_judgment


# ── _parse_atomic_cards ──

def test_atomic_normal_json_array():
    raw = json.dumps([
        {"content": "AI链式哈希讨论", "type": "event", "keywords": ["技术开发", "链式哈希"], "importance": 0.7, "unresolved": False, "valence": 0.2, "arousal": 0.3, "source": "both"},
        {"content": "计划下周完成测试", "type": "plan", "keywords": ["技术开发", "测试"], "importance": 0.5, "unresolved": True, "valence": 0.1, "arousal": 0.2, "source": "user"},
    ])
    result = _parse_atomic_cards(raw)
    assert len(result) == 2
    assert result[0]["type"] == "event"
    assert result[0]["importance"] == 0.7
    assert result[1]["unresolved"] == 1


def test_atomic_json_in_markdown_block():
    raw = '```json\n[{"content": "测试卡片", "type": "fact", "keywords": ["测试"], "importance": 0.3, "unresolved": false}]\n```'
    result = _parse_atomic_cards(raw)
    assert len(result) == 1
    assert result[0]["content"] == "测试卡片"


def test_atomic_json_with_preamble():
    raw = '好的，以下是记忆卡片：\n[{"content": "前面有废话的卡片", "type": "event", "keywords": ["杂项"], "importance": 0.3, "unresolved": false}]'
    result = _parse_atomic_cards(raw)
    assert len(result) == 1


def test_atomic_json_with_postamble():
    raw = '[{"content": "后面有废话", "type": "event", "keywords": ["杂项"], "importance": 0.3, "unresolved": false}]\n希望这些卡片对你有帮助！'
    result = _parse_atomic_cards(raw)
    assert len(result) == 1


def test_atomic_empty_input():
    assert _parse_atomic_cards("") == []
    assert _parse_atomic_cards(None) == []


def test_atomic_not_json():
    assert _parse_atomic_cards("这不是 JSON") == []


def test_atomic_empty_array():
    assert _parse_atomic_cards("[]") == []


def test_atomic_skips_empty_content():
    raw = json.dumps([
        {"content": "", "type": "event", "keywords": [], "importance": 0.3, "unresolved": False},
        {"content": "有效卡片", "type": "fact", "keywords": ["测试"], "importance": 0.3, "unresolved": False},
    ])
    result = _parse_atomic_cards(raw)
    assert len(result) == 1


def test_atomic_handles_already_parsed_list():
    raw = [{"content": "直接列表", "type": "fact", "keywords": ["测试"], "importance": 0.5, "unresolved": False}]
    result = _parse_atomic_cards(raw)
    assert len(result) == 1


def test_atomic_default_values():
    raw = json.dumps([{"content": "最小字段"}])
    result = _parse_atomic_cards(raw)
    assert len(result) == 1
    assert result[0]["type"] == "event"
    assert result[0]["importance"] == 0.5
    assert result[0]["unresolved"] == 0
    assert result[0]["valence"] == 0.0
    assert result[0]["source"] == "both"


# ── _parse_emotion_output ──

def test_emotion_normal():
    raw = json.dumps([{"valence": 0.8, "arousal": 0.6}, {"valence": -0.3, "arousal": -0.5}])
    result = _parse_emotion_output(raw, ["卡片A", "卡片B"])
    assert len(result) == 2
    assert result[0]["valence"] == 0.8
    assert result[1]["valence"] == -0.3


def test_emotion_clamps():
    raw = json.dumps([{"valence": 2.0, "arousal": -2.0}])
    result = _parse_emotion_output(raw, ["test"])
    assert result[0]["valence"] == 1.0
    assert result[0]["arousal"] == -1.0


def test_emotion_invalid_json_fallback():
    result = _parse_emotion_output("not json", ["A", "B"])
    assert len(result) == 2
    assert result[0] == {"valence": 0.0, "arousal": 0.0}


def test_emotion_list_input():
    result = _parse_emotion_output([{"valence": 0.5}], ["test"])
    assert result[0]["valence"] == 0.5
    assert result[0]["arousal"] == 0.0


def test_emotion_mismatched_length():
    result = _parse_emotion_output(json.dumps([{"valence": 0.5}]), ["A", "B", "C"])
    assert len(result) == 3
    assert result[0]["valence"] == 0.5
    assert result[1] == {"valence": 0.0, "arousal": 0.0}


# ── _parse_lifecycle_judgment ──

def test_lifecycle_valid_json():
    raw = json.dumps({"should_close": True, "confidence": 0.9, "relation": "follow_up"})
    result = _parse_lifecycle_judgment(raw)
    assert result["should_close"] is True
    assert result["confidence"] == 0.9
    assert result["relation"] == "follow_up"


def test_lifecycle_invalid_json():
    result = _parse_lifecycle_judgment("not json")
    assert result["should_close"] is False
    assert result["confidence"] == 0.0


def test_lifecycle_dict_input():
    result = _parse_lifecycle_judgment({"should_close": True})
    assert result["should_close"] is True


def test_lifecycle_partial_fields():
    result = _parse_lifecycle_judgment(json.dumps({"should_close": True}))
    assert result["confidence"] == 0.0
    assert result["relation"] == "related"


def test_lifecycle_none_input():
    result = _parse_lifecycle_judgment(None)
    assert result["should_close"] is False
