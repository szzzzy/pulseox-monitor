# 维护测量历史环形缓冲，为绘图层生成序列数据。

from __future__ import annotations

import math
from collections import deque
from datetime import datetime
from enum import Enum
from typing import Any

from PySide6.QtCore import QObject, Signal

from .models import FlexibleMessage


class DisplayMode(Enum):
    """时间窗口显示模式。

    OBSERVE — 固定采集观察窗口：[observe_start, observe_start + window]。
    MONITOR — 实时滚动监控窗口：[now - window, now]。
    HISTORY — 静态历史快照：[origin_time, snapshot_time]。
    """
    OBSERVE = "observe"
    MONITOR = "monitor"
    HISTORY = "history"

_SENTINELS: set[str] = {"", "--", "N/A", "n/a", "NA", "na", "null", "NULL", "None"}
_MIN_X_STEP_SECONDS = 0.001

_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "bpm": (
        "heart_rate", "heartRate", "hr", "HR", "pulse", "pulse_rate",
        "pulseRate", "modules.heart_rate.value", "modules.hr.value",
        "modules.pulse.value",
    ),
    "bpm_valid": (
        "bpmValid", "heart_rate_valid", "heartRateValid", "hr_valid",
        "hrValid", "pulse_valid", "pulseValid", "modules.heart_rate.valid",
        "modules.hr.valid", "modules.pulse.valid",
    ),
    "spo2": (
        "SpO2", "SPO2", "spo2_pct", "spo2Percent", "oxygen",
        "blood_oxygen", "bloodOxygen", "modules.SpO2.value",
        "modules.oxygen.value",
    ),
    "spo2_valid": (
        "spo2Valid", "SpO2_valid", "SpO2Valid", "oxygen_valid",
        "oxygenValid", "modules.SpO2.valid", "modules.oxygen.valid",
    ),
    "rr": (
        "resp_rate", "respRate", "respiratory_rate", "respiratoryRate",
        "breathing_rate", "breathingRate", "modules.resp_rate.value",
        "modules.respiratory_rate.value",
    ),
    "rr_valid": (
        "rrValid", "resp_rate_valid", "respRateValid",
        "respiratory_rate_valid", "respiratoryRateValid",
        "modules.resp_rate.valid", "modules.respiratory_rate.valid",
    ),
    "ibi": ("ibi_ms", "ibiMs", "interval_ms", "intervalMs", "modules.ibi_ms.value"),
    "ibi_valid": ("ibiValid", "ibi_ms_valid", "ibiMsValid", "modules.ibi_ms.valid"),
    "red": ("red_raw", "redRaw", "ppg_red", "ppgRed", "red_adc", "redAdc"),
    "ir": ("ir_raw", "irRaw", "ppg_ir", "ppgIr", "ir_adc", "irAdc"),
    "baseline_ir": (
        "ir_baseline", "irBaseline", "baselineIR", "baseline", "dc_ir",
        "dcIr",
    ),
    "signal_quality": (
        "signalQuality", "signal_quality_score", "signalQualityScore",
        "sq", "SQ", "quality", "modules.sq.value",
    ),
    "motion_score": ("motionScore", "motion", "motion.score", "modules.motion.value"),
    "motion_artifact": ("motionArtifact", "motion_detected", "motionDetected"),
    "signal_ir_pi_x1000": (
        "ir_pi_x1000", "irPiX1000", "ir_pi", "irPI", "modules.ir_pi.value",
    ),
    "signal_red_pi_x1000": (
        "red_pi_x1000", "redPiX1000", "red_pi", "redPI",
        "modules.red_pi.value",
    ),
    "signal_ir_ac_rms": ("ir_ac_rms", "irAcRms", "ir_ac", "irAC"),
    "signal_red_ac_rms": ("red_ac_rms", "redAcRms", "red_ac", "redAC"),
    "spo2_ratio_x1000": ("ratio_x1000", "ratioX1000", "r_ratio", "rRatio"),
    "spo2_ratio_valid": ("ratio_valid", "ratioValid", "r_ratio_valid", "rRatioValid"),
    "ir_signal_delta": ("ir_delta", "irDelta"),
    "ir_signal_span": ("ir_span", "irSpan"),
    "red_signal_span": ("red_span", "redSpan"),
    "mean_ibi": ("mean_ibi_ms", "meanIbi", "meanIbiMs"),
    "sd1_sd2_x100": ("sd1_sd2", "sd1Sd2", "sd1Sd2X100"),
    "lf_power_x100": ("lf_power", "lfPower", "lfPowerX100"),
    "hf_power_x100": ("hf_power", "hfPower", "hfPowerX100"),
    "lf_hf_x100": ("lf_hf", "lfHf", "lfHfX100"),
    "ecg_filtered": ("ecg_filt", "ecgFilt", "ecgFiltered", "ecg.filtered"),
    "ecg_raw": ("ecgRaw", "ecg.raw"),
    "ecg_hr": ("ecgHeartRate", "ecg_heart_rate", "ecgHr"),
    "ecg_valid": ("ecgValid",),
    "ecg_rr_ms": ("ecg_rr", "ecgRr", "ecgRrMs"),
    "ptt_ms": ("ptt", "pttMs"),
    "ptt_valid": ("pttValid",),
    "ecg_signal_quality": ("ecgSignalQuality", "ecg_sq", "ecgSq"),
    "ecg_invalid_reason": ("ecgInvalidReason",),
    "ecg_raw_span": ("ecgRawSpan",),
    "ecg_filtered_span": ("ecgFilteredSpan", "ecgFiltSpan"),
    "ecg_noise_level": ("ecgNoiseLevel",),
    "ecg_qrs_threshold": ("ecgQrsThreshold",),
    "ecg_peak_snr_x100": ("ecgPeakSnrX100", "ecgPeakSNRx100"),
    "ecg_dma_available_high_watermark": ("ecgDmaAvailHwm", "ecg_dma_avail_hwm"),
}


def _to_plot_float(value: object) -> float:
    """将任意值安全转为绘图用 float，不可转换时返回 math.nan。

    支持：int, float, 数字字符串（如 "72", "98.5"）。
    拒绝：bool, None, 哨兵字符串（"--", "N/A" 等）, 非数字字符串。
    """
    if value is None:
        return math.nan
    if isinstance(value, bool):
        return math.nan
    if isinstance(value, dict):
        for key in ("value", "val", "last", "avg", "mean", "filtered", "raw"):
            if key in value:
                converted = _to_plot_float(value[key])
                if not math.isnan(converted):
                    return converted
        return math.nan
    if isinstance(value, (list, tuple)):
        for item in reversed(value):
            converted = _to_plot_float(item)
            if not math.isnan(converted):
                return converted
        return math.nan
    if isinstance(value, (int, float)):
        converted = float(value)
        return converted if math.isfinite(converted) else math.nan
    if isinstance(value, str):
        stripped = value.strip()
        if stripped in _SENTINELS:
            return math.nan
        try:
            return float(stripped)
        except (ValueError, TypeError):
            return math.nan
    return math.nan


def _candidate_paths(field_path: str) -> tuple[str, ...]:
    return (field_path, *_FIELD_ALIASES.get(field_path, ()))


def _plot_value(message: FlexibleMessage, field_path: str) -> float:
    for candidate in _candidate_paths(field_path):
        value = message.get(*candidate.split("."))
        converted = _to_plot_float(value)
        if not math.isnan(converted):
            return converted
    return math.nan


def _plot_bool(message: FlexibleMessage, field_path: str, default: bool = True) -> bool:
    for candidate in _candidate_paths(field_path):
        if message.has(*candidate.split(".")):
            return message.get_bool(*candidate.split("."), default=default)
    return default


def _schema_blocks_plotting(message: FlexibleMessage) -> bool:
    """明确 schema 异常帧保留在历史中，但不进入曲线数值。

    110 列当前协议不阻断；旧 102 列允许绘图但触发 legacy warning。
    仅 field_count < 90 或 v1.x schema 时阻断。
    """
    if message.message_type != "measurement":
        return False
    field_count = message.field_count
    if field_count is not None and field_count < 90:
        return True
    schema_version = message.schema_version
    if schema_version:
        normalized = schema_version.strip().lower()
        if normalized.startswith("v"):
            normalized = normalized[1:]
        if normalized == "1" or normalized.startswith("1."):
            return True
    return False


def _needs_received_time_fallback(x_values: list[float]) -> bool:
    previous: float | None = None
    for value in x_values:
        if not math.isfinite(value):
            return True
        if previous is not None and value <= previous:
            return True
        previous = value
    return False


def _make_strictly_increasing(x_values: list[float]) -> list[float]:
    adjusted: list[float] = []
    previous: float | None = None
    for value in x_values:
        if not math.isfinite(value):
            value = (previous or 0.0) + _MIN_X_STEP_SECONDS
        if previous is not None and value <= previous:
            value = previous + _MIN_X_STEP_SECONDS
        adjusted.append(value)
        previous = value
    return adjusted


class DataManager(QObject):
    data_received = Signal()
    latest_changed = Signal(object)  # FlexibleMessage

    def __init__(
        self,
        max_history: int = 5000,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._history: deque[FlexibleMessage] = deque(maxlen=max_history)
        self._available_fields: set[str] = set()
        self._origin_time: datetime = datetime.now()

        # 三模式状态
        self._display_mode: DisplayMode = DisplayMode.MONITOR
        self._observe_start: datetime | None = None
        self._observe_window: float = 120.0
        self._monitor_window: float = 120.0
        self._history_snapshot: datetime | None = None
        self._paused: bool = False

    # ---- 属性 ----

    @property
    def origin_time(self) -> datetime:
        return self._origin_time

    @property
    def display_mode(self) -> DisplayMode:
        return self._display_mode

    @property
    def paused(self) -> bool:
        return self._paused

    # ---- 模式管理 ----

    def set_display_mode(self, mode: DisplayMode) -> None:
        """切换显示模式并初始化对应锚点。

        已在目标模式时无操作（不重置锚点）。
        Observe：记录 observe_start = PC now。
        History：记录 history_snapshot = PC now。
        Monitor：无需额外锚点。

        参数：
          mode: 目标显示模式。
        """
        if self._display_mode == mode:
            return  # 已在目标模式，不重置锚点
        self._display_mode = mode
        if mode == DisplayMode.OBSERVE:
            self._observe_start = datetime.now()
        elif mode == DisplayMode.HISTORY:
            self._history_snapshot = datetime.now()

    def set_window_seconds(self, seconds: float) -> None:
        """设置当前模式的窗口长度（秒）。

        仅对 Observe 和 Monitor 模式生效，History 模式无窗口长度概念。

        参数：
          seconds: 窗口秒数（如 30、120、600）。
        """
        if self._display_mode == DisplayMode.OBSERVE:
            self._observe_window = seconds
        elif self._display_mode == DisplayMode.MONITOR:
            self._monitor_window = seconds

    def window_seconds(self) -> float | None:
        """返回当前模式的窗口长度（秒）。

        返回：
          Observe/Monitor 模式返回对应窗口秒数，History 模式返回 None。
        """
        if self._display_mode == DisplayMode.OBSERVE:
            return self._observe_window
        elif self._display_mode == DisplayMode.MONITOR:
            return self._monitor_window
        return None

    def restart_observe(self) -> None:
        """重新开始 Observe 窗口，将 observe_start 重置为 PC now。"""
        self._observe_start = datetime.now()

    def refresh_history(self) -> None:
        """推进 History 快照时间到 PC now，使之后到达的数据进入可见范围。"""
        self._history_snapshot = datetime.now()

    def set_paused(self, paused: bool) -> None:
        """设置暂停状态标志。

        参数：
          paused: True 表示暂停界面刷新（不暂停底层数据接收）。
        """
        self._paused = paused

    def observe_complete(self) -> bool:
        """检查 Observe 窗口是否已到达结束时间。

        返回：
          True 表示当前 PC 时间已超过 observe_start + observe_window。
          非 Observe 模式或 observe_start 未设置时返回 False。
        """
        if self._display_mode != DisplayMode.OBSERVE or self._observe_start is None:
            return False
        return datetime.now().timestamp() >= self._observe_start.timestamp() + self._observe_window

    def get_state(self) -> dict[str, object]:
        """返回当前显示状态的完整快照字典。

        返回的键：
          mode:              DisplayMode 枚举值
          x_min / x_max:     当前 X 轴时间范围（epoch 秒）
          visible_points:    窗口内的可见数据点数
          buffer_points:     环形缓冲中的总数据点数
          status:            状态文本（Live/Complete/Rolling/Static/Paused）
          plot_time_source:  绘图时间源（固定 "PC received"）
          device_rtc:        设备 RTC 诊断信息
          paused:            是否暂停
        """
        now = datetime.now()
        mode = self._display_mode
        buffer_count = len(self._history)

        if mode == DisplayMode.OBSERVE:
            if self._observe_start is not None:
                x_min = self._observe_start.timestamp()
                x_max = x_min + self._observe_window
                complete = self.observe_complete()
                status = "Complete" if complete else "Live"
            else:
                x_min = now.timestamp()
                x_max = x_min + self._observe_window
                status = "Live"
        elif mode == DisplayMode.MONITOR:
            x_max = now.timestamp()
            x_min = x_max - self._monitor_window
            status = "Rolling"
        elif mode == DisplayMode.HISTORY:
            x_min = self._origin_time.timestamp()
            snap = self._history_snapshot
            if snap is not None:
                x_max = snap.timestamp()
                status = "Static"
            else:
                # 无快照（Clear 后）：显示空范围
                x_max = x_min
                status = "Static (empty)"
        else:
            x_min = self._origin_time.timestamp()
            x_max = now.timestamp()
            status = "Unknown"

        if self._paused:
            status = "Paused"

        # 计算可见点数（窗口内的点数）
        msgs, _ = self._windowed_messages_with_x()
        visible = len(msgs)

        # 检测时间源
        time_source = self._detect_time_source()

        return {
            "mode": mode,
            "x_min": x_min,
            "x_max": x_max,
            "visible_points": visible,
            "buffer_points": buffer_count,
            "status": status,
            "plot_time_source": "PC received",
            "device_rtc": self._device_rtc_info(),
            "paused": self._paused,
        }

    def _detect_time_source(self) -> str:
        """返回绘图时间源标识（固定为 "PC received"）。

        设备 RTC 不参与绘图，仅通过 _device_rtc_info() 提供诊断信息。
        """
        return "PC received"

    def _device_rtc_info(self) -> str:
        """返回设备 RTC 诊断信息（仅用于状态栏展示，不影响绘图）。

        返回：
          "—"      — 无数据。
          "valid (HH:MM:SS)" — 设备 RTC 有效，显示最近时间。
          "invalid" — 设备 RTC 不可用。
        """
        msgs = list(self._history)
        if not msgs:
            return "—"
        latest = msgs[-1]
        if latest.timestamp_valid():
            dt = latest.device_datetime
            if dt:
                return f"valid ({dt.strftime('%H:%M:%S')})"
            return "valid"
        if latest.device_datetime is not None:
            return f"valid ({latest.device_datetime.strftime('%H:%M:%S')})"
        return "invalid"

    # ---- 向后兼容（旧 API，已废弃） ----

    def set_time_window(self, seconds: float | None) -> None:
        """已废弃。请使用 set_window_seconds(seconds)。

        None 参数不再支持（旧 "All" 模式已移除），将被忽略。

        参数：
          seconds: 窗口秒数。传入 None 无效果。
        """
        if seconds is not None:
            self.set_window_seconds(seconds)

    def time_window(self) -> float | None:
        """已废弃。请使用 window_seconds()。

        返回：
          当前模式的窗口秒数，History 模式返回 None。
        """
        return self.window_seconds()

    def set_switch_time(self, dt: datetime | None) -> None:
        """已废弃。Observe/Monitor/History 模式不再使用固定锚点。

        此方法为空操作，仅保留以兼容旧调用方。
        """

    # ---- 环形缓冲管理 ----

    def __len__(self) -> int:
        return len(self._history)

    @property
    def max_history(self) -> int:
        return self._history.maxlen or 0

    def clear(self) -> None:
        """清空环形缓冲并重置状态。

        副作用：
          - 清空所有历史消息和可用字段集合。
          - 重置 origin_time = PC now。
          - Observe 模式：重新开始 Observe（restart_observe）。
          - History 模式：清除快照（snapshot = None），之后新数据不会自动显示。
          - Monitor 模式：仅清空缓冲，保持 rolling 窗口。
        """
        self._history.clear()
        self._available_fields.clear()
        self._origin_time = datetime.now()
        if self._display_mode == DisplayMode.OBSERVE:
            self._observe_start = datetime.now()
        elif self._display_mode == DisplayMode.HISTORY:
            self._history_snapshot = None

    # ---- 数据写入 ----

    def add_message(self, message: FlexibleMessage) -> None:
        self._history.append(message)
        self._available_fields.update(self._collect_fields(message.raw))
        self.latest_changed.emit(message)
        self.data_received.emit()

    # ---- 数据查询 ----

    def latest(self) -> FlexibleMessage | None:
        return self._history[-1] if self._history else None

    def latest_flat(self) -> dict[str, object]:
        msg = self.latest()
        if msg is None:
            return {}
        return _flatten_dict(msg.raw)

    def available_fields(self) -> set[str]:
        return set(self._available_fields)

    def messages(self) -> list[FlexibleMessage]:
        return list(self._history)

    # ---- 时间戳 ----
    # PC received_at 是唯一可信 wall-clock 时间源。
    # 设备 RTC 不参与绘图、窗口过滤和显示模式判断。

    def _plot_time(self, msg: FlexibleMessage) -> float:
        """提取消息的 PC 接收时间作为绘图 X 轴时间。

        参数：
          msg: 数据消息。

        返回：
          received_at 的 epoch 秒（float）。
        """
        return msg.received_at.timestamp()

    def _series_x(self, messages: list[FlexibleMessage]) -> list[float]:
        """为消息列表生成严格递增的 X 轴时间序列。

        所有 X 值基于 received_at，通过 _make_strictly_increasing 处理
        重复时间戳（插入 1ms 间隙）。

        参数：
          messages: 消息列表。

        返回：
          严格递增的 epoch 秒列表。
        """
        x = [m.received_at.timestamp() for m in messages]
        return _make_strictly_increasing(x)

    def _windowed_messages_with_x(self) -> tuple[list[FlexibleMessage], list[float]]:
        """按当前显示模式过滤消息并生成 X 轴时间序列。

        Observe：仅保留 [observe_start, observe_start + observe_window] 内的消息。
        Monitor：仅保留 [now - monitor_window, now] 内的消息。
        History：仅保留 [origin_time, history_snapshot] 内的消息，
                 snapshot 为 None 时返回空。

        返回：
          (messages, x_values) 元组。无数据时返回 ([], [])。
        """
        messages = list(self._history)
        if not messages:
            return [], []

        x = self._series_x(messages)

        if self._display_mode == DisplayMode.OBSERVE:
            if self._observe_start is None:
                return messages, x
            start = self._observe_start.timestamp()
            end = start + self._observe_window
            pairs = [(m, t) for m, t in zip(messages, x) if start <= t <= end]

        elif self._display_mode == DisplayMode.MONITOR:
            cutoff = datetime.now().timestamp() - self._monitor_window
            pairs = [(m, t) for m, t in zip(messages, x) if t >= cutoff]

        elif self._display_mode == DisplayMode.HISTORY:
            if self._history_snapshot is None:
                # 无快照时不显示任何数据，保持 History 静态语义
                return [], []
            end = self._history_snapshot.timestamp()
            pairs = [(m, t) for m, t in zip(messages, x) if t <= end]

        else:
            return messages, x

        if not pairs:
            return [], []
        return [m for m, _ in pairs], [t for _, t in pairs]

    # ---- 序列提取 ----

    def series(
        self,
        field_path: str,
        valid_check_path: str | None = None,
        max_points: int | None = None,
    ) -> tuple[list[float], list[float], list[bool], list[float]]:
        """返回 (x, y, validity_mask, rx_ms_list)。

        - x: 时间戳 (float)
        - y: 字段值，缺失/null 时为 NaN
        - validity_mask: 每个点是否有效（用于灰显），True = 有效
        - rx_ms_list: 每个点的 rx_ms
        """
        messages, x_values = self._windowed_messages_with_x()

        x: list[float] = []
        y: list[float] = []
        valid: list[bool] = []
        rx: list[float] = []

        for msg in messages:
            if _schema_blocks_plotting(msg):
                y.append(math.nan)
                is_valid = False
            else:
                y.append(_plot_value(msg, field_path))

                is_valid = True
                if valid_check_path:
                    is_valid = _plot_bool(msg, valid_check_path, default=True)
            valid.append(is_valid)

            rx.append(float(msg.rx_ms or 0))

        x = list(x_values)

        if max_points is not None and len(x) > max_points:
            step = len(x) // max_points
            x = x[::step]
            y = y[::step]
            valid = valid[::step]
            rx = rx[::step]

        return x, y, valid, rx

    def series_multi(
        self,
        field_paths: list[str],
    ) -> dict[str, tuple[list[float], list[float]]]:
        """批量提取多条序列，共享同一条 X 轴。"""
        messages, x = self._windowed_messages_with_x()
        result: dict[str, tuple[list[float], list[float]]] = {}

        for path in field_paths:
            y: list[float] = []
            for msg in messages:
                if _schema_blocks_plotting(msg):
                    y.append(math.nan)
                else:
                    y.append(_plot_value(msg, path))
            result[path] = (list(x), y)

        return result

    # ---- 工具 ----

    @staticmethod
    def _collect_fields(obj: Any, prefix: str = "") -> set[str]:
        fields: set[str] = set()
        if isinstance(obj, dict):
            for key, value in obj.items():
                full = f"{prefix}.{key}" if prefix else key
                fields.add(full)
                if isinstance(value, dict):
                    fields.update(DataManager._collect_fields(value, full))
        return fields


def _flatten_dict(obj: Any, prefix: str = "") -> dict[str, object]:
    result: dict[str, object] = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            full = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                result.update(_flatten_dict(value, full))
            else:
                result[full] = value
    return result
