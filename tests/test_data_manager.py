# 验证数据管理器的环形缓冲和绘图序列输出。

from __future__ import annotations

import math
import unittest
from datetime import datetime

from pulseox_monitor.data_manager import DataManager
from pulseox_monitor.models import MeasurementMessage


# 验证环形缓冲和绘图序列的生成逻辑。
class DataManagerTests(unittest.TestCase):
    def test_ring_buffer_discards_old_data(self) -> None:
        # 当历史容量满后，最旧的数据应被自动丢弃。
        manager = DataManager(max_history=2)
        manager.add_measurement(self._measurement(received_at=datetime(2026, 4, 9, 10, 0, 0), ir=1))
        manager.add_measurement(self._measurement(received_at=datetime(2026, 4, 9, 10, 0, 1), ir=2))
        manager.add_measurement(self._measurement(received_at=datetime(2026, 4, 9, 10, 0, 2), ir=3))

        samples = manager.samples()
        self.assertEqual(len(samples), 2)
        self.assertEqual(samples[0].ir, 2)
        self.assertEqual(samples[1].ir, 3)

    def test_invalid_rtc_uses_received_time_for_plot(self) -> None:
        # RTC 无效时，绘图时间应回退到 PC 接收时间。
        manager = DataManager(max_history=10)
        received_at = datetime(2026, 4, 9, 10, 0, 0)
        sample = manager.add_measurement(self._measurement(received_at=received_at, rtc_valid=False))
        self.assertFalse(sample.timestamp_valid)
        self.assertEqual(sample.plot_timestamp, received_at)

    def test_invalid_bpm_and_spo2_become_nan_in_series(self) -> None:
        # 无效的 BPM 和 SpO2 在曲线序列中应表示为 NaN。
        manager = DataManager(max_history=10)
        manager.add_measurement(
            self._measurement(
                received_at=datetime(2026, 4, 9, 10, 0, 0),
                bpm_valid=False,
                bpm=None,
                spo2_valid=False,
                spo2=None,
            )
        )
        series = manager.plot_series()
        self.assertTrue(math.isnan(series["bpm"][1][0]))
        self.assertTrue(math.isnan(series["spo2"][1][0]))

    def _measurement(
        self,
        *,
        received_at: datetime,
        rtc_valid: bool = True,
        ir: int = 123,
        bpm_valid: bool = True,
        bpm: int | None = 70,
        spo2_valid: bool = True,
        spo2: int | None = 98,
    ) -> MeasurementMessage:
        # 构造测试用 MeasurementMessage，减少重复样板。
        return MeasurementMessage(
            bridge="gw-01",
            source="sensor-a",
            channel="1",
            protocol="mqtt",
            frame="1",
            message="measurement",
            received_at=received_at,
            rtc_valid=rtc_valid,
            date="20260409" if rtc_valid else None,
            time="100000" if rtc_valid else None,
            device_datetime=datetime(2026, 4, 9, 10, 0, 0) if rtc_valid else None,
            red=456,
            ir=ir,
            baseline_ir=400,
            finger=True,
            bpm_valid=bpm_valid,
            bpm=bpm,
            spo2_valid=spo2_valid,
            spo2=spo2,
        )


if __name__ == "__main__":
    unittest.main()
