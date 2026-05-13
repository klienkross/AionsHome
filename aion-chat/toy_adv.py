"""
BLE 广播玩具控制桥接模块
调用 ToyController 的 ble_core 构建 BLE 广播 payload
"""

import sys, logging
from pathlib import Path

logger = logging.getLogger("toy_adv")

# ToyController 路径（与 AionsHome 同级）
_TC_DIR = str(Path(__file__).resolve().parent.parent.parent / "ToyController")
if _TC_DIR not in sys.path:
    sys.path.insert(0, _TC_DIR)

try:
    from ble_core import pack_command, DeviceType
    _available = True
except ImportError:
    logger.warning("[toy_adv] ToyController 未找到，广播功能不可用")
    _available = False

# mode 和 speed 可能互斥（待实机验证）：
#   mode > 0 时设备按波形预设运行，speed 可能被忽略
#   mode = 0 时设备稳定振动，speed 控制强度
# 两个值都存，实现时根据验证结果决定发哪个
DEFAULT_PRESET_MAP = [
    None,
    {"mode": 1, "speed": 1},       # 1: 微风轻拂
    {"mode": 2, "speed": 2},       # 2: 春水初生
    {"mode": 3, "speed": 3},       # 3: 暗流涌动
    {"mode": 9, "speed": 4},       # 4: 如梦似幻
    {"mode": 4, "speed": 5},       # 5: 情潮渐涨
    {"mode": 7, "speed": 6},       # 6: 烈焰焚身
    {"mode": 8, "speed": 7},       # 7: 极乐之巅
    {"mode": 16, "speed": 8},      # 8: 魂飞魄散
    {"mode": 19, "speed": 9},      # 9: 失控
]


def is_available() -> bool:
    return _available


def get_preset_map(settings: dict) -> list:
    return settings.get("toy_adv_presets", DEFAULT_PRESET_MAP)


def build_for_preset(preset_num: int, settings: dict) -> str | None:
    """预设编号(1-9) → payload hex 字符串。preset_num=0 表示 STOP。"""
    if not _available:
        return None

    model = settings.get("toy_adv_model", "K134")
    channel = settings.get("toy_adv_channel", 37)
    preset_map = get_preset_map(settings)

    if preset_num == 0:
        return pack_command(model, 0, 0, channel=channel).hex()

    if preset_num < 1 or preset_num >= len(preset_map) or preset_map[preset_num] is None:
        logger.warning(f"[toy_adv] 无效预设编号: {preset_num}")
        return None

    entry = preset_map[preset_num]
    mode = entry.get("mode", 0)
    speed = entry.get("speed", 0)
    return pack_command(model, mode, speed, channel=channel).hex()


def build_direct(mode: int, speed: int, settings: dict, speed_b: int = 0) -> str | None:
    """扩展格式：直接指定 mode + speed → payload hex。"""
    if not _available:
        return None

    model = settings.get("toy_adv_model", "K134")
    channel = settings.get("toy_adv_channel", 37)
    if speed_b:
        return pack_command(model, mode, speed, speed_b, channel=channel).hex()
    return pack_command(model, mode, speed, channel=channel).hex()
