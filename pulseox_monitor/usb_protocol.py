# =============================================================================
# USB 协议常量与行过滤 —— 纯 Python，无 PySide6 依赖。
#
# 此模块可被 usb_handler.py（需要 PySide6）和测试（无需 PySide6）同时导入。
# =============================================================================

from __future__ import annotations

import json

# ---- USB 生命周期命令 ----
CMD_USB_START = "GUI_USB_START"
CMD_USB_STOP = "GUI_USB_STOP"
CMD_USB_PING = "GUI_USB_PING"

# ---- 默认参数 ----
DEFAULT_BAUDRATE = 115200
DEFAULT_PING_INTERVAL_MS = 5000

# ---- ESP32 日志行前缀（需过滤） ----
# ESP-IDF 日志格式:  <级别字母> (<时间戳>) <标签>: <消息>
# 例: I (12345) wifi: connected
_ESP_LOG_PREFIXES = frozenset({"I", "W", "E", "D", "V"})


def is_data_line(line: str) -> bool:
    """判断一行文本是否为有效数据行（JSON 或 CSV），而非 ESP_LOG 噪声。

    过滤规则：
      1. 空白行 → 跳过。
      2. 以 '{' 或 '[' 开头 → JSON 数据行，保留。
      3. 以 "M," 开头 → STM32 raw CSV 行，保留。
      4. 以 ESP_LOG 级别字母（I/W/E/D/V）开头且紧跟空格 → 日志噪声，丢弃。
      5. 其他 → 尝试 JSON 解析，成功则保留，失败则丢弃。

    参数：
      line: 从串口读取的单行文本（应已 strip 首尾空白）。

    返回：
      True 表示该行应传递给 MessageDispatcher。
    """
    if not line:
        return False

    first_char = line[0]

    # JSON 对象 / 数组
    if first_char in ("{", "["):
        return True

    # STM32 raw CSV
    if line.startswith("M,"):
        return True

    # ESP_LOG 模式: 单个字母 + 空格 + 括号
    # 例如 "I (12345) wifi: connected"
    if first_char in _ESP_LOG_PREFIXES and len(line) > 2 and line[1] == " ":
        return False

    # 未识别的行：尝试 JSON 解析，成功则保留
    try:
        json.loads(line)
        return True
    except (json.JSONDecodeError, ValueError):
        return False
