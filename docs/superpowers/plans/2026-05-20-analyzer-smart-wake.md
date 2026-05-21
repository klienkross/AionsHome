# 智能唤醒分析层 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 新建 `analyzer.py` 本地规则引擎，替代 sentinel 的 call_core 判断，用纯 Python 统计（零 token）决定是否唤醒 bot。

**Architecture:** analyzer.py 是一个无状态函数模块（仅内存冷却 dict），嵌入 sensor.py 的 `_analyze_events()` 末尾。通过 `SETTINGS["analyzer_enabled"]` 开关控制，关闭时回退到原来 sentinel 自己判断。

**Tech Stack:** Python 3.11+，标准库 time/logging，无外部依赖。pytest 测试。

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `aion-chat/analyzer.py` | Create | 规则引擎：extract_apps、is_active、三条规则函数、should_wake 入口、冷却管理 |
| `aion-chat/tests/test_analyzer.py` | Create | analyzer 所有公开函数的单元测试 |
| `aion-chat/sensor.py:308-340` | Modify | sentinel prompt 去掉 call_core 判断指引 |
| `aion-chat/sensor.py:376-377` | Modify | 末尾加 analyzer 开关分支 |

---

### Task 1: analyzer.py — 辅助函数 extract_apps + is_active

**Files:**
- Create: `aion-chat/tests/test_analyzer.py`
- Create: `aion-chat/analyzer.py`

- [x] **Step 1: Write failing tests for extract_apps and is_active**

```python
# aion-chat/tests/test_analyzer.py
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from analyzer import extract_apps, is_active

class TestExtractApps:
    def test_finds_chinese_app(self):
        log = "📱 K在家，手机亮屏刷了会小红书，之后看了B站"
        apps = extract_apps(log)
        assert "小红书" in apps
        assert "B站" in apps

    def test_finds_english_app(self):
        log = "📱 K在PC上用VS Code写代码，同时开着Edge查资料"
        apps = extract_apps(log)
        assert "VS Code" in apps
        assert "Edge" in apps

    def test_no_apps(self):
        log = "📱 K的手机处于锁屏状态，无活动"
        apps = extract_apps(log)
        assert len(apps) == 0

    def test_ignores_aion(self):
        """Aion 是 bot 自身，不算用户使用的 app"""
        log = "📱 前台app：Aion Oloth"
        apps = extract_apps(log)
        assert "Aion" in apps


class TestIsActive:
    def test_active_with_known_app(self):
        log = "📱 K在刷小红书"
        assert is_active(log) is True

    def test_active_with_keyword(self):
        log = "📱 手机亮屏，荣耀桌面前台"
        assert is_active(log) is True

    def test_inactive(self):
        log = "📱 K的手机处于锁屏状态，无活动记录"
        assert is_active(log) is False

    def test_inactive_sleep(self):
        log = "📱 无传感器事件，设备静默"
        assert is_active(log) is False
```

- [x] **Step 2: Run tests to verify they fail**

Run: `cd D:\pyworks\AionsHome\aion-chat && python -m pytest tests/test_analyzer.py -v`
Expected: ModuleNotFoundError or ImportError — analyzer.py doesn't exist yet.

- [x] **Step 3: Implement extract_apps and is_active**

```python
# aion-chat/analyzer.py
"""
本地分析层：纯 Python 规则判断是否唤醒 bot，替代 sentinel 的 call_core。
"""

import time
import logging

log = logging.getLogger("analyzer")

KNOWN_APPS = {
    "小红书", "B站", "QQ", "微信", "美团", "DeepSeek",
    "VS Code", "Obsidian", "Edge", "终端", "钉钉",
    "Aion", "MacroDroid", "Tailscale", "网易云音乐",
    "人升", "玻尔", "钉钉PC",
}

ACTIVE_KEYWORDS = {"亮屏", "前台", "使用", "切换", "打开"}


def extract_apps(monitoringlog: str) -> set[str]:
    """从 sentinel 报告文本中提取提到的已知 app 名称"""
    found = set()
    for app in KNOWN_APPS:
        if app in monitoringlog:
            found.add(app)
    return found


def is_active(monitoringlog: str) -> bool:
    """判断报告是否表明用户有活跃的屏幕活动"""
    if extract_apps(monitoringlog):
        return True
    return any(kw in monitoringlog for kw in ACTIVE_KEYWORDS)
```

- [x] **Step 4: Run tests to verify they pass**

Run: `cd D:\pyworks\AionsHome\aion-chat && python -m pytest tests/test_analyzer.py -v`
Expected: All 8 tests PASS.

- [x] **Step 5: Commit**

```
git add aion-chat/analyzer.py aion-chat/tests/test_analyzer.py
git commit -m "feat: analyzer 辅助函数 extract_apps + is_active"
```

---

### Task 2: analyzer.py — 三条规则函数 + should_wake 入口

**Files:**
- Modify: `aion-chat/tests/test_analyzer.py` (append tests)
- Modify: `aion-chat/analyzer.py` (add rules + should_wake)

- [x] **Step 1: Write failing tests for three rules and should_wake**

Append to `aion-chat/tests/test_analyzer.py`:

```python
from analyzer import should_wake, _cooldowns, ANALYZER_CONFIG

def _make_log(time_str: str, monitoringlog: str, date: str = "2026-05-20") -> dict:
    """构造 monitor_log 条目用于测试"""
    h, m, s = (int(x) for x in time_str.split(":"))
    import calendar
    from datetime import datetime
    dt = datetime.strptime(f"{date} {time_str}", "%Y-%m-%d %H:%M:%S")
    ts = calendar.timegm(dt.timetuple())
    return {
        "timestamp": ts,
        "time": time_str,
        "date": date,
        "monitoringlog": monitoringlog,
        "summary": "",
        "call_core": False,
        "core_reason": "",
        "source": "sensor",
    }


class TestLateNightLeisure:
    def setup_method(self):
        _cooldowns.clear()

    def test_triggers_on_leisure_app_at_night(self):
        entry = _make_log("02:30:00", "📱 K在刷小红书和B站")
        result = should_wake(entry, [])
        assert result["wake"] is True
        assert result["rule"] == "late_night_leisure"

    def test_no_trigger_when_work_app_present(self):
        entry = _make_log("01:30:00", "📱 K在用VS Code写代码，顺便看了下小红书")
        result = should_wake(entry, [])
        assert result["wake"] is False

    def test_no_trigger_outside_window(self):
        entry = _make_log("10:00:00", "📱 K在刷小红书")
        result = should_wake(entry, [])
        assert result["wake"] is False


class TestLateNightActive:
    def setup_method(self):
        _cooldowns.clear()

    def test_triggers_on_any_activity_after_2am(self):
        entry = _make_log("03:00:00", "📱 K手机亮屏，荣耀桌面前台")
        result = should_wake(entry, [])
        assert result["wake"] is True
        assert result["rule"] == "late_night_active"

    def test_no_trigger_before_2am(self):
        entry = _make_log("01:30:00", "📱 K手机亮屏，荣耀桌面前台")
        # 1:30 不在 late_night_active 的 (2,5) 窗口
        # 但也没有消遣 app，所以 late_night_leisure 也不触发
        result = should_wake(entry, [])
        assert result["wake"] is False

    def test_no_trigger_when_inactive(self):
        entry = _make_log("03:00:00", "📱 K的手机处于锁屏状态，无活动")
        result = should_wake(entry, [])
        assert result["wake"] is False


class TestProlongedScreenTime:
    def setup_method(self):
        _cooldowns.clear()

    def test_triggers_after_8_consecutive_active(self):
        logs = []
        for i in range(8):
            h = 10 + (i * 15) // 60
            m = (i * 15) % 60
            logs.append(_make_log(f"{h:02d}:{m:02d}:00", f"📱 K在用小红书和Edge"))
        entry = logs[-1]
        result = should_wake(entry, logs)
        assert result["wake"] is True
        assert result["rule"] == "prolonged_screen_time"

    def test_no_trigger_with_gap(self):
        logs = []
        for i in range(8):
            h = 10 + (i * 15) // 60
            m = (i * 15) % 60
            text = "📱 K在用小红书" if i != 4 else "📱 K的手机锁屏，无活动"
            logs.append(_make_log(f"{h:02d}:{m:02d}:00", text))
        entry = logs[-1]
        result = should_wake(entry, logs)
        assert result["wake"] is False

    def test_no_trigger_during_excluded_hours(self):
        logs = []
        for i in range(8):
            m = i * 15
            h = 2 + m // 60
            logs.append(_make_log(f"0{h}:{m % 60:02d}:00", "📱 K在刷小红书"))
        entry = logs[-1]
        # 排除时段(0,8)内不触发 prolonged_screen_time
        # 但 late_night_leisure 和 late_night_active 会触发
        result = should_wake(entry, logs)
        assert result["rule"] != "prolonged_screen_time"


class TestCooldown:
    def setup_method(self):
        _cooldowns.clear()

    def test_same_rule_blocked_by_cooldown(self):
        entry1 = _make_log("02:30:00", "📱 K在刷小红书")
        result1 = should_wake(entry1, [])
        assert result1["wake"] is True

        entry2 = _make_log("02:45:00", "📱 K还在刷小红书")
        result2 = should_wake(entry2, [])
        # late_night_leisure 冷却中，但 late_night_active 可以在 2:45 触发
        assert result2["rule"] != "late_night_leisure"

    def test_different_rules_not_blocked(self):
        entry1 = _make_log("01:30:00", "📱 K在刷小红书")
        result1 = should_wake(entry1, [])
        assert result1["wake"] is True
        assert result1["rule"] == "late_night_leisure"

        entry2 = _make_log("03:00:00", "📱 K手机亮屏在刷B站")
        result2 = should_wake(entry2, [])
        assert result2["wake"] is True
        # late_night_leisure 冷却中，但 late_night_active 不受影响
        assert result2["rule"] == "late_night_active"
```

- [x] **Step 2: Run tests to verify new tests fail**

Run: `cd D:\pyworks\AionsHome\aion-chat && python -m pytest tests/test_analyzer.py -v`
Expected: ImportError for `should_wake`, `_cooldowns`, `ANALYZER_CONFIG`.

- [x] **Step 3: Implement config, cooldown, rules, and should_wake**

Append to `aion-chat/analyzer.py` (after the existing `is_active` function):

```python
# ── 配置 ──────────────────────────────────────────
ANALYZER_CONFIG = {
    "late_night_leisure": {
        "window": (1, 5),
        "leisure_apps": {"小红书", "B站", "QQ", "微信"},
        "work_apps": {"VS Code", "终端", "DeepSeek", "Obsidian", "Edge", "钉钉PC"},
        "cooldown": 8 * 3600,
    },
    "late_night_active": {
        "window": (2, 5),
        "cooldown": 8 * 3600,
    },
    "prolonged_screen_time": {
        "min_consecutive": 8,
        "exclude_hours": (0, 8),
        "cooldown": 3 * 3600,
    },
}

# ── 冷却 ──────────────────────────────────────────
_cooldowns: dict[str, float] = {}


def _check_cooldown(rule: str) -> bool:
    """返回 True 表示冷却中（不应触发）"""
    cfg = ANALYZER_CONFIG.get(rule, {})
    cd = cfg.get("cooldown", 0)
    last = _cooldowns.get(rule, 0)
    return (time.time() - last) < cd


def _record_cooldown(rule: str):
    _cooldowns[rule] = time.time()


def _get_hour(log_entry: dict) -> int:
    """从 log_entry 的 time 字段 (HH:MM:SS) 提取小时"""
    try:
        return int(log_entry["time"].split(":")[0])
    except (KeyError, ValueError, IndexError):
        return -1


# ── 规则函数 ──────────────────────────────────────
def _rule_late_night_leisure(log_entry: dict, recent_logs: list[dict]) -> dict | None:
    cfg = ANALYZER_CONFIG["late_night_leisure"]
    hour = _get_hour(log_entry)
    if not (cfg["window"][0] <= hour < cfg["window"][1]):
        return None
    if _check_cooldown("late_night_leisure"):
        return None

    text = log_entry.get("monitoringlog", "")
    apps = extract_apps(text)
    leisure = apps & cfg["leisure_apps"]
    work = apps & cfg["work_apps"]

    if leisure and not work:
        _record_cooldown("late_night_leisure")
        app_names = "、".join(leisure)
        return {"wake": True, "rule": "late_night_leisure",
                "reason": f"凌晨{hour}点还在刷{app_names}"}
    return None


def _rule_late_night_active(log_entry: dict, recent_logs: list[dict]) -> dict | None:
    cfg = ANALYZER_CONFIG["late_night_active"]
    hour = _get_hour(log_entry)
    if not (cfg["window"][0] <= hour < cfg["window"][1]):
        return None
    if _check_cooldown("late_night_active"):
        return None

    text = log_entry.get("monitoringlog", "")
    if is_active(text):
        _record_cooldown("late_night_active")
        return {"wake": True, "rule": "late_night_active",
                "reason": f"凌晨{hour}点仍有屏幕活动"}
    return None


def _rule_prolonged_screen_time(log_entry: dict, recent_logs: list[dict]) -> dict | None:
    cfg = ANALYZER_CONFIG["prolonged_screen_time"]
    hour = _get_hour(log_entry)
    lo, hi = cfg["exclude_hours"]
    if lo <= hour < hi:
        return None
    if _check_cooldown("prolonged_screen_time"):
        return None

    n = cfg["min_consecutive"]
    if len(recent_logs) < n:
        return None

    tail = recent_logs[-n:]
    if all(is_active(e.get("monitoringlog", "")) for e in tail):
        _record_cooldown("prolonged_screen_time")
        hours = n * 15 / 60
        return {"wake": True, "rule": "prolonged_screen_time",
                "reason": f"已连续{hours:.0f}小时在屏幕前，该起来活动一下"}
    return None


# ── 入口 ──────────────────────────────────────────
_RULES = [_rule_late_night_leisure, _rule_late_night_active, _rule_prolonged_screen_time]

def should_wake(log_entry: dict, recent_logs: list[dict]) -> dict:
    """
    输入：当前 monitor_log 条目 + 最近历史条目
    输出：{"wake": bool, "rule": str, "reason": str}
    """
    for rule_fn in _RULES:
        result = rule_fn(log_entry, recent_logs)
        if result:
            log.info("规则触发: %s — %s", result["rule"], result["reason"])
            return result
    return {"wake": False, "rule": "", "reason": ""}
```

- [x] **Step 4: Run all tests to verify they pass**

Run: `cd D:\pyworks\AionsHome\aion-chat && python -m pytest tests/test_analyzer.py -v`
Expected: All tests PASS.

- [x] **Step 5: Commit**

```
git add aion-chat/analyzer.py aion-chat/tests/test_analyzer.py
git commit -m "feat: analyzer 三条规则 + should_wake 入口"
```

---

### Task 3: sensor.py — 集成分析层 + 修改 sentinel prompt

**Files:**
- Modify: `aion-chat/sensor.py:308-340` (sentinel prompt)
- Modify: `aion-chat/sensor.py:376-377` (call_core 分支)

- [x] **Step 1: Modify sentinel prompt — 去掉 call_core 判断指引**

In `aion-chat/sensor.py`, replace lines 329-340 (the JSON template + field descriptions + call_core判断依据):

**Old** (sensor.py lines 329-340):
```python
请严格按照以下JSON格式回复，不要包含其他任何内容：
{{"monitoringlog":"根据传感器数据分析{user_name}当前的状态和活动。例如：{user_name}在家，手机亮屏刷了会小红书，之后放下手机没有活动。今天步数3420步。","summary":"综合分析{user_name}这段时间的整体状况，一两句话即可。","call_core":false,"core_reason":""}}

字段说明：
- monitoringlog: 基于传感器数据的客观记录，只写事实，禁止推测情绪或心理状态
- summary: 综合最近的状态变化和关键事件，一两句话
- call_core: 是否唤醒主脑主动联系{user_name}
- core_reason: 仅当call_core为true时填写，限一句话

call_core判断依据（默认false，只有明确理由才设true）：
- false: {user_name}正常使用手机 / 夜间在睡觉 / 前不久才发过消息 / 没有显著变化
- true: 地理围栏变化且{ai_name}还不知道 / 超过2小时无任何活动需关心 / 深夜2点后仍活跃"""
```

**New:**
```python
请严格按照以下JSON格式回复，不要包含其他任何内容：
{{"monitoringlog":"根据传感器数据分析{user_name}当前的状态和活动。例如：{user_name}在家，手机亮屏刷了会小红书，之后放下手机没有活动。今天步数3420步。","summary":"综合分析{user_name}这段时间的整体状况，一两句话即可。","call_core":false,"core_reason":""}}

字段说明：
- monitoringlog: 基于传感器数据的客观记录，只写事实，禁止推测情绪或心理状态
- summary: 综合最近的状态变化和关键事件，一两句话
- call_core: 始终填 false（唤醒判断由系统另行处理）
- core_reason: 始终留空"""
```

- [x] **Step 2: Modify _analyze_events — 替换 call_core 分支**

In `aion-chat/sensor.py`, replace lines 376-377:

**Old:**
```python
    if call_core:
        await _call_core_sensor(monitoring_log, last_user_ts, summary, core_reason, recent_logs)
```

**New:**
```python
    if SETTINGS.get("analyzer_enabled", False):
        from analyzer import should_wake
        result = should_wake(log_entry, recent_logs)
        if result["wake"]:
            log_entry["call_core"] = True
            log_entry["core_reason"] = f"[{result['rule']}] {result['reason']}"
            await _call_core_sensor(monitoring_log, last_user_ts, summary, result["reason"], recent_logs)
    else:
        if call_core:
            await _call_core_sensor(monitoring_log, last_user_ts, summary, core_reason, recent_logs)
```

- [x] **Step 3: Verify module loads without errors**

Run: `cd D:\pyworks\AionsHome\aion-chat && python -c "from analyzer import should_wake; print('OK')"`
Expected: `OK`

- [x] **Step 4: Run all analyzer tests to make sure nothing broke**

Run: `cd D:\pyworks\AionsHome\aion-chat && python -m pytest tests/test_analyzer.py -v`
Expected: All tests PASS.

- [x] **Step 5: Commit**

```
git add aion-chat/sensor.py
git commit -m "feat: 集成分析层开关，sentinel 不再判断 call_core"
```

---

### Task 4: 手动验证 + 开关测试

- [x] **Step 1: Verify analyzer_enabled=false preserves old behavior**

检查 `aion-chat/data/settings.json` 不含 `analyzer_enabled`（或为 false），确认走原来的 `if call_core:` 分支。

Run: `cd D:\pyworks\AionsHome\aion-chat && python -c "from config import SETTINGS; print('analyzer_enabled:', SETTINGS.get('analyzer_enabled', False))"`
Expected: `analyzer_enabled: False`

- [x] **Step 2: Enable analyzer and test with a synthetic log entry**

```python
# 在 aion-chat/ 目录运行：
python -c "
from analyzer import should_wake, _cooldowns
_cooldowns.clear()

entry = {
    'timestamp': 1779282000,
    'time': '02:30:00',
    'date': '2026-05-20',
    'monitoringlog': '📱 K在刷小红书和B站',
    'summary': '', 'call_core': False, 'core_reason': '', 'source': 'sensor',
}
result = should_wake(entry, [])
print(f'wake={result[\"wake\"]}, rule={result[\"rule\"]}, reason={result[\"reason\"]}')
assert result['wake'] and result['rule'] == 'late_night_leisure'
print('PASS: 深夜消遣触发')

_cooldowns.clear()
entry['time'] = '10:00:00'
entry['monitoringlog'] = '📱 K在刷小红书'
result = should_wake(entry, [])
print(f'wake={result[\"wake\"]}')
assert not result['wake']
print('PASS: 白天不触发')
print('All manual checks passed.')
"
```

Expected: All assertions pass, prints "All manual checks passed."

- [x] **Step 3: Run full test suite one final time**

Run: `cd D:\pyworks\AionsHome\aion-chat && python -m pytest tests/ -v`
Expected: All tests PASS.

- [x] **Step 4: Final commit (if any adjustments were needed)**

Only if previous steps required fixes. Otherwise skip.
