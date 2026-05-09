# ntfy.sh 传感器数据中转桥接

**日期**：2026-05-09
**状态**：设计完成

## 背景

MacroDroid 通过 Tailscale VPN 直连服务端 webhook 时遭遇 ConnectException，根因未定位（防火墙配置文件 / MacroDroid 网络栈兼容性）。需要一条不依赖内网直连的备用通道。

## 方案

使用 ntfy.sh 作为公网消息中转：

```
MacroDroid → POST JSON → ntfy.sh/<topic> ← SSE 长连接 ← AionsHome 服务端
                                                            ↓
                                                     handle_sensor_event()
                                                     (现有管道，零改动)
```

## 数据流

### MacroDroid 端

POST URL 从 `http://100.123.108.28:8080/api/webhooks/sensor?token=xxx` 改为 `https://ntfy.sh/<topic>`。

Body 格式不变：

```json
{"event": "screen", "data": {"state": "on"}}
```

### 服务端

新增 `ntfy_bridge.py` 模块，职责：

1. 服务启动时通过 `ntfy_bridge.start(event_loop)` 启动后台任务
2. SSE 长连接订阅 `https://ntfy.sh/<topic>/sse`
3. 收到消息后解析 JSON，校验有 `event` 字段，调用 `handle_sensor_event(payload)`
4. 连接断开时指数退避重连（10s → 20s → 40s → 上限 60s），恢复后重置

### 集成点

- `main.py`：启动时加一行 `ntfy_bridge.start(loop)`
- 现有 `/api/webhooks/sensor` 端点保留不动，两条通道并存

## 配置

在 `data/settings.json` 中新增：

```json
{
  "ntfy_topic": "aions-sensor-<12位随机hex>",
  "ntfy_enabled": true
}
```

- `ntfy_topic`：首次启用时为空则自动生成随机 topic 名
- `ntfy_enabled`：开关，false 时不启动订阅

## 消息校验

不做事件类型白名单。只拒绝：

- 不是合法 JSON
- 解析后没有 `event` 字段

任何合法事件都交给 `sensor.py` 处理，其 `_to_activity_entry()` 已有 else 兜底。未来加新事件类型只改 sensor.py，bridge 不用动。

## API

新增 `GET /api/webhooks/ntfy-status` 返回：

```json
{
  "enabled": true,
  "topic": "aions-sensor-a7f3k9x2m",
  "connected": true
}
```

用于调试，不改前端 UI。

## 文件清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `aion-chat/ntfy_bridge.py` | 新增 | SSE 订阅 + 重连 + 消息分发，约 60 行 |
| `aion-chat/main.py` | 改 | 启动时调用 `ntfy_bridge.start()` |
| `aion-chat/routes/webhooks.py` | 改 | 新增 `/ntfy-status` 端点 |
