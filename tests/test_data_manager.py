# 验证数据管理器的环形缓冲和序列输出。

from __future__ import annotations

import math
import unittest
from datetime import datetime, timedelta

from pulseox_monitor.data_manager import DataManager, DisplayMode
from pulseox_monitor.models import FlexibleMessage


def _make_msg(
    received_at: datetime | None = None,
    rtc_valid: bool = True,
    ir: int = 123,
    bpm_valid: bool = True,
    bpm: int | None = 70,
    spo2_valid: bool = True,
    spo2: int | None = 98,
    **kwargs,
) -> FlexibleMessage:
    payload = {
        "message": "measurement",
        "bridge": "gw-01",
        "source": "sensor-a",
        "channel": "1",
        "protocol": "mqtt",
        "frame": "100",
        "rtc_valid": rtc_valid,
        "date": "20260409" if rtc_valid else None,
        "time": "100000" if rtc_valid else None,
        "red": 456,
        "ir": ir,
        "baseline_ir": 400,
        "finger": True,
        "bpm_valid": bpm_valid,
        "bpm": bpm,
        "spo2_valid": spo2_valid,
        "spo2": spo2,
        "rx_ms": 100,
        "parse_ok": True,
        "field_count": 12,
        "parse_warnings": [],
        "extra_fields": [],
    }
    payload.update(kwargs)
    return FlexibleMessage.from_dict(payload, received_at)


class DataManagerTests(unittest.TestCase):
    def test_ring_buffer_discards_old_data(self) -> None:
        manager = DataManager(max_history=2)
        manager.add_message(_make_msg(received_at=datetime(2026, 4, 9, 10, 0, 0), ir=1))
        manager.add_message(_make_msg(received_at=datetime(2026, 4, 9, 10, 0, 1), ir=2))
        manager.add_message(_make_msg(received_at=datetime(2026, 4, 9, 10, 0, 2), ir=3))

        self.assertEqual(len(manager), 2)
        msgs = manager.messages()
        self.assertEqual(msgs[0].ir, 2)
        self.assertEqual(msgs[1].ir, 3)

    def test_invalid_rtc_no_device_datetime(self) -> None:
        manager = DataManager(max_history=10)
        received_at = datetime(2026, 4, 9, 10, 0, 0)
        msg = _make_msg(received_at=received_at, rtc_valid=False)
        manager.add_message(msg)
        latest = manager.latest()
        self.assertIsNotNone(latest)
        self.assertFalse(latest.timestamp_valid())
        self.assertEqual(latest.device_datetime, None)
        self.assertEqual(latest.plot_timestamp(), received_at)

    def test_invalid_bpm_and_spo2_become_nan_in_series(self) -> None:
        manager = DataManager(max_history=10)
        manager.add_message(
            _make_msg(
                received_at=datetime.now(),
                rtc_valid=False,
                bpm_valid=False,
                bpm=None,
                spo2_valid=False,
                spo2=None,
            )
        )
        _, y_bpm, valid_bpm, _ = manager.series("bpm", valid_check_path="bpm_valid")
        _, y_spo2, valid_spo2, _ = manager.series("spo2", valid_check_path="spo2_valid")
        self.assertTrue(math.isnan(y_bpm[0]))
        self.assertTrue(math.isnan(y_spo2[0]))
        self.assertFalse(valid_bpm[0])
        self.assertFalse(valid_spo2[0])

    def test_valid_check_path(self) -> None:
        manager = DataManager(max_history=10)
        manager.add_message(
            _make_msg(
                received_at=datetime.now(),
                rtc_valid=False,
                bpm_valid=False,
                bpm=72,
            )
        )
        _, y_vals, valid, _ = manager.series("bpm", valid_check_path="bpm_valid")
        self.assertEqual(y_vals[0], 72.0)
        self.assertFalse(valid[0])

    def test_missing_field_returns_all_nan(self) -> None:
        manager = DataManager(max_history=10)
        manager.add_message(_make_msg(received_at=datetime.now()))
        _, y_vals, _, _ = manager.series("rr")
        self.assertTrue(all(math.isnan(v) for v in y_vals))

    def test_modules_path(self) -> None:
        manager = DataManager(max_history=10)
        now = datetime.now()
        payload = {
            "message": "measurement",
            "rtc_valid": True, "date": now.strftime("%Y%m%d"), "time": now.strftime("%H%M%S"),
            "red": 100, "ir": 200, "baseline_ir": 180, "finger": True,
            "bpm_valid": True, "bpm": 72, "spo2_valid": True, "spo2": 98,
            "rx_ms": 100, "parse_ok": True, "field_count": 12,
            "parse_warnings": [], "extra_fields": [],
            "modules": {
                "bpm": {"available": True, "valid": True, "value": 72},
                "rr": {"available": False, "value": None},
            },
        }
        msg = FlexibleMessage.from_dict(payload)
        manager.add_message(msg)
        _, y_vals, _, _ = manager.series("modules.bpm.value")
        self.assertEqual(y_vals[0], 72.0)

    def test_latest_flat(self) -> None:
        manager = DataManager(max_history=10)
        payload = {
            "message": "measurement",
            "rtc_valid": True, "date": "20260409", "time": "100000",
            "red": 100, "ir": 200, "baseline_ir": 180, "finger": True,
            "bpm_valid": True, "bpm": 72, "spo2_valid": True, "spo2": 98,
            "rx_ms": 100, "parse_ok": True, "field_count": 12,
            "parse_warnings": [], "extra_fields": [],
            "modules": {"bpm": {"available": True, "value": 72}},
        }
        msg = FlexibleMessage.from_dict(payload)
        manager.add_message(msg)
        flat = manager.latest_flat()
        self.assertIn("modules.bpm.available", flat)
        self.assertEqual(flat["modules.bpm.available"], True)
        self.assertEqual(flat["modules.bpm.value"], 72)

    def test_time_window_filtering(self) -> None:
        manager = DataManager(max_history=100)
        now = datetime.now()
        for i in range(60):
            manager.add_message(
                _make_msg(
                    received_at=now - timedelta(seconds=60 - i),
                    rtc_valid=False,
                    bpm=70 + i,
                )
            )
        # Default: MONITOR mode with 120s window, all 60 recent messages visible
        x, y, _, _ = manager.series("bpm")
        self.assertGreater(len(x), 50)

        # 30 second window
        manager.set_time_window(30)
        x, y, _, _ = manager.series("bpm")
        self.assertLessEqual(len(x), 31)  # ~30 messages in 30s


    def test_string_values_converted_to_plot_float(self) -> None:
        """bpm/spo2/ir 为数字字符串时 series 返回 float 而非 NaN。"""
        manager = DataManager(max_history=10)
        now = datetime.now()
        payload = {
            "message": "measurement",
            "rtc_valid": True, "date": now.strftime("%Y%m%d"), "time": now.strftime("%H%M%S"),
            "red": "100", "ir": "200", "baseline_ir": "180", "finger": True,
            "bpm_valid": True, "bpm": "72",
            "spo2_valid": True, "spo2": "98",
            "rx_ms": 100, "parse_ok": True, "field_count": 12,
            "parse_warnings": [], "extra_fields": [],
        }
        msg = FlexibleMessage.from_dict(payload)
        manager.add_message(msg)
        _, y_bpm, _, _ = manager.series("bpm")
        _, y_spo2, _, _ = manager.series("spo2")
        _, y_ir, _, _ = manager.series("ir")
        self.assertEqual(y_bpm[0], 72.0)
        self.assertEqual(y_spo2[0], 98.0)
        self.assertEqual(y_ir[0], 200.0)

    def test_duplicate_device_seconds_fall_back_to_received_time(self) -> None:
        """设备 RTC 只有秒级重复时，绘图 X 轴仍应严格递增。"""
        manager = DataManager(max_history=10)
        now = datetime.now()
        for i in range(3):
            manager.add_message(
                _make_msg(
                    received_at=datetime(now.year, now.month, now.day, now.hour, now.minute, now.second, i * 100_000),
                    rtc_valid=True,
                    bpm=70 + i,
                )
            )

        x, y, _, _ = manager.series("bpm")
        self.assertEqual(y, [70.0, 71.0, 72.0])
        self.assertLess(x[0], x[1])
        self.assertLess(x[1], x[2])

    def test_aliases_wrapped_values_and_arrays_are_plottable(self) -> None:
        """常见字段别名、value 包装和数组值也应能进入曲线。"""
        manager = DataManager(max_history=10)
        payload = {
            "message": "measurement",
            "rtc_valid": False,
            "heartRate": {"value": "74"},
            "bpmValid": "true",
            "SpO2": [96, "97"],
            "spo2Valid": "true",
            "irRaw": [200, 201],
            "redRaw": {"value": 101},
            "signalQuality": "88",
        }
        manager.add_message(FlexibleMessage.from_dict(payload))

        _, y_bpm, valid_bpm, _ = manager.series("bpm", valid_check_path="bpm_valid")
        _, y_spo2, _, _ = manager.series("spo2")
        _, y_ir, _, _ = manager.series("ir")
        _, y_red, _, _ = manager.series("red")
        _, y_sq, _, _ = manager.series("signal_quality")

        self.assertEqual(y_bpm[0], 74.0)
        self.assertTrue(valid_bpm[0])
        self.assertEqual(y_spo2[0], 97.0)
        self.assertEqual(y_ir[0], 201.0)
        self.assertEqual(y_red[0], 101.0)
        self.assertEqual(y_sq[0], 88.0)

    # ---- 三模式测试 ----

    def test_observe_mode_fixed_window(self) -> None:
        manager = DataManager(max_history=100)
        now = datetime.now()
        # Add data spanning the last 60 seconds
        for i in range(60):
            manager.add_message(
                _make_msg(
                    received_at=now - timedelta(seconds=60 - i),
                    rtc_valid=False,
                    bpm=70 + i,
                )
            )
        # Set observe_start to 30s ago, window = 30s → captures [now-30, now]
        manager.set_display_mode(DisplayMode.OBSERVE)
        manager._observe_start = now - timedelta(seconds=30)
        manager.set_window_seconds(30)
        x, y, _, _ = manager.series("bpm")
        self.assertLessEqual(len(x), 31)
        self.assertGreater(len(x), 0)

    def test_monitor_mode_rolling_cutoff(self) -> None:
        manager = DataManager(max_history=100)
        manager.set_display_mode(DisplayMode.MONITOR)
        manager.set_window_seconds(30)
        now = datetime.now()
        for i in range(60):
            manager.add_message(
                _make_msg(
                    received_at=now - timedelta(seconds=60 - i),
                    rtc_valid=False,
                    bpm=70 + i,
                )
            )
        x, y, _, _ = manager.series("bpm")
        self.assertLessEqual(len(x), 31)

    def test_history_mode_snapshot_filtering(self) -> None:
        manager = DataManager(max_history=100)
        now = datetime.now()
        for i in range(60):
            manager.add_message(
                _make_msg(
                    received_at=now - timedelta(seconds=60 - i),
                    rtc_valid=False,
                    bpm=70 + i,
                )
            )
        # Switch to History — snapshot = now, shows all 60 messages
        manager.set_display_mode(DisplayMode.HISTORY)
        x, y, _, _ = manager.series("bpm")
        self.assertEqual(len(x), 60)

        # Refresh history to a past time by manipulating snapshot
        manager._history_snapshot = now - timedelta(seconds=30)
        x, y, _, _ = manager.series("bpm")
        self.assertLessEqual(len(x), 31)

    def test_observe_complete_detection(self) -> None:
        manager = DataManager(max_history=100)
        manager.set_display_mode(DisplayMode.OBSERVE)
        manager.set_window_seconds(0.01)  # Very short window for test
        self.assertFalse(manager.observe_complete())
        # After window duration passes...
        import time
        time.sleep(0.02)
        self.assertTrue(manager.observe_complete())

    def test_clear_resets_state(self) -> None:
        manager = DataManager(max_history=100)
        now = datetime.now()
        manager.add_message(_make_msg(received_at=now, bpm=72))
        self.assertEqual(len(manager), 1)

        # Clear in Monitor mode
        manager.set_display_mode(DisplayMode.MONITOR)
        old_origin = manager.origin_time
        manager.clear()
        self.assertEqual(len(manager), 0)
        self.assertNotEqual(manager.origin_time, old_origin)

        # Clear in Observe mode should restart observe
        manager.set_display_mode(DisplayMode.OBSERVE)
        manager.add_message(_make_msg(received_at=datetime.now(), bpm=72))
        manager.clear()
        self.assertEqual(len(manager), 0)
        self.assertIsNotNone(manager._observe_start)

        # Clear in History mode should clear snapshot
        manager.set_display_mode(DisplayMode.HISTORY)
        manager.add_message(_make_msg(received_at=datetime.now(), bpm=72))
        manager.clear()
        self.assertEqual(len(manager), 0)
        self.assertIsNone(manager._history_snapshot)

    def test_get_state_returns_correct_fields(self) -> None:
        manager = DataManager(max_history=100)
        manager.set_display_mode(DisplayMode.MONITOR)
        manager.set_window_seconds(30)
        now = datetime.now()
        manager.add_message(_make_msg(received_at=now, bpm=72))

        state = manager.get_state()
        self.assertEqual(state["mode"], DisplayMode.MONITOR)
        self.assertIn("x_min", state)
        self.assertIn("x_max", state)
        self.assertIn("visible_points", state)
        self.assertIn("buffer_points", state)
        self.assertIn("status", state)
        self.assertIn("plot_time_source", state)
        self.assertEqual(state["plot_time_source"], "PC received")
        self.assertIn("device_rtc", state)
        self.assertIn("paused", state)
        self.assertEqual(state["buffer_points"], 1)
        self.assertEqual(state["status"], "Rolling")


    # ---- PC received_at 时间源测试 ----

    def test_rtc_deviation_observe_uses_received_at(self) -> None:
        """设备 RTC 与 PC 差很大时，Observe 仍按 received_at 正常显示。"""
        manager = DataManager(max_history=100)
        now = datetime.now()
        manager.set_display_mode(DisplayMode.OBSERVE)
        manager._observe_start = now - timedelta(seconds=10)
        manager.set_window_seconds(30)
        msg = _make_msg(
            received_at=now,
            rtc_valid=True,
            bpm=72,
        )
        # 覆盖 RTC 为 2020 年（远早于 PC），但系列 X 仍应使用 received_at
        msg.raw["date"] = "20200101"
        msg.raw["time"] = "000000"
        manager.add_message(msg)
        x, y, _, _ = manager.series("bpm")
        self.assertEqual(len(x), 1)
        self.assertAlmostEqual(x[0], now.timestamp(), delta=2)

    def test_rtc_deviation_monitor_uses_received_at(self) -> None:
        """设备 RTC 与 PC 差很大时，Monitor 仍按 received_at 正常显示。"""
        manager = DataManager(max_history=100)
        manager.set_display_mode(DisplayMode.MONITOR)
        manager.set_window_seconds(120)
        now = datetime.now()
        msg = _make_msg(
            received_at=now,
            rtc_valid=True,
            bpm=72,
        )
        msg.raw["date"] = "20200101"
        msg.raw["time"] = "000000"
        manager.add_message(msg)
        x, y, _, _ = manager.series("bpm")
        self.assertEqual(len(x), 1)
        self.assertAlmostEqual(x[0], now.timestamp(), delta=2)

    # ---- History 静态语义测试 ----

    def test_history_static_after_new_data(self) -> None:
        """History 切入后，新数据进入 buffer 但 visible series 不变化。"""
        import time
        manager = DataManager(max_history=100)
        now = datetime.now()
        manager.add_message(_make_msg(received_at=now - timedelta(seconds=5), bpm=70))
        manager.add_message(_make_msg(received_at=now - timedelta(seconds=3), bpm=72))

        manager.set_display_mode(DisplayMode.HISTORY)
        time.sleep(0.02)  # 确保 snapshot 时间在消息之前
        x1, _, _, _ = manager.series("bpm")
        self.assertEqual(len(x1), 2)

        # 新数据到来 — received_at 在 snapshot 之后，不应出现在 History
        manager.add_message(_make_msg(received_at=datetime.now(), bpm=75))
        x2, _, _, _ = manager.series("bpm")
        self.assertEqual(len(x2), 2, "History 快照未更新，不应显示新数据")

    def test_history_refresh_updates_snapshot(self) -> None:
        """Refresh History 后 snapshot 推进，可见新数据。"""
        import time
        manager = DataManager(max_history=100)
        now = datetime.now()
        manager.add_message(_make_msg(received_at=now - timedelta(seconds=5), bpm=70))
        manager.set_display_mode(DisplayMode.HISTORY)
        time.sleep(0.02)  # 确保 snapshot 时间在后续消息之前
        manager.add_message(_make_msg(received_at=datetime.now(), bpm=75))
        x1, _, _, _ = manager.series("bpm")
        self.assertEqual(len(x1), 1)  # 只有快照前的数据

        manager.refresh_history()
        x2, _, _, _ = manager.series("bpm")
        self.assertEqual(len(x2), 2)  # 快照更新后包含所有数据

    def test_history_clear_no_auto_show(self) -> None:
        """History Clear 后，新数据到来不自动出现在 History。"""
        manager = DataManager(max_history=100)
        now = datetime.now()
        manager.add_message(_make_msg(received_at=now, bpm=72))
        manager.set_display_mode(DisplayMode.HISTORY)
        self.assertEqual(len(manager.series("bpm")[0]), 1)

        manager.clear()
        self.assertEqual(len(manager.series("bpm")[0]), 0)

        # Clear 后新数据到来，不应自动显示在 History（snapshot 仍为 None）
        manager.add_message(_make_msg(received_at=datetime.now(), bpm=75))
        x, _, _, _ = manager.series("bpm")
        self.assertEqual(len(x), 0, "Clear 后 History 不应自动显示新数据")

        # 只有 Refresh History 后才显示
        manager.refresh_history()
        x, _, _, _ = manager.series("bpm")
        self.assertEqual(len(x), 1)

    # ---- 重复点击不重置测试 ----

    def test_repeated_set_display_mode_no_reset(self) -> None:
        """重复设置相同模式不会重置 observe_start / history_snapshot。"""
        manager = DataManager(max_history=100)

        # Observe
        manager.set_display_mode(DisplayMode.OBSERVE)
        first_start = manager._observe_start
        manager.set_display_mode(DisplayMode.OBSERVE)
        self.assertEqual(manager._observe_start, first_start, "重复设置 Observe 不应重置起点")

        # History
        manager.set_display_mode(DisplayMode.HISTORY)
        first_snap = manager._history_snapshot
        manager.set_display_mode(DisplayMode.HISTORY)
        self.assertEqual(manager._history_snapshot, first_snap, "重复设置 History 不应重置快照")


if __name__ == "__main__":
    unittest.main()
