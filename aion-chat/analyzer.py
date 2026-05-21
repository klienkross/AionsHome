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
