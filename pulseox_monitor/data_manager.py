# 维护测量历史环形缓冲，为绘图层生成序列数据。

from __future__ import annotations

import math
from collections import deque
from datetime import datetime
from typing import Any

from PySide6.QtCore import QObject, Signal

from .models import FlexibleMessage

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
        return float(value)
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
        self._time_window_seconds: float | None = None
        self._available_fields: set[str] = set()
        self._origin_time: datetime = datetime.now()
        self._switch_time: datetime | None = None

    @property
    def origin_time(self) -> datetime:
        """GUI 启动时的 PC 时间，作为 All 视图的 X 轴原点。"""
        return self._origin_time

    def set_switch_time(self, dt: datetime | None) -> None:
        """设置时间窗口切换时刻。非 None 时仅保留该时刻之后的数据。"""
        self._switch_time = dt

    # ---- 环形缓冲管理 ----

    def __len__(self) -> int:
        return len(self._history)

    @property
    def max_history(self) -> int:
        return self._history.maxlen or 0

    def clear(self) -> None:
        self._history.clear()
        self._available_fields.clear()

    def set_time_window(self, seconds: float | None) -> None:
        self._time_window_seconds = seconds

    def time_window(self) -> float | None:
        return self._time_window_seconds

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

    def _plot_time(self, msg: FlexibleMessage) -> float:
        return msg.plot_timestamp().timestamp()

    def _series_x(self, messages: list[FlexibleMessage]) -> list[float]:
        x = [self._plot_time(m) for m in messages]
        if _needs_received_time_fallback(x):
            x = [m.received_at.timestamp() for m in messages]
        return _make_strictly_increasing(x)

    def _windowed_messages_with_x(self) -> tuple[list[FlexibleMessage], list[float]]:
        messages = list(self._history)
        if not messages:
            return [], []

        x = self._series_x(messages)
        if self._switch_time is not None:
            # 固定窗口模式：仅保留切换时刻之后的数据
            cutoff = self._switch_time.timestamp()
        elif self._time_window_seconds is None:
            return messages, x
        else:
            cutoff = datetime.now().timestamp() - self._time_window_seconds
        pairs = [(m, t) for m, t in zip(messages, x) if t >= cutoff]
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
