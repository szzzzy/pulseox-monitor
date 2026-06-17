# =============================================================================
# FlexibleMessage —— MQTT 上行消息的灵活解析模型。
#
# 设计目标：
#   1. 容忍字段缺失 —— 旧 ESP32（81 字段）或新 ESP32（102 字段）JSON 都能解析。
#   2. 容忍非法值 —— "--", "N/A", 空字符串等哨兵值自动转换为 None/NaN。
#   3. 永不抛异常 —— 所有类型转换函数（_safe_*）在无法转换时返回 None 或默认值。
#   4. Schema 感知 —— 通过 schema_version / field_count / parse_warnings 检测不兼容的旧数据。
#   5. 保留原始上下文 —— raw 字典保留完整 JSON，extra_fields 保留未知字段。
#
# 数据流：
#   MQTT JSON/CSV → MessageDispatcher → FlexibleMessage.from_dict() → FlexibleMessage
#                                                                       ├── .bpm, .spo2, ...
#                                                                       ├── .detect_schema_issue()
#                                                                       └── .raw (完整原始数据)
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


class MessageValidationError(ValueError):
    """消息解析校验异常。

    当 dispatcher 遇到不可恢复的输入错误时抛出（非 JSON、非 CSV 等）。
    UI 层捕获此异常后记录日志，不崩溃。
    """
    pass


# =============================================================================
# 安全类型转换函数
#
# 原则：
#   - 永不抛异常。
#   - 遇到无法转换的值时返回 None（数值类型）或 default（布尔类型）。
#   - 先通过 _clean_value() 清洗哨兵值，再进行类型转换。
# =============================================================================

def _clean_value(value: Any) -> Any:
    """清洗哨兵字符串，将其统一转换为 None。

    清洗规则（大小写不敏感）：
      - 空字符串 ""
      - 双击 "--"           (STM32 的缺失值标记)
      - "N/A", "n/a", "NA", "na"
      - "null", "NULL", "None"

    非字符串类型的值原样返回，不做清洗。

    参数：
      value: 任意类型的输入值。

    返回：
      清洗后的值。哨兵字符串返回 None，其他值原样返回。
    """
    if isinstance(value, str):
        stripped = value.strip()
        if stripped in {"", "--", "N/A", "n/a", "NA", "na", "null", "NULL", "None"}:
            return None
        return stripped
    return value


def _safe_bool(value: Any, default: bool = False) -> bool:
    """安全地将任意值转换为布尔类型。

    转换规则：
      - None / 哨兵值 → default
      - bool 类型 → 原值
      - int 类型 0/1 → bool(value)
      - 字符串 "true"/"1"/"yes"/"y" → True
      - 字符串 "false"/"0"/"no"/"n" → False
      - 其他情况 → default

    参数：
      value:   待转换的值。
      default: 无法转换时的默认布尔值。

    返回：
      转换后的布尔值，或 default。
    """
    value = _clean_value(value)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y"}:
            return True
        if normalized in {"false", "0", "no", "n"}:
            return False
    return default


def _safe_optional_bool(value: Any) -> bool | None:
    """安全地将任意值转换为三态布尔值。

    返回 True / False / None，None 表示字段缺失或无法判断。
    用于 ESP32 连接状态这类诊断字段，避免把未知状态误显示为断开。
    """
    value = _clean_value(value)
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
        if normalized in {
            "true", "1", "yes", "y", "on", "connected", "connect",
            "online", "ready", "up", "ok",
        }:
            return True
        if normalized in {
            "false", "0", "no", "n", "off", "disconnected", "disconnect",
            "offline", "not_connected", "down", "fail", "failed",
        }:
            return False
    return None


def _safe_int(value: Any) -> int | None:
    """安全地将任意值转换为 int 类型。

    转换规则：
      - None / 哨兵值 → None
      - bool 类型 → int(value)  (True→1, False→0)
      - int 类型 → 原值
      - float 类型且等于自身取整 → int(value)  (3.0→3, 3.1→None)
      - 字符串 → 尝试 int(text)，失败返回 None
      - 其他类型 → None

    参数：
      value: 待转换的值。

    返回：
      转换后的 int，或 None。
    """
    value = _clean_value(value)
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == int(value):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except (ValueError, TypeError):
            return None
    return None


def _safe_float(value: Any) -> float | None:
    """安全地将任意值转换为 float 类型。

    转换规则：
      - None / 哨兵值 → None
      - int/float 类型（非 bool）→ float(value)
      - 字符串 → 尝试 float(text)，失败返回 None
      - 其他类型 → None

    注意：bool 类型不会被转换为 float（True→1.0 是意外行为）。

    参数：
      value: 待转换的值。

    返回：
      转换后的 float，或 None。
    """
    value = _clean_value(value)
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except (ValueError, TypeError):
            return None
    return None


def _safe_str(value: Any) -> str | None:
    """安全地将任意值转换为字符串。

    转换规则：
      - None / 哨兵值 → None
      - str 类型 → 原值
      - int/float/bool → str(value)
      - 其他类型 → None

    参数：
      value: 待转换的值。

    返回：
      转换后的 str，或 None。
    """
    value = _clean_value(value)
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return None


def parse_device_datetime(date_text: str, time_text: str) -> datetime:
    """解析设备上报的日期和时间字符串为 datetime 对象。

    STM32/ESP32 的日期时间格式：
      date_text: YYYYMMDD (例如 "20260409")
      time_text: HHMMSS   (例如 "120000")

    合并后通过 strptime 解析为 datetime。

    参数：
      date_text: 8 位日期字符串。
      time_text: 6 位时间字符串。

    返回：
      解析后的 datetime 对象。

    异常：
      ValueError —— 字符串格式不正确时抛出。
    """
    return datetime.strptime(f"{date_text}{time_text}", "%Y%m%d%H%M%S")


# =============================================================================
# FlexibleMessage —— 灵活消息模型
#
# 这是整个数据模型的核心类。
# 使用 @dataclass(slots=True) 实现高效内存布局，
# 通过 property 提供类型安全的字段访问。
# =============================================================================

@dataclass(slots=True)
class FlexibleMessage:
    """MQTT 上行消息的灵活数据模型。

    核心字段：
      raw:          原始 JSON/CSV 字典（保留所有字段，包括未知字段）。
      received_at:  PC 端接收时间。
      message_type: 消息类型（measurement / rtc_set_ack / parse_error）。

    缓存头字段：
      bridge, source, channel, protocol, frame —— MQTT 桥接元信息。

    使用方式：
      msg = FlexibleMessage.from_dict(json_payload)
      if msg.message_type == "measurement":
          print(msg.bpm, msg.spo2, msg.signal_quality)
    """

    # ---- 核心数据字段 ----
    raw: dict[str, Any]            # 原始 JSON/CSV 字典，保留所有字段
    received_at: datetime          # PC 端接收时间
    message_type: str              # 消息类型标识

    # ---- MQTT 桥接头信息缓存 ----
    bridge: str | None = None      # 桥接程序标识 (e.g. "gw-01")
    source: str | None = None      # 数据源标识 (e.g. "sensor-a")
    channel: str | None = None     # 通道标识 (e.g. "1")
    protocol: str | None = None    # 传输协议 (e.g. "mqtt", "csv")
    frame: str | None = None       # 帧序号

    # =========================================================================
    # 构造方法
    # =========================================================================

    @staticmethod
    def from_dict(
        payload: dict[str, Any],
        received_at: datetime | None = None,
    ) -> FlexibleMessage:
        """从解析后的 JSON/CSV 字典创建 FlexibleMessage 实例。

        从 payload 中提取公共头字段，其余字段保留在 raw 中供后续访问。

        参数：
          payload:     解析后的 JSON/CSV 字典。
          received_at: PC 端接收时间，None 时使用 datetime.now()。

        返回：
          FlexibleMessage 实例。
        """
        message_type = _safe_str(payload.get("message")) or "unknown"

        return FlexibleMessage(
            raw=payload,
            received_at=received_at or datetime.now(),
            message_type=message_type,
            bridge=_safe_str(payload.get("bridge")),
            source=_safe_str(payload.get("source")),
            channel=_safe_str(payload.get("channel")),
            protocol=_safe_str(payload.get("protocol")),
            frame=_safe_str(payload.get("frame")),
        )

    # =========================================================================
    # 通用嵌套路径访问方法
    # =========================================================================

    def get(self, *path: str, default: Any = None) -> Any:
        """安全遍历嵌套字典路径，返回路径终点的值。

        使用方式：
          msg.get("modules", "bpm", "value")  → 访问 raw["modules"]["bpm"]["value"]

        若路径中任何节点不存在或类型不是 dict，返回 default。
        不会抛出 KeyError 或 TypeError。

        参数：
          *path:   嵌套键序列，例如 ("modules", "bpm", "value")。
          default: 路径不存在时的默认值。

        返回：
          路径终点的值，或 default。
        """
        node: Any = self.raw
        for key in path:
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                return default
        return node

    def get_bool(self, *path: str, default: bool = False) -> bool:
        """按嵌套路径获取值并安全转换为 bool。

        参数：
          *path:   嵌套键序列。
          default: 路径不存在或无法转换时的默认值。

        返回：
          转换后的 bool 值。
        """
        return _safe_bool(self.get(*path), default=default)

    def get_optional_bool(self, *path: str) -> bool | None:
        """按嵌套路径获取值并安全转换为三态 bool。

        返回 True / False / None。None 表示路径不存在或值无法判断。
        """
        if not self.has(*path):
            return None
        return _safe_optional_bool(self.get(*path))

    def get_int(self, *path: str, default: int | None = None) -> int | None:
        """按嵌套路径获取值并安全转换为 int。

        参数：
          *path:   嵌套键序列。
          default: 路径不存在或无法转换时的默认值。

        返回：
          转换后的 int 值，或 None。
        """
        return _safe_int(self.get(*path))

    def get_float(self, *path: str, default: float | None = None) -> float | None:
        """按嵌套路径获取值并安全转换为 float。

        参数：
          *path:   嵌套键序列。
          default: 路径不存在或无法转换时的默认值。

        返回：
          转换后的 float 值，或 None。
        """
        return _safe_float(self.get(*path))

    def get_str(self, *path: str, default: str | None = None) -> str | None:
        """按嵌套路径获取值并安全转换为 str。

        参数：
          *path:   嵌套键序列。
          default: 路径不存在或无法转换时的默认值。

        返回：
          转换后的 str 值，或 None。
        """
        return _safe_str(self.get(*path))

    def has(self, *path: str) -> bool:
        """检查嵌套路径是否存在于 raw 字典中。

        与 get() 不同，此方法只检查存在性，不返回实际值。

        参数：
          *path: 嵌套键序列。

        返回：
          True 表示路径完整存在，False 表示路径中某节点缺失或类型不对。
        """
        node: Any = self.raw
        for key in path:
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                return False
        return True

    def _first_optional_bool(self, paths: tuple[tuple[str, ...], ...]) -> bool | None:
        """按候选路径顺序返回第一个可判断的三态布尔值。"""
        for path in paths:
            value = self.get_optional_bool(*path)
            if value is not None:
                return value
        return None

    def _first_str(self, paths: tuple[tuple[str, ...], ...]) -> str | None:
        """按候选路径顺序返回第一个非空字符串值。"""
        for path in paths:
            if not self.has(*path):
                continue
            value = self.get_str(*path)
            if value:
                return value
        return None

    # =========================================================================
    # modules 子对象快捷访问
    #
    # 部分 ESP32 固件将测量值放在 modules 子对象中：
    #   {"modules": {"bpm": {"available": true, "valid": true, "value": 72}}}
    # =========================================================================

    def module_available(self, module_name: str) -> bool:
        """检查指定模块是否可用。

        访问路径: raw["modules"][module_name]["available"]

        参数：
          module_name: 模块名称 (e.g. "bpm", "spo2")。

        返回：
          模块的 available 布尔值。
        """
        return self.get_bool("modules", module_name, "available", default=False)

    def module_valid(self, module_name: str) -> bool:
        """检查指定模块的数据是否有效。

        访问路径: raw["modules"][module_name]["valid"]

        参数：
          module_name: 模块名称。

        返回：
          模块的 valid 布尔值。
        """
        v = self.get("modules", module_name, "valid")
        return _safe_bool(v, default=False)

    def module_value(self, module_name: str) -> Any:
        """获取指定模块的测量值。

        访问路径: raw["modules"][module_name]["value"]

        参数：
          module_name: 模块名称。

        返回：
          模块的 value（可以是任意类型）。
        """
        return self.get("modules", module_name, "value")

    # =========================================================================
    # 顶层属性 —— RTC / 日期 / 时间
    # =========================================================================

    @property
    def rtc_valid(self) -> bool:
        """设备 RTC 是否有效。

        STM32 RTC 有效时 rtc_valid=True，上报的 date/time 可信。
        RTC 无效时（电池断电、未设置等），使用 PC 接收时间作为回退。
        """
        return self.get_bool("rtc_valid")

    @property
    def date(self) -> str | None:
        """设备日期字符串（格式：YYYYMMDD）。

        仅当 rtc_valid=True 时此值可信。
        """
        return self.get_str("date")

    @property
    def time(self) -> str | None:
        """设备时间字符串（格式：HHMMSS）。

        仅当 rtc_valid=True 时此值可信。
        """
        return self.get_str("time")

    @property
    def device_datetime(self) -> datetime | None:
        """设备端的日期时间（datetime 对象）。

        返回规则：
          - rtc_valid=False → None
          - date 或 time 缺失 → None
          - 格式解析失败 → None

        RTC 无效时应使用 received_at（PC 接收时间）作为替代。
        """
        if not self.rtc_valid:
            return None
        d = self.date
        t = self.time
        if not d or not t:
            return None
        try:
            return parse_device_datetime(d, t)
        except ValueError:
            return None

    # =========================================================================
    # 顶层属性 —— PPG 原始信号
    # =========================================================================

    @property
    def red(self) -> int | None:
        """红光 PPG ADC 原始值。"""
        return self.get_int("red")

    @property
    def ir(self) -> int | None:
        """红外光 PPG ADC 原始值。"""
        return self.get_int("ir")

    @property
    def baseline_ir(self) -> int | None:
        """红外光 PPG 基线 ADC 值。"""
        return self.get_int("baseline_ir")

    @property
    def finger(self) -> bool:
        """手指在位检测。

        True = 手指在位，False = 手指离位。
        False 时所有 PPG 衍生指标（BPM、SpO2 等）可能无效。
        """
        return self.get_bool("finger")

    # =========================================================================
    # 顶层属性 —— 核心生命体征
    # =========================================================================

    @property
    def bpm_valid(self) -> bool:
        """BPM（心率）是否有效。"""
        return self.get_bool("bpm_valid")

    @property
    def bpm(self) -> int | None:
        """心率值（bpm，次/分）。"""
        return self.get_int("bpm")

    @property
    def spo2_valid(self) -> bool:
        """SpO2（血氧饱和度）是否有效。"""
        return self.get_bool("spo2_valid")

    @property
    def spo2(self) -> int | None:
        """血氧饱和度值（%）。"""
        return self.get_int("spo2")

    @property
    def rr_valid(self) -> bool:
        """RR（呼吸率）是否有效。"""
        return self.get_bool("rr_valid")

    @property
    def rr(self) -> int | None:
        """呼吸率值（次/分）。"""
        return self.get_int("rr")

    @property
    def ibi_valid(self) -> bool:
        """IBI（心搏间期）是否有效。"""
        return self.get_bool("ibi_valid")

    @property
    def ibi(self) -> int | None:
        """心搏间期值（ms）。"""
        return self.get_int("ibi")

    # =========================================================================
    # 顶层属性 —— 信号质量
    # =========================================================================

    @property
    def signal_quality(self) -> int | None:
        """信号质量评分（0-100）。

        0 = 极差/无信号，100 = 极佳。
        当前 102 列 schema 中位于列 30。
        """
        return self.get_int("signal_quality")

    @property
    def motion_artifact(self) -> bool:
        """是否检测到运动伪影。

        True 时 BPM/SpO2 等衍生指标可能不可靠。
        绘图时在对应区域叠加橙色背景带。
        """
        return self.get_bool("motion_artifact")

    @property
    def motion_score(self) -> int | None:
        """运动评分（数值越大表示运动干扰越严重）。"""
        return self.get_int("motion_score")

    @property
    def raw_signal_present(self) -> bool | None:
        """原始 PPG 信号是否存在。

        None = 字段缺失（旧固件），True = 有信号，False = 无信号。
        """
        return self.get("raw_signal_present")

    # =========================================================================
    # 顶层属性 —— ESP32 状态上报（esp-status-v1）
    # =========================================================================

    @property
    def esp_online(self) -> bool | None:
        """ESP32 状态消息中的 online 字段。

        None 表示尚未上报或无法判断。GUI 还会结合 lastEspStatusAt 做超时判定。
        """
        return self._first_optional_bool((("online",), ("esp", "online")))

    @property
    def esp_usb_active(self) -> bool | None:
        """ESP32 USB 会话是否 active。

        新协议优先使用 usb.active；兼容 transport.usb_active 与旧字段。
        """
        return self._first_optional_bool((
            ("usb", "active"),
            ("transport", "usb_active"),
            ("esp_usb_active",),
            ("usb_active",),
            ("esp_status", "usb", "active"),
            ("esp_status", "transport", "usb_active"),
        ))

    @property
    def esp_usb_connected(self) -> bool | None:
        """ESP32 USB 物理连接/线缆连接状态。

        新协议优先使用 usb.connected；兼容 transport.usb_connected 与旧字段。
        """
        return self._first_optional_bool((
            ("usb", "connected"),
            ("transport", "usb_connected"),
            ("esp_usb_connected",),
            ("usb_connected",),
            ("usb_online",),
            ("usb_ready",),
            ("usb",),
            ("usb", "is_connected"),
            ("usb", "online"),
            ("usb", "ready"),
            ("esp", "usb_connected"),
            ("esp", "usb_online"),
            ("esp", "usb", "connected"),
            ("esp", "usb", "online"),
            ("esp_status", "usb_connected"),
            ("esp_status", "usb_online"),
            ("esp_status", "usb", "connected"),
            ("esp_status", "usb", "online"),
            ("connections", "usb"),
            ("connections", "usb_connected"),
            ("transport_status", "usb"),
            ("transport_status", "usb_connected"),
        ))

    @property
    def esp_mqtt_connected(self) -> bool | None:
        """ESP32 MQTT 连接状态。新协议优先使用 mqtt.connected。"""
        return self._first_optional_bool((
            ("mqtt", "connected"),
            ("transport", "mqtt_connected"),
            ("esp_mqtt_connected",),
            ("mqtt_connected",),
            ("mqtt_online",),
            ("mqtt_ready",),
            ("mqtt",),
            ("mqtt", "is_connected"),
            ("mqtt", "online"),
            ("mqtt", "ready"),
            ("esp", "mqtt_connected"),
            ("esp", "mqtt_online"),
            ("esp", "mqtt", "connected"),
            ("esp", "mqtt", "online"),
            ("esp_status", "mqtt_connected"),
            ("esp_status", "mqtt_online"),
            ("esp_status", "mqtt", "connected"),
            ("esp_status", "mqtt", "online"),
            ("connections", "mqtt"),
            ("connections", "mqtt_connected"),
            ("transport_status", "mqtt"),
            ("transport_status", "mqtt_connected"),
        ))

    @property
    def esp_mqtt_subscribed(self) -> bool | None:
        """ESP32 是否已订阅 MQTT 命令主题。"""
        return self._first_optional_bool((
            ("mqtt", "subscribed"),
            ("transport", "mqtt_subscribed"),
            ("esp_mqtt_subscribed",),
            ("mqtt_subscribed",),
        ))

    @property
    def esp_wifi_connected(self) -> bool | None:
        """ESP32 WiFi 连接状态。"""
        return self._first_optional_bool((
            ("wifi", "connected"),
            ("esp_wifi_connected",),
            ("wifi_connected",),
        ))

    @property
    def esp_transport_active(self) -> str | None:
        """ESP32 当前上行通道：usb / mqtt / usb_idle / offline 等。"""
        return self._first_str((
            ("transport", "active"),
            ("esp_transport_active",),
            ("transport_active",),
            ("esp_transport_mode",),
            ("transport_mode",),
            ("active_transport",),
            ("activeTransport",),
            ("selected_transport",),
            ("selectedTransport",),
            ("link_mode",),
            ("esp", "transport_mode"),
            ("esp", "active_transport"),
            ("esp_status", "transport", "active"),
            ("esp_status", "transport_mode"),
            ("esp_status", "active_transport"),
        ))

    @property
    def esp_transport_mode(self) -> str | None:
        """向后兼容属性：返回当前上行通道的大写规范化文本。"""
        value = self.esp_transport_active
        if not value:
            return None
        normalized = value.strip().upper().replace("-", "_").replace(" ", "_")
        if normalized in {"MQTT", "WIFI_MQTT"}:
            return "MQTT"
        if normalized in {"USB", "SERIAL", "UART", "USB_SERIAL"}:
            return "USB"
        return normalized

    @property
    def esp_stm32_protocol_state(self) -> str | None:
        """STM32/协议状态，例如 ok/error。"""
        return self.get_str("stm32", "protocol_state")

    @property
    def esp_stm32_last_frame(self) -> str | None:
        """STM32 最近帧类型。"""
        return self.get_str("stm32", "last_frame")

    @property
    def esp_stm32_last_frame_ms(self) -> int | None:
        """STM32 最近帧时间戳（ESP uptime ms）。"""
        return self.get_int("stm32", "last_frame_ms")

    @property
    def esp_protocol_ok_count(self) -> int | None:
        """ESP32 协议解析成功计数。"""
        return self.get_int("counters", "protocol_ok")

    @property
    def esp_protocol_error_count(self) -> int | None:
        """ESP32 协议解析错误计数。"""
        return self.get_int("counters", "protocol_error")

    # =========================================================================
    # 顶层属性 —— ECG 相关（当前 102 列 schema，列 72-77）
    # =========================================================================

    @property
    def ecg_valid(self) -> bool:
        """ECG 数据是否有效。

        当前 schema 中位于列 72。
        """
        return self.get_bool("ecg_valid")

    @property
    def ecg_hr(self) -> int | None:
        """ECG 心率值（bpm）。

        当前 schema 中位于列 73。
        """
        return self.get_int("ecg_hr")

    @property
    def ecg_rr_ms(self) -> int | None:
        """ECG RR 间期（ms）。

        当前 schema 中位于列 74。
        """
        return self.get_int("ecg_rr_ms")

    @property
    def ecg_lead_off(self) -> int | None:
        """ECG 导联脱落位掩码。

        当前 schema 中位于列 75。
        bit 0 = LD- 脱落，bit 1 = LD+ 脱落。
        0 = 导联正常。
        """
        return self.get_int("ecg_lead_off")

    @property
    def ecg_raw(self) -> int | None:
        """ECG 原始 ADC 值（低频趋势）。"""
        return self.get_int("ecg_raw")

    @property
    def ecg_filtered(self) -> int | None:
        """ECG 滤波后 ADC 值（低频趋势）。

        当前 schema 中位于列 77。
        """
        return self.get_int("ecg_filtered")

    # =========================================================================
    # 顶层属性 —— PTT 相关（当前 102 列 schema，列 78-79）
    # =========================================================================

    @property
    def ptt_valid(self) -> bool:
        """PTT（脉搏传导时间）是否有效。

        当前 schema 中位于列 78。
        """
        return self.get_bool("ptt_valid")

    @property
    def ptt_ms(self) -> int | None:
        """脉搏传导时间（ms）。

        当前 schema 中位于列 79。
        注意：PTT 不是血压估计值，仅作工程参考。
        """
        return self.get_int("ptt_ms")

    # =========================================================================
    # 顶层属性 —— HRV 时域指标
    # =========================================================================

    @property
    def hrv_valid(self) -> bool:
        """HRV（心率变异性）时域数据是否有效。"""
        return self.get_bool("hrv_valid")

    @property
    def mean_ibi(self) -> int | None:
        """平均 IBI（ms）。"""
        return self.get_int("mean_ibi")

    @property
    def sdnn(self) -> int | None:
        """SDNN —— 全部正常心搏间期标准差（ms）。"""
        return self.get_int("sdnn")

    @property
    def rmssd(self) -> int | None:
        """RMSSD —— 相邻心搏间期差值的均方根（ms）。"""
        return self.get_int("rmssd")

    @property
    def sd1(self) -> int | None:
        """庞加莱图 SD1（短轴，ms）。"""
        return self.get_int("sd1")

    @property
    def sd2(self) -> int | None:
        """庞加莱图 SD2（长轴，ms）。"""
        return self.get_int("sd2")

    @property
    def sd1_sd2_x100(self) -> int | None:
        """庞加莱图 SD1/SD2 比值 ×100。"""
        return self.get_int("sd1_sd2_x100")

    @property
    def rhythm_irregular(self) -> bool:
        """是否检测到心律不齐。

        True 时 Overview Tab 显示 "IRREGULAR" 提示。
        """
        return self.get_bool("rhythm_irregular")

    # =========================================================================
    # 顶层属性 —— HRV 频域指标
    # =========================================================================

    @property
    def hrv_freq_valid(self) -> bool:
        """HRV 频域数据是否有效。"""
        return self.get_bool("hrv_freq_valid")

    @property
    def lf_power_x100(self) -> int | None:
        """低频功率（LF）×100。"""
        return self.get_int("lf_power_x100")

    @property
    def hf_power_x100(self) -> int | None:
        """高频功率（HF）×100。"""
        return self.get_int("hf_power_x100")

    @property
    def lf_hf_x100(self) -> int | None:
        """LF/HF 比值 ×100。"""
        return self.get_int("lf_hf_x100")

    # =========================================================================
    # 顶层属性 —— PPG 信号诊断
    # =========================================================================

    @property
    def signal_ir_pi_x1000(self) -> int | None:
        """IR 通道灌注指数 ×1000。"""
        return self.get_int("signal_ir_pi_x1000")

    @property
    def signal_red_pi_x1000(self) -> int | None:
        """Red 通道灌注指数 ×1000。"""
        return self.get_int("signal_red_pi_x1000")

    @property
    def signal_ir_ac_rms(self) -> int | None:
        """IR 通道 AC 有效值。"""
        return self.get_int("signal_ir_ac_rms")

    @property
    def signal_red_ac_rms(self) -> int | None:
        """Red 通道 AC 有效值。"""
        return self.get_int("signal_red_ac_rms")

    @property
    def spo2_ratio_valid(self) -> bool:
        """SpO2 R 比值是否有效。"""
        return self.get_bool("spo2_ratio_valid")

    @property
    def spo2_ratio_x1000(self) -> int | None:
        """SpO2 R 比值 ×1000。"""
        return self.get_int("spo2_ratio_x1000")

    @property
    def spo2_balance_status(self) -> int | None:
        """SpO2 平衡状态码。

        0 = 平衡，非 0 = 失衡（具体含义见 STM32 固件文档）。
        """
        return self.get_int("spo2_balance_status")

    @property
    def ir_signal_delta(self) -> int | None:
        """IR 信号变化量。"""
        return self.get_int("ir_signal_delta")

    @property
    def ir_signal_span(self) -> int | None:
        """IR 信号跨度。"""
        return self.get_int("ir_signal_span")

    @property
    def red_signal_span(self) -> int | None:
        """Red 信号跨度。"""
        return self.get_int("red_signal_span")

    # =========================================================================
    # 顶层属性 —— 解析元信息
    # =========================================================================

    @property
    def parse_ok(self) -> bool:
        """ESP32/STM32 端解析是否成功。

        True = 设备端成功解析了 STM32 USART CSV 帧。
        False = 设备端遇到了字段数不匹配、CRC 错误等。
        注意：parse_ok 只反映设备端状态，不反映 PC 端 JSON 解析是否成功。
        """
        return self.get_bool("parse_ok", default=True)

    @property
    def rx_ms(self) -> int | None:
        """MQTT 消息从 STM32 到 ESP32 再到 PC 的接收延迟（ms）。

        仅作粗略参考，不是精确的端到端延迟。
        """
        return self.get_int("rx_ms")

    @property
    def field_count(self) -> int | None:
        """STM32 USART CSV 的列数（当前固件应为 102）。

        用于检测旧固件（81 列）还是新固件（102 列）。
        <90 的 field_count 触发 schema 警告。
        """
        return self.get_int("field_count")

    @property
    def schema_version(self) -> str | None:
        """ESP32 JSON 的 schema 版本号。

        例如 "1.0"（旧 81 字段 schema）、"2.0"（新 102 字段 schema）。
        v1.x 版本触发 schema 警告。
        """
        return self.get_str("schema_version")

    @property
    def extra_fields(self) -> list[str]:
        """JSON/CSV 中的未知额外字段列表。

        格式如 ["col95=123", "col96=456"]。
        这些字段不在 KNOWN_FIELD_PATHS 中，但保留在此供诊断查看，
        不作为错误处理。
        """
        v = self.get("extra_fields", default=[])
        return v if isinstance(v, list) else []

    @property
    def parse_warnings(self) -> list[Any]:
        """ESP32/STM32 端的解析警告列表。

        例如 ["unexpected trailing data", "CRC mismatch"]。
        用于诊断面板展示，帮助判断数据质量问题。
        """
        warnings = self.get("parse_warnings", default=[])
        return warnings if isinstance(warnings, list) else []

    # =========================================================================
    # Schema 兼容性检测
    # =========================================================================

    def detect_schema_issue(self) -> str | None:
        """检测当前消息是否存在 schema 兼容性问题。

        检测规则：
          1. field_count < 90 → 疑似旧 81 字段 schema。
          2. parse_warnings 非空 → 设备端已报告问题。
          3. schema_version 以 "1." 开头 → 明确的旧版本。
          4. 缺少 ecg_valid 和 signal_quality 且无元数据 → 潜在旧 schema。

        返回：
          若存在问题时返回人类可读的警告字符串，
          否则返回 None（表示 schema 正常或无法判断）。
        """
        warnings: list[str] = []

        # 规则 1: field_count 异常（<90 很可能为旧 81 字段固件）
        fc = self.field_count
        if fc is not None and fc < 90:
            warnings.append(f"field_count={fc} (<90, 疑似旧 81 字段 schema)")

        # 规则 2: 设备端已报告的解析警告
        if self.parse_warnings:
            warnings.append(f"parse_warnings: {self.parse_warnings}")

        # 规则 3: schema_version 为 v1.x（旧版本）
        sv = self.schema_version
        if sv and sv.startswith("1."):
            warnings.append(f"schema_version={sv} (v1.x 为旧版本)")

        # 规则 4: 无元数据时的启发式检测
        if not fc and not sv and not self.parse_warnings:
            if not self.has("ecg_valid") and not self.has("signal_quality"):
                warnings.append("缺少 ecg_valid/signal_quality 字段 —— 可能为旧 schema")

        return "; ".join(warnings) if warnings else None

    # =========================================================================
    # 顶层属性 —— 系统诊断（当前 102 字段 schema 的系统字段）
    #
    # 替换了旧 schema 中的 sd_card_ready / sd_log_error / display_brightness_index。
    # 当前 102 列中位于列 80-90。
    # =========================================================================

    @property
    def sd_log_active(self) -> bool:
        """SD 卡日志是否活跃（正在记录）。

        当前 schema 中位于列 80。
        """
        return self.get_bool("sd_log_active")

    @property
    def sd_state(self) -> int | None:
        """SD 卡状态码。

        当前 schema 中位于列 81。
        0=未初始化, 1=就绪, 2=记录中, 3=错误。
        """
        return self.get_int("sd_state")

    @property
    def sd_error(self) -> int | None:
        """SD 卡错误码。

        当前 schema 中位于列 82。
        0=无错误，非 0 表示具体的错误类型。
        """
        return self.get_int("sd_error")

    @property
    def sd_total_written(self) -> int | None:
        """SD 卡累计写入字节数。

        当前 schema 中位于列 83。
        """
        return self.get_int("sd_total_written")

    @property
    def display_refresh_count(self) -> int | None:
        """OLED 显示屏刷新计数。

        当前 schema 中位于列 84。
        """
        return self.get_int("display_refresh_count")

    @property
    def display_last_refresh_tick(self) -> int | None:
        """OLED 显示屏上次刷新时的系统 tick。

        当前 schema 中位于列 85。
        """
        return self.get_int("display_last_refresh_tick")

    @property
    def debug_mode(self) -> bool:
        """是否处于调试模式。

        当前 schema 中位于列 86。
        """
        return self.get_bool("debug_mode")

    @property
    def current_page(self) -> int | None:
        """当前 OLED 显示页面编号。

        当前 schema 中位于列 87。
        """
        return self.get_int("current_page")

    @property
    def crash_flag(self) -> bool:
        """是否发生过崩溃（看门狗/硬故障等）。

        当前 schema 中位于列 88。
        True = 系统曾崩溃并重启。
        """
        return self.get_bool("crash_flag")

    @property
    def crash_source(self) -> int | None:
        """崩溃来源码。

        当前 schema 中位于列 89。
        编码含义见 STM32 固件的 crash_reason 枚举。
        """
        return self.get_int("crash_source")

    @property
    def reboot_count(self) -> int | None:
        """系统累计重启次数。

        当前 schema 中位于列 90。
        """
        return self.get_int("reboot_count")

    # =========================================================================
    # 顶层属性 —— RTC 对时确认（message_type = "rtc_set_ack"）
    # =========================================================================

    @property
    def rtc_set_ok(self) -> bool:
        """RTC 对时是否成功。

        STM32 收到 SETTIME 命令后反馈 set_ok=True/False。
        """
        return self.get_bool("set_ok")

    @property
    def rtc_set_reason(self) -> str | None:
        """RTC 对时失败时的原因描述。"""
        return self.get_str("reason")

    # =========================================================================
    # 顶层属性 —— 设备端解析错误（message_type = "parse_error"）
    # =========================================================================

    @property
    def error_text(self) -> str | None:
        """设备端解析错误的描述文本。"""
        return self.get_str("error")

    @property
    def raw_line(self) -> str | None:
        """原始 STM32 CSV 行（用于 Raw Frame 视图和错误诊断）。"""
        return self.get_str("raw_line")

    # =========================================================================
    # ECG 导联脱落位解析
    #
    # ecg_lead_off 是一个位掩码：
    #   bit 0 = LD- 脱落
    #   bit 1 = LD+ 脱落
    #   0 = 导联正常
    # =========================================================================

    @property
    def ecg_lead_off_ld_minus(self) -> bool:
        """LD-（负导联）是否脱落。"""
        lo = self.ecg_lead_off
        return (lo or 0) & 1 != 0

    @property
    def ecg_lead_off_ld_plus(self) -> bool:
        """LD+（正导联）是否脱落。"""
        lo = self.ecg_lead_off
        return (lo or 0) & 2 != 0

    @property
    def ecg_lead_off_label(self) -> str:
        """ECG 导联状态的友好文本标签。

        返回：
          "OK" —— 导联正常。
          "LD-" —— LD- 脱落。
          "LD+" —— LD+ 脱落。
          "LD-,LD+" —— 双导联脱落。
        """
        lo = self.ecg_lead_off or 0
        if lo == 0:
            return "OK"
        parts = []
        if lo & 1:
            parts.append("LD-")
        if lo & 2:
            parts.append("LD+")
        return ",".join(parts)

    # =========================================================================
    # 时间戳工具
    # =========================================================================

    def plot_timestamp(self) -> datetime:
        """获取用于绘图的时间戳。

        优先级：
          1. 设备 RTC 时间（device_datetime）—— 精度最高。
          2. PC 端接收时间（received_at）—— RTC 无效时的回退。

        返回：
          datetime 对象，用于 X 轴时间轴。
        """
        return self.device_datetime or self.received_at

    def timestamp_valid(self) -> bool:
        """设备 RTC 时间是否可用。

        True = RTC 有效且 device_datetime 可解析。
        False = 使用 PC 接收时间作为回退。
        """
        return self.rtc_valid and self.device_datetime is not None


# =============================================================================
# KNOWN_FIELD_PATHS —— 所有已知字段路径列表
#
# 格式: [(dotted_path, display_name), ...]
# dotted_path: raw 字典中的字段键（支持点号分隔的嵌套路径）
# display_name: 在 UI 中显示的中文/英文名称
#
# 用途：
#   - DataManager 的 available_fields 集合基于此列表。
#   - UI 诊断面板可遍历此列表展示所有已知字段的值。
# =============================================================================

KNOWN_FIELD_PATHS: list[tuple[str, str]] = [
    # ---- 核心生命体征 ----
    ("bpm", "BPM"),
    ("spo2", "SpO2"),
    ("rr", "RR"),
    ("ibi", "IBI"),

    # ---- 原始 PPG 信号 ----
    ("red", "Red"),
    ("ir", "IR"),
    ("baseline_ir", "Baseline IR"),

    # ---- 信号质量 ----
    ("signal_quality", "SQ"),
    ("motion_score", "Motion Score"),
    ("signal_ir_pi_x1000", "IR PI×1000"),
    ("signal_red_pi_x1000", "Red PI×1000"),
    ("spo2_ratio_x1000", "R Ratio×1000"),
    ("spo2_balance_status", "Balance"),
    ("signal_ir_ac_rms", "IR AC RMS"),
    ("signal_red_ac_rms", "Red AC RMS"),

    # ---- HRV 时域 ----
    ("mean_ibi", "Mean IBI"),
    ("sdnn", "SDNN"),
    ("rmssd", "RMSSD"),
    ("sd1", "SD1"),
    ("sd2", "SD2"),
    ("sd1_sd2_x100", "SD1/SD2×100"),

    # ---- HRV 频域 ----
    ("lf_power_x100", "LF Power×100"),
    ("hf_power_x100", "HF Power×100"),
    ("lf_hf_x100", "LF/HF×100"),

    # ---- ECG / PTT（当前 102 字段 schema: ecg_filtered, ecg_hr, ecg_rr_ms, ptt_ms） ----
    ("ecg_filtered", "ECG Filt"),
    ("ecg_hr", "ECG HR"),
    ("ecg_rr_ms", "ECG RR"),
    ("ptt_ms", "PTT"),

    # ---- PPG 信号诊断 ----
    ("ir_signal_delta", "IR Delta"),
    ("ir_signal_span", "IR Span"),
    ("red_signal_span", "Red Span"),

    # ---- 手指检测 ----
    ("baseline_range_ir", "IR Baseline Range"),

    # ---- 解析校验 ----
    ("parse_ok", "Parse OK"),
    ("field_count", "Field Count"),
    ("rx_ms", "RX ms"),

    # ---- ESP32 链路状态 ----
    ("online", "ESP Online"),
    ("transport.active", "ESP Active Transport"),
    ("usb.active", "ESP USB Session Active"),
    ("usb.connected", "ESP USB Physical Connected"),
    ("mqtt.connected", "ESP MQTT Connected"),
    ("mqtt.subscribed", "ESP MQTT Subscribed"),
    ("wifi.connected", "ESP WiFi Connected"),
    ("stm32.protocol_state", "STM32 Protocol State"),
    ("stm32.last_frame", "STM32 Last Frame"),
    ("stm32.last_frame_ms", "STM32 Last Frame ms"),
    ("counters.protocol_ok", "Protocol OK Count"),
    ("counters.protocol_error", "Protocol Error Count"),
    ("esp_usb_connected", "ESP USB Connected"),
    ("esp_mqtt_connected", "ESP MQTT Connected"),
    ("esp_transport_mode", "ESP Transport Mode"),
]
