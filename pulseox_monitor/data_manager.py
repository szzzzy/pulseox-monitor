# 维护测量历史环形缓冲，并为绘图层生成序列数据。

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime

from .models import MeasurementMessage


# 绘图层和状态面板消费的标准化测量样本。
@dataclass(slots=True)
class MeasurementSample:
    received_at: datetime
    device_timestamp: datetime | None
    plot_timestamp: datetime
    timestamp_valid: bool
    ir: int
    red: int
    baseline_ir: int
    finger: bool
    bpm_valid: bool
    bpm: int | None
    spo2_valid: bool
    spo2: int | None


# 保存测量历史，并为绘图层提供规整后的序列。
class DataManager:
    def __init__(self, max_history: int = 1200) -> None:
        # 创建固定长度的环形缓冲区。
        self._history: deque[MeasurementSample] = deque(maxlen=max_history)

    @property
    def max_history(self) -> int:
        # 返回当前环形缓冲区的最大样本数。
        return self._history.maxlen or 0

    def __len__(self) -> int:
        # 返回当前历史样本数。
        return len(self._history)

    def clear(self) -> None:
        # 清空全部历史样本。
        self._history.clear()

    def add_measurement(self, message: MeasurementMessage) -> MeasurementSample:
        # 把解析后的 measurement 消息写入环形缓冲区。
        # RTC 无效时仍然保留样本，但绘图时间统一回退到 PC 接收时间。
        if message.rtc_valid and message.device_datetime is not None:
            timestamp_valid = True
            plot_timestamp = message.device_datetime
        else:
            timestamp_valid = False
            plot_timestamp = message.received_at

        sample = MeasurementSample(
            received_at=message.received_at,
            device_timestamp=message.device_datetime,
            plot_timestamp=plot_timestamp,
            timestamp_valid=timestamp_valid,
            ir=message.ir,
            red=message.red,
            baseline_ir=message.baseline_ir,
            finger=message.finger,
            bpm_valid=message.bpm_valid,
            bpm=message.bpm,
            spo2_valid=message.spo2_valid,
            spo2=message.spo2,
        )
        self._history.append(sample)
        return sample

    def latest(self) -> MeasurementSample | None:
        # 返回最近一个样本。
        if not self._history:
            return None
        return self._history[-1]

    def samples(self) -> list[MeasurementSample]:
        # 导出当前历史的浅拷贝。
        return list(self._history)

    def plot_series(self) -> dict[str, tuple[list[float], list[float]]]:
        # 把样本历史转换成 pyqtgraph 可直接使用的曲线数据。
        x_values: list[float] = []
        ir_values: list[float] = []
        red_values: list[float] = []
        bpm_values: list[float] = []
        spo2_values: list[float] = []

        for sample in self._history:
            x_values.append(sample.plot_timestamp.timestamp())
            ir_values.append(float(sample.ir))
            red_values.append(float(sample.red))

            # 无效的 BPM 和 SpO2 用 NaN 占位，绘图层会自动跳过这些点。
            bpm_values.append(
                float(sample.bpm) if sample.bpm_valid and sample.bpm is not None else math.nan
            )
            spo2_values.append(
                float(sample.spo2) if sample.spo2_valid and sample.spo2 is not None else math.nan
            )

        return {
            "ir": (x_values, ir_values),
            "red": (x_values, red_values),
            "bpm": (x_values, bpm_values),
            "spo2": (x_values, spo2_values),
        }
