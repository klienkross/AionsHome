# 智能唤醒分析层（analyzer）设计文档

## 背景

当前 sentinel（qwen-flash）每 15 分钟分析传感器事件，同时负责写报告和判断 `call_core`（是否唤醒 bot）。问题是 sentinel 的判断能力不足——产生过"未携带杯套"之类的离谱唤醒理由，同时每次判断都消耗 token。

## 目标

插入一个本地分析层，替代 sentinel 的 call_core 判断。sentinel 只负责写报告，分析层用纯 Python 规则决定是否唤醒 bot。零 token 开销。

## 架构

```
sensor events → 15min窗口 → sentinel(qwen-flash，只写报告)
                                    ↓
                            append_monitor_log
                                    ↓
                    analyzer.should_wake(log_entry, recent_logs)
                                    ↓
                        {wake: false} → 结束
                        {wake: true, rule: "late_night_leisure", reason: "凌晨2点还在刷小红书"}
                                    ↓
                    _call_core_sensor(..., core_reason=reason)
```

### 介入方式：嵌入式（替代 sentinel 的 call_core）

- sentinel prompt 去掉 call_core 判断指引，始终返回 `call_core: false`
- `sensor.py` 的 `_analyze_events()` 末尾加开关
- `SETTINGS["analyzer_enabled"]` 控制开关，默认 false，关掉回到原来 sentinel 判断

## 规则设计

三条独立规则，各自有冷却，互不干扰，任一触发即 wake。

### 规则 1：`late_night_leisure`（深夜刷消遣 app）

- **时间窗口**：1:00 ~ 5:00
- **判定**：monitoringlog 中出现消遣 app 且没有工作 app → 触发
  - 消遣+工作同时出现 → 不触发（给写代码时摸手机留余地）
- **冷却**：8 小时（每晚最多 1 次）

### 规则 2：`late_night_active`（深夜纯活跃）

- **时间窗口**：2:00 ~ 5:00（比规则 1 晚 1 小时，升级提醒）
- **判定**：该时段有任何亮屏/app 活动 → 触发
- 与规则 1 不互斥——1 点触发规则 1，2 点还在就触发规则 2
- **冷却**：8 小时（每晚最多 1 次）

### 规则 3：`prolonged_screen_time`（久坐不动）

- **判定**：最近连续 8 条 monitor_log（约 2 小时）每条都有亮屏/app 活动
- **排除**：0:00 ~ 8:00 不触发（深夜有专门规则）
- **冷却**：3 小时
- **未来扩展**：有步数数据后加"步数长期不变"辅助判据

## 配置

```python
ANALYZER_CONFIG = {
    "late_night_leisure": {
        "window": (1, 5),          # 1:00~5:00
        "leisure_apps": {"小红书", "B站", "QQ", "微信"},
        "work_apps": {"VS Code", "终端", "DeepSeek", "Obsidian", "Edge", "钉钉PC"},
        "cooldown": 8 * 3600,
    },
    "late_night_active": {
        "window": (2, 5),          # 2:00~5:00
        "cooldown": 8 * 3600,
    },
    "prolonged_screen_time": {
        "min_consecutive": 8,      # 8×15min = 2小时
        "exclude_hours": (0, 8),
        "cooldown": 3 * 3600,
    },
}
```

## 接口

```python
def should_wake(log_entry: dict, recent_logs: list[dict]) -> dict:
    """
    输入：当前 monitor_log 条目 + 最近 6 小时的历史条目
    输出：{"wake": bool, "rule": str, "reason": str}
    """
```

## App 提取

从 monitoringlog 文本中用子串匹配提取已知 app 名称（sentinel 报告格式固定）：

```python
KNOWN_APPS = {
    "小红书", "B站", "QQ", "微信", "美团", "DeepSeek",
    "VS Code", "Obsidian", "Edge", "终端", "钉钉",
    "Aion", "MacroDroid", "Tailscale", "网易云音乐",
    "人升", "玻尔", "钉钉PC",
}
```

活跃度判断：检查 monitoringlog 中是否出现任何已知 app 或"亮屏"/"前台"等关键词。

## 冷却机制

内存 dict `{rule_name: last_trigger_timestamp}`，进程重启清零。不持久化。

## 改动范围

| 文件 | 改动 |
|------|------|
| 新建 `analyzer.py` | 规则引擎 ~100 行，纯 Python 无外部依赖 |
| `sensor.py` | `_analyze_events()` 末尾加开关 ~10 行 |
| `sensor.py` | sentinel prompt 去掉 call_core 判断指引 |

## 拆装

`SETTINGS["analyzer_enabled"]` 开关：
- `true`：分析层接管 call_core 判断
- `false`（默认）：回到原来 sentinel 自己判断的逻辑
