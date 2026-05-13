# 密语时刻：BLE 广播设备集成设计

## 概述

将 ToyController（BLE 广播类玩具控制）集成到 AionsHome 的密语时刻面板中，作为第二种连接方式与现有 GATT 设备（SOSEXY）共存。

**方案**：A1——后端用 `ble_core.py` 构建 payload，通过 WebSocket 推送给前端，前端调用 Android APK 的 `BleBridge.sendAdvertise()` 发射 BLE 广播。

## 数据流

```
AI 回复包含 [TOY:x] 指令
    │
    ▼  后端 chat.py 正则提取
toy_matches
    │
    ├─ GATT 模式（现有，不改动）:
    │    WebSocket 推送 { type: "toy_command", commands: [...] }
    │    → 前端 toyExecCmd() → toySendData2() → BLE GATT 写入
    │
    └─ 广播模式（新增）:
         后端 toy_adv.build_for_preset(preset_num)
           → ble_core.pack_command(model, mode, speed, channel)
           → payloadHex
         WebSocket 推送 { type: "toy_adv", payloadHex, preset }
           → 前端 AionBle.sendAdvertise(payloadHex, 2000)
           → Android BluetoothLeAdvertiser 发射 BLE 广播
```

两种模式互斥，由用户在密语时刻面板中切换。后端根据 `toy_adv_enabled` 决定推送哪种事件：开启时只推 `toy_adv`，关闭时只推 `toy_command`（现有行为）。不会同时推两种。

## 指令格式

### 简单格式（现有，向后兼容）

```
[TOY:1]~[TOY:9]   预设档位（1最温柔，9最强烈）
[TOY:STOP]         停止
```

后端根据预设配置表映射为 mode + speed。

### 扩展格式（新增，可选）

```
[TOY:mode=3,speed=7]             直接指定波形和强度
[TOY:mode=8,speed=5,speed_b=3]  双马达设备
```

后端正则同时匹配两种格式。扩展格式优先直接用参数，简单格式查配置表。

### 正则更新

现有：`\[TOY:(\d|STOP)\]`

新增：`\[TOY:((?:\d|STOP)|(?:mode=\d+[^]]*?))\]`

后端解析时判断匹配内容是纯数字/STOP 还是 key=value 格式，分别处理。

## 后端改动

### 新增 `aion-chat/toy_adv.py`

职责：
1. 管理广播设备配置（model、channel）
2. 管理预设映射表（预设编号 → mode + speed）
3. 构建 payload hex

```python
import sys, json
from pathlib import Path
sys.path.insert(0, "D:/pyworks/ToyController")
from ble_core import pack_command

# mode 和 speed 可能互斥（待实机验证）：
#   - mode > 0 时设备按波形预设运行，speed 可能被忽略
#   - mode = 0 时设备稳定振动，speed 控制强度
# 预设表同时存两个值，build 时根据验证结果决定发哪个
DEFAULT_PRESET_MAP = [
    None,                              # 0: 占位
    {"mode": 1, "speed": 1},           # 1: 微风轻拂
    {"mode": 2, "speed": 2},           # 2: 春水初生
    {"mode": 3, "speed": 3},           # 3: 暗流涌动
    {"mode": 9, "speed": 4},           # 4: 如梦似幻
    {"mode": 4, "speed": 5},           # 5: 情潮渐涨
    {"mode": 7, "speed": 6},           # 6: 烈焰焚身
    {"mode": 8, "speed": 7},           # 7: 极乐之巅
    {"mode": 16, "speed": 8},          # 8: 魂飞魄散
    {"mode": 19, "speed": 9},          # 9: 失控
]

def build_for_preset(preset_num: int, settings: dict) -> str | None:
    """预设编号 → payload hex。返回 None 表示 STOP。"""
    model = settings.get("toy_adv_model", "K134")
    channel = settings.get("toy_adv_channel", 37)
    preset_map = settings.get("toy_adv_presets", DEFAULT_PRESET_MAP)

    if preset_num == 0:  # STOP
        return pack_command(model, 0, 0, channel=channel).hex()

    entry = preset_map[preset_num]
    return pack_command(model, entry["mode"], entry["speed"], channel=channel).hex()

def build_direct(mode: int, speed: int, speed_b: int = 0, settings: dict) -> str:
    """扩展格式 → payload hex。"""
    model = settings.get("toy_adv_model", "K134")
    channel = settings.get("toy_adv_channel", 37)
    if speed_b:
        return pack_command(model, mode, speed, speed_b, channel=channel).hex()
    return pack_command(model, mode, speed, channel=channel).hex()
```

### `aion-chat/routes/chat.py` 改动

1. **正则更新**：扩展 `TOY_CMD_PATTERN` 支持两种格式
2. **toy_matches 处理**：在现有推送 `toy_command` 之后，如果 `toy_adv_enabled`，额外构建并推送 `toy_adv` 事件
3. **AI prompt**：`whisper_mode` 开启且 `toy_adv_enabled` 时，在 abilities 中追加扩展格式说明和可用波形列表

### 配置存储

在现有 `settings.json` 中新增字段：

```json
{
  "toy_adv_enabled": false,
  "toy_adv_model": "K134",
  "toy_adv_channel": 37,
  "toy_adv_presets": [
    null,
    {"mode": 1, "speed": 1},
    {"mode": 2, "speed": 2},
    {"mode": 3, "speed": 3},
    {"mode": 9, "speed": 4},
    {"mode": 4, "speed": 5},
    {"mode": 7, "speed": 6},
    {"mode": 8, "speed": 7},
    {"mode": 16, "speed": 8},
    {"mode": 19, "speed": 9}
  ]
}
```

### 新增 API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/toy-adv/devices` | 返回 ToyController 支持的 BLE 设备列表（调用 `list_devices`） |
| GET | `/api/toy-adv/waveforms` | 返回可用波形列表（调用 `list_waveforms`） |
| GET | `/api/toy-adv/config` | 返回当前广播设备配置 |
| POST | `/api/toy-adv/config` | 更新广播设备配置（model、channel、presets） |
| POST | `/api/toy-adv/test` | 手动发送一次测试广播 |

## 前端改动

### 密语时刻面板（`chat.html` + `chat.js`）

在现有面板顶部加设备类型切换：

```html
<!-- 设备类型切换 -->
<div class="whisper-device-type">
  <button class="active" id="toyTypeGatt" onclick="toySetType('gatt')">直连</button>
  <button id="toyTypeAdv" onclick="toySetType('adv')">广播</button>
</div>
```

**GATT 模式**：显示现有的连接按钮和逻辑，不改动。

**广播模式**：替换连接区域为配置区：
- 设备型号下拉（从 `/api/toy-adv/devices` 拉列表）
- 信道选择（37/38/39）
- 连接测试按钮（调 `/api/toy-adv/test`）
- 保存按钮（调 POST `/api/toy-adv/config`）

**预设网格**：共用现有 9 宫格 UI，但广播模式下：
- 点击预设 → 调 POST `/api/toy-adv/test` 发送对应预设
- 停止按钮 → 发送 STOP

**预设映射编辑器**（新增弹窗）：
- 每档显示 mode 下拉（20 种波形）+ speed 滑块（0-9）
- 保存到后端配置

**WebSocket 事件处理**：

```javascript
} else if (data.type === "toy_adv") {
  if (window.AionBle && window.AionBle.sendAdvertise) {
    window.AionBle.sendAdvertise(data.payloadHex, 2000);
    toyLog('📡 广播 → 预设' + data.preset, 'wl-send');
  }
}
```

### 设备类型持久化

`localStorage.setItem('toy_device_type', 'gatt' | 'adv')`

## Android APK 改动

### `BleBridge.java`

新增方法：

```java
@JavascriptInterface
public void sendAdvertise(String payloadHex, int durationMs) {
    BluetoothLeAdvertiser advertiser = adapter.getBluetoothLeAdvertiser();
    if (advertiser == null) {
        callJs("toyNativeBle.onError('此设备不支持BLE广播')");
        return;
    }

    byte[] payload = hexToBytes(payloadHex);

    AdvertiseSettings settings = new AdvertiseSettings.Builder()
        .setAdvertiseMode(AdvertiseSettings.ADVERTISE_MODE_LOW_LATENCY)
        .setTxPowerLevel(AdvertiseSettings.ADVERTISE_TX_POWER_HIGH)
        .setConnectable(false)
        .setTimeout(durationMs)
        .build();

    AdvertiseData data = new AdvertiseData.Builder()
        .setIncludeDeviceName(false)
        .addManufacturerData(0xFFFF, payload)
        .build();

    advertiser.startAdvertising(settings, data, advCallback);
}
```

### 权限

`AndroidManifest.xml` 中确认声明 `BLUETOOTH_ADVERTISE`（Android 12+）。

## 测试计划

1. **后端单元测试**：`toy_adv.build_for_preset()` 对所有预设编号生成正确 payload
2. **API 测试**：`/api/toy-adv/devices`、`/api/toy-adv/config` 正常返回和保存
3. **前端测试**：面板切换 GATT/广播模式，配置保存和加载
4. **端到端测试**：手机 APK 上 AI 发 `[TOY:3]` → 广播发射 → 设备响应
5. **扩展格式测试**：`[TOY:mode=3,speed=7]` 正确解析和发送

## 已知限制

- BLE 广播需要手机端 APK，PC 浏览器无法发射（适配器不支持 Peripheral 模式）
- 广播是单向的，无法确认设备是否收到
- 广播距离有限（通常 < 10m）
