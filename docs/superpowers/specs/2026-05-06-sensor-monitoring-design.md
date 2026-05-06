# 传感器事件驱动环境感知系统

## 概述

通过 MacroDroid webhook 推送手机传感器数据（地理围栏、步数、亮屏、app 使用等），新建 `sensor.py` 模块做事件累积和 Sentinel 分析，补充/替代摄像头监控，实现无摄像头的用户状态感知。

## 数据流

```
MacroDroid ──POST──→ /api/webhooks/sensor
                            │
                            ▼
                      sensor.py
                   ┌─────────────┐
                   │  事件分类    │
                   └──┬──────┬───┘
              高优(围栏) │    │ 低优(亮屏/步数/充电...)
                        │    │
                        ▼    ▼
              立刻分析   累积窗口(15min)
                        │    │
                        └──┬─┘
                           ▼
              Sentinel 分析(文本 / 文本+摄像头帧)
                           │
                  ┌────────┼────────┐
                  ▼        ▼        ▼
            monitor_log  location   call_core?
            (统一日志)   _status    (唤醒AI)
```

## 事件格式

MacroDroid 统一推送 JSON：

```json
{
  "event": "geofence",
  "data": { "zone": "home", "action": "enter" },
  "ts": 1746528000
}
```

### 事件类型

| event | data 示例 | 优先级 | 说明 |
|-------|----------|--------|------|
| `geofence` | `{"zone":"home","action":"enter/exit"}` | 高优 → 立刻分析 | 地理围栏进出 |
| `screen` | `{"state":"on/off"}` | 低优 → 进窗口 | 亮屏/灭屏 |
| `app` | `{"package":"com.tencent.mm","name":"微信"}` | 低优 → 进窗口 | 前台 app 切换 |
| `steps` | `{"count":3420}` | 低优 → 进窗口 | 当前步数（定时推） |
| `charging` | `{"state":"on/off"}` | 低优 → 进窗口 | 充电状态 |
| `battery` | `{"level":45}` | 低优 → 进窗口 | 电量（可选） |
| `ringer` | `{"mode":"silent/vibrate/normal"}` | 低优 → 进窗口 | 响铃模式（可选） |

`ts` 为 MacroDroid 端 unix 时间戳，未传则用服务端接收时间。

地理围栏 `zone` 名由用户在 MacroDroid 自行定义（如 `home`、`office`、`gym`），服务端透传给 Sentinel。

## 累积窗口机制

`sensor.py` 维护一个事件缓冲区：

1. 收到低优事件 → 追加到 `_buffer`
2. 如果窗口计时器未启动 → 启动 15 分钟计时
3. 计时到 → 快照并清空 `_buffer`，打包送 Sentinel 分析
4. 回到空闲，等下一个事件重新开窗

完全无事件时不触发分析（如睡觉时手机静默），避免浪费 Sentinel 调用。

## 高优事件（地理围栏）处理

地理围栏事件不进窗口，立刻处理：

1. 更新 `location_status.json` 的 `state` 字段（`at_home` / `at_gym` / `at_office` / 自定义 zone 名）和 `state_changed_at`
2. 把当前窗口里已有的低优事件一起带上分析，然后清空窗口重新计时
3. 调 Sentinel 分析，判断是否唤醒 Core
4. 写 monitor_log + WebSocket 广播

不调高德逆地理编码，围栏本身带语义地点名。只更新 state 相关字段，不碰 address、weather、POI。

## Sentinel 分析

### 输入

分析时打包以下上下文给 Sentinel：

- 时间窗口内的传感器事件列表
- 当前位置状态（围栏 zone）
- 摄像头帧（如果 CameraMonitor 在运行则附图，否则纯文本）
- 现有上下文：近期聊天记录、chat_status、设备活动摘要、monitor_log 历史

### 输出

格式与摄像头分析一致：

```json
{
  "monitoringlog": "14:00-14:15 期间，用户在家刷了会小红书和微信，之后放下手机。步数增加280步。",
  "summary": "下午一直在家，活动量不大。",
  "call_core": false,
  "core_reason": ""
}
```

结果写入现有 monitor_log 体系，前端 `monitor-logs.html` 无需改动即可展示。

## 与摄像头监控的关系

- **补充而非替代**：摄像头开着就是"传感器 + 画面"，没开就纯传感器
- **各跑各的**：摄像头 10-20 分钟定时分析，传感器 15 分钟窗口分析，互不干扰
- **共享日志**：都写 monitor_log，Sentinel 能看到对方的历史，不会重复唤醒 Core

## 与现有位置系统的关系

- `location.py` 不删不重构，GPS 心跳那套保留
- 地理围栏更新 `location_status.json` 的 state 字段，GPS 心跳更新坐标和地址字段，各写各的不冲突
- 现有位置系统 `enabled` 开关关闭时，围栏推的 state 仍然生效

## 文件改动范围

| 文件 | 改动 |
|------|------|
| `aion-chat/sensor.py` | **新建** — 事件接收、窗口累积、Sentinel 分析、位置状态更新 |
| `aion-chat/routes/webhooks.py` | `_HANDLERS` 注册 `"sensor"` channel |
| `aion-chat/location.py` | `format_location_for_prompt()` 小改：位置系统关闭时仍输出围栏 state；state_label 映射支持自定义 zone 名 |

不碰：`camera.py`、`sentinel.py`、`webhook_ai.py`、前端页面。

## MacroDroid 配置参考

服务端地址假设为 `http://<SERVER>:8000`，token 在设置页配置。

所有 macro 的 HTTP Request 动作统一设置：
- **Method**: POST
- **URL**: `http://<SERVER>:8000/api/webhooks/sensor?token=<YOUR_TOKEN>`
- **Content-Type**: application/json

### 地理围栏（高优）

每个地点建两个 macro（进入 + 离开），或用一个 macro + 条件判断。

**Macro: 到家**
- 触发器: Location Trigger → Geofence → 选择家的位置 → Enter
- HTTP Body: `{"event":"geofence","data":{"zone":"home","action":"enter"}}`

**Macro: 离家**
- 触发器: Location Trigger → Geofence → 选择家的位置 → Exit
- HTTP Body: `{"event":"geofence","data":{"zone":"home","action":"exit"}}`

其他地点同理，改 `zone` 名即可（如 `office`、`gym`、`school`）。

### 亮屏/灭屏

**Macro: 亮屏**
- 触发器: Device Events → Screen On
- HTTP Body: `{"event":"screen","data":{"state":"on"}}`

**Macro: 灭屏**
- 触发器: Device Events → Screen Off
- HTTP Body: `{"event":"screen","data":{"state":"off"}}`

### 前台 App 切换

**Macro: App 切换**
- 触发器: Application → App Opened/Closed → 选择要监控的 app（或全部）
- HTTP Body: `{"event":"app","data":{"package":"[package_name]","name":"[app_name]"}}`
- 可用 MacroDroid 内置变量 `{trigger_app_package}` 和 `{trigger_app_name}` 自动填充

### 步数（定时推送）

**Macro: 步数上报**
- 触发器: Timer → Regular Interval → 每 30 分钟
- 动作1: Set Variable → 用 Shell Script `cat /sys/class/misc/step_counter/step_count` 或 MacroDroid 健康插件获取步数
- HTTP Body: `{"event":"steps","data":{"count":[step_count]}}`

### 充电状态

**Macro: 开始充电**
- 触发器: Battery/Power → Power Connected
- HTTP Body: `{"event":"charging","data":{"state":"on"}}`

**Macro: 停止充电**
- 触发器: Battery/Power → Power Disconnected
- HTTP Body: `{"event":"charging","data":{"state":"off"}}`
