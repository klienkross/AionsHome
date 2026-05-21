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
        assert result2["rule"] != "late_night_leisure"

    def test_different_rules_not_blocked(self):
        entry1 = _make_log("01:30:00", "📱 K在刷小红书")
        result1 = should_wake(entry1, [])
        assert result1["wake"] is True
        assert result1["rule"] == "late_night_leisure"

        entry2 = _make_log("03:00:00", "📱 K手机亮屏在刷B站")
        result2 = should_wake(entry2, [])
        assert result2["wake"] is True
        assert result2["rule"] == "late_night_active"
