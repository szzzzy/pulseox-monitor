# 验证消息分发器是否按协议正确解析不同消息类型。

from __future__ import annotations

import json
import unittest
from datetime import datetime

from pulseox_monitor.dispatcher import MessageDispatcher
from pulseox_monitor.models import MessageValidationError


class DispatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dispatcher = MessageDispatcher()
        self.received_at = datetime(2026, 4, 9, 12, 0, 0)

    def test_dispatch_measurement(self) -> None:
        payload = json.dumps(
            {
                "bridge": "gw-01",
                "source": "sensor-a",
                "channel": "1",
                "protocol": "mqtt",
                "frame": "100",
                "message": "measurement",
                "rtc_valid": True,
                "date": "20260409",
                "time": "115959",
                "red": 1200,
                "ir": 2400,
                "baseline_ir": 2200,
                "finger": True,
                "bpm_valid": True,
                "bpm": 72,
                "spo2_valid": True,
                "spo2": 98,
            }
        )
        message = self.dispatcher.dispatch(payload, received_at=self.received_at)
        self.assertIsNotNone(message)
        self.assertEqual(message.message_type, "measurement")
        self.assertEqual(message.device_datetime, datetime(2026, 4, 9, 11, 59, 59))
        self.assertEqual(message.bpm, 72)

    def test_dispatch_ack(self) -> None:
        payload = json.dumps(
            {
                "bridge": "gw-01",
                "source": "sensor-a",
                "channel": "1",
                "protocol": "mqtt",
                "frame": "101",
                "message": "rtc_set_ack",
                "set_ok": True,
                "rtc_valid": True,
                "date": "20260409",
                "time": "120001",
                "reason": "",
            }
        )
        message = self.dispatcher.dispatch(payload, received_at=self.received_at)
        self.assertIsNotNone(message)
        self.assertEqual(message.message_type, "rtc_set_ack")
        self.assertTrue(message.rtc_set_ok)
        self.assertEqual(message.device_datetime, datetime(2026, 4, 9, 12, 0, 1))

    def test_dispatch_measurement_with_nested_data(self) -> None:
        payload = json.dumps(
            {
                "bridge": "gw-01",
                "source": "sensor-a",
                "channel": "1",
                "protocol": "mqtt",
                "frame": "100",
                "message": "measurement",
                "data": {
                    "rtc_valid": True,
                    "date": 20260409,
                    "time": 120002,
                    "red": 1200,
                    "ir": 2400,
                    "baseline_ir": 2200,
                    "finger": 1,
                    "bpm_valid": True,
                    "bpm": 72,
                    "spo2_valid": True,
                    "spo2": 98,
                },
            }
        )
        message = self.dispatcher.dispatch(payload, received_at=self.received_at)
        self.assertIsNotNone(message)
        self.assertEqual(message.message_type, "measurement")
        self.assertEqual(message.device_datetime, datetime(2026, 4, 9, 12, 0, 2))

    def test_dispatch_parse_error(self) -> None:
        payload = json.dumps(
            {
                "bridge": "gw-01",
                "source": "sensor-a",
                "channel": "1",
                "protocol": "mqtt",
                "frame": "102",
                "message": "parse_error",
                "error": "bad checksum",
                "raw": "010203",
            }
        )
        message = self.dispatcher.dispatch(payload, received_at=self.received_at)
        self.assertIsNotNone(message)
        self.assertEqual(message.message_type, "parse_error")

    def test_unknown_message_no_longer_raises(self) -> None:
        # 未知消息类型不再抛异常，返回 FlexibleMessage 且 message_type="unknown"
        payload = json.dumps(
            {
                "bridge": "gw-01",
                "source": "sensor-a",
                "channel": "1",
                "protocol": "mqtt",
                "frame": "999",
                "message": "unknown",
            }
        )
        message = self.dispatcher.dispatch(payload, received_at=self.received_at)
        self.assertIsNotNone(message)
        self.assertEqual(message.message_type, "unknown")

    def test_missing_optional_fields_do_not_raise(self) -> None:
        # 12字段老固件兼容——缺失字段不抛异常
        payload = json.dumps(
            {
                "message": "measurement",
                "bridge": "gw-01",
                "source": "sensor-a",
                "channel": "1",
                "protocol": "mqtt",
                "frame": "100",
                "rtc_valid": True,
                "date": "20260409",
                "time": "120000",
                "red": 100,
                "ir": 200,
                "baseline_ir": 180,
                "finger": True,
                "bpm_valid": False,
                "bpm": None,
                "spo2_valid": False,
                "spo2": None,
            }
        )
        message = self.dispatcher.dispatch(payload, received_at=self.received_at)
        self.assertIsNotNone(message)
        # RR, IBI, HRV, ECG, PTT 等缺失字段应为 None
        self.assertIsNone(message.rr)
        self.assertIsNone(message.ibi)
        self.assertIsNone(message.sdnn)
        self.assertIsNone(message.ecg_hr)
        self.assertIsNone(message.ptt_ms)
        self.assertFalse(message.rr_valid)

    def test_non_json_raises(self) -> None:
        with self.assertRaises(MessageValidationError):
            self.dispatcher.dispatch("not json at all")

    def test_json_non_dict_raises(self) -> None:
        with self.assertRaises(MessageValidationError):
            self.dispatcher.dispatch("[1, 2, 3]")

    # ── CSV input tests ──

    def test_csv_happy_path_110_columns(self) -> None:
        """110-column STM32 CSV should parse into a measurement message."""
        cols = ["0"] * 110
        cols[0] = "M"
        # RTC + 日期时间 (1-3)
        cols[1] = "1"              # rtc_valid
        cols[2] = "20260409"       # date
        cols[3] = "120000"         # time
        # PPG 原始信号 (4-7)
        cols[4] = "1200"           # red
        cols[5] = "2400"           # ir
        cols[6] = "2200"           # baseline_ir
        cols[7] = "1"              # finger
        # valid/value 成对，valid 在前（列 8-15）
        cols[8] = "1"      # bpm_valid
        cols[9] = "72"     # bpm
        cols[10] = "1"     # spo2_valid
        cols[11] = "98"    # spo2
        cols[12] = "1"     # rr_valid
        cols[13] = "16"    # rr
        cols[14] = "1"     # ibi_valid
        cols[15] = "820"   # ibi
        # 信号质量 + 原始信号 (30-31)
        cols[30] = "85"    # signal_quality
        cols[31] = "1"     # raw_signal_present
        # SD / Display / Debug (64-71)
        cols[64] = "1"     # sd_log_active
        cols[70] = "0"     # debug_mode
        cols[71] = "3"     # current_page
        # ECG (72-77)
        cols[72] = "1"     # ecg_valid
        cols[73] = "72"    # ecg_hr
        cols[74] = "830"   # ecg_rr_ms
        cols[77] = "2100"  # ecg_filtered
        # PTT (78-79)
        cols[78] = "1"     # ptt_valid
        cols[79] = "250"   # ptt_ms
        # 任务心跳 (100-101)
        cols[100] = "3"    # max_task_heartbeat
        cols[101] = "5"    # ui_task_heartbeat
        # ECG 质量字段 (102-109, v3 新增)
        cols[102] = "80"   # ecg_signal_quality
        cols[103] = "0"    # ecg_invalid_reason
        cols[104] = "500"  # ecg_raw_span
        cols[105] = "300"  # ecg_filtered_span
        cols[106] = "20"   # ecg_noise_level
        cols[107] = "150"  # ecg_qrs_threshold
        cols[108] = "95"   # ecg_peak_snr_x100
        cols[109] = "200"  # ecg_dma_available_high_watermark
        csv_line = ",".join(cols)

        msg = self.dispatcher.dispatch(csv_line)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.message_type, "measurement")
        self.assertEqual(msg.protocol, "csv")
        # RTC
        self.assertTrue(msg.rtc_valid)
        self.assertEqual(msg.date, "20260409")
        self.assertEqual(msg.time, "120000")
        # PPG 原始信号
        self.assertEqual(msg.red, 1200)
        self.assertEqual(msg.ir, 2400)
        self.assertEqual(msg.baseline_ir, 2200)
        self.assertTrue(msg.finger)
        # 核心生命体征
        self.assertTrue(msg.bpm_valid)
        self.assertEqual(msg.bpm, 72)
        self.assertTrue(msg.spo2_valid)
        self.assertEqual(msg.spo2, 98)
        self.assertTrue(msg.rr_valid)
        self.assertEqual(msg.rr, 16)
        self.assertTrue(msg.ibi_valid)
        self.assertEqual(msg.ibi, 820)
        # 信号质量
        self.assertEqual(msg.signal_quality, 85)
        self.assertTrue(msg.raw_signal_present)
        # 系统诊断
        self.assertTrue(msg.sd_log_active)
        self.assertFalse(msg.debug_mode)
        self.assertEqual(msg.current_page, 3)
        # ECG / PTT
        self.assertTrue(msg.ecg_valid)
        self.assertEqual(msg.ecg_hr, 72)
        self.assertEqual(msg.ecg_rr_ms, 830)
        self.assertEqual(msg.ecg_filtered, 2100)
        self.assertTrue(msg.ptt_valid)
        self.assertEqual(msg.ptt_ms, 250)
        # ECG 质量字段 (v3)
        self.assertEqual(msg.ecg_signal_quality, 80)
        self.assertEqual(msg.ecg_invalid_reason, 0)
        self.assertEqual(msg.ecg_raw_span, 500)
        self.assertEqual(msg.ecg_filtered_span, 300)
        self.assertEqual(msg.ecg_noise_level, 20)
        self.assertEqual(msg.ecg_qrs_threshold, 150)
        self.assertEqual(msg.ecg_peak_snr_x100, 95)
        self.assertEqual(msg.ecg_dma_available_high_watermark, 200)
        # 元信息 (rx_ms 不是 CSV 列，CSV 消息中应为 None)
        self.assertEqual(msg.field_count, 110)
        self.assertIsNone(msg.rx_ms)
        self.assertTrue(msg.parse_ok)
        self.assertIsNone(msg.detect_schema_issue())

    def test_csv_wrong_column_count_warns(self) -> None:
        """CSV with wrong column count should still parse but with warnings."""
        cols = ["0"] * 81
        cols[0] = "M"
        cols[8] = "1"     # bpm_valid
        cols[9] = "70"    # bpm
        cols[10] = "1"    # spo2_valid
        cols[11] = "96"   # spo2
        cols[30] = "50"   # signal_quality
        csv_line = ",".join(cols)

        msg = self.dispatcher.dispatch(csv_line)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.field_count, 81)
        self.assertFalse(msg.parse_ok)
        self.assertIn("columns=81", str(msg.parse_warnings))
        # schema issue should be detected due to field_count < 90 AND parse_ok=False
        issue = msg.detect_schema_issue()
        self.assertIsNotNone(issue)
        self.assertIn("field_count=81", issue)
        self.assertIn("parse_ok=False", issue)

    def test_csv_with_dash_sentinels_ignored(self) -> None:
        """-- and empty values in CSV should be treated as missing."""
        cols = [""] * 110  # empty strings are sentinels → all fields default
        cols[0] = "M"
        # bpm_valid (col 8) = "--" → sentinel → default False
        cols[8] = "--"
        # bpm (col 9) = "--" → sentinel → None
        cols[9] = "--"
        # spo2_valid (col 10) = "" → sentinel → default False
        cols[10] = ""
        # spo2 (col 11) = "" → sentinel → None
        cols[11] = ""
        # signal_quality (col 30) = "--" → sentinel → None
        cols[30] = "--"
        csv_line = ",".join(cols)

        msg = self.dispatcher.dispatch(csv_line)
        self.assertIsNotNone(msg)
        self.assertIsNone(msg.bpm)
        self.assertFalse(msg.bpm_valid)
        self.assertIsNone(msg.spo2)
        self.assertFalse(msg.spo2_valid)
        self.assertIsNone(msg.signal_quality)

    def test_csv_not_starting_with_m_raises(self) -> None:
        """CSV not starting with M, prefix should fall through to JSON parser."""
        with self.assertRaises(MessageValidationError):
            self.dispatcher.dispatch("X,1,2,3")

    # ── CSV column-mapping correctness tests ──

    def test_csv_valid_value_ordering_8_15(self) -> None:
        """列 8-15: valid 在前，value 在后。"""
        cols = [""] * 110
        cols[0] = "M"
        cols[8] = "1"     # bpm_valid
        cols[9] = "75"    # bpm
        cols[10] = "1"    # spo2_valid
        cols[11] = "97"   # spo2
        cols[12] = "0"    # rr_valid
        cols[13] = "20"   # rr
        cols[14] = "1"    # ibi_valid
        cols[15] = "800"  # ibi
        csv_line = ",".join(cols)

        msg = self.dispatcher.dispatch(csv_line)
        self.assertIsNotNone(msg)
        self.assertTrue(msg.bpm_valid)
        self.assertEqual(msg.bpm, 75)
        self.assertTrue(msg.spo2_valid)
        self.assertEqual(msg.spo2, 97)
        self.assertFalse(msg.rr_valid)
        self.assertEqual(msg.rr, 20)
        self.assertTrue(msg.ibi_valid)
        self.assertEqual(msg.ibi, 800)

    def test_csv_signal_quality_block_30_38(self) -> None:
        """列 30-38: signal_quality, raw_signal_present, PI, AC RMS, ratio, balance."""
        cols = [""] * 110
        cols[0] = "M"
        cols[30] = "90"    # signal_quality
        cols[31] = "1"     # raw_signal_present
        cols[32] = "500"   # signal_ir_pi_x1000
        cols[33] = "300"   # signal_red_pi_x1000
        cols[34] = "1200"  # signal_ir_ac_rms
        cols[35] = "800"   # signal_red_ac_rms
        cols[36] = "1"     # spo2_ratio_valid
        cols[37] = "850"   # spo2_ratio_x1000
        cols[38] = "2"     # spo2_balance_status
        csv_line = ",".join(cols)

        msg = self.dispatcher.dispatch(csv_line)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.signal_quality, 90)
        self.assertTrue(msg.raw_signal_present)
        self.assertEqual(msg.signal_ir_pi_x1000, 500)
        self.assertEqual(msg.signal_red_pi_x1000, 300)
        self.assertEqual(msg.signal_ir_ac_rms, 1200)
        self.assertEqual(msg.signal_red_ac_rms, 800)
        self.assertTrue(msg.spo2_ratio_valid)
        self.assertEqual(msg.spo2_ratio_x1000, 850)
        self.assertEqual(msg.spo2_balance_status, 2)

    def test_csv_system_diagnostics_61_71(self) -> None:
        """列 61-71: RTC/UART/SD/Display/Debug/current_page."""
        cols = [""] * 110
        cols[0] = "M"
        cols[61] = "1"     # rtc_read_ok
        cols[62] = "1"     # uart_rx_message_valid
        cols[63] = "1"     # uart_tx_message_valid
        cols[64] = "1"     # sd_log_active
        cols[65] = "2"     # sd_state
        cols[66] = "0"     # sd_error
        cols[67] = "5000"  # sd_total_written
        cols[68] = "42"    # display_refresh_count
        cols[69] = "100"   # display_last_refresh_tick
        cols[70] = "0"     # debug_mode
        cols[71] = "2"     # current_page
        csv_line = ",".join(cols)

        msg = self.dispatcher.dispatch(csv_line)
        self.assertIsNotNone(msg)
        self.assertTrue(msg.sd_log_active)
        self.assertEqual(msg.sd_state, 2)
        self.assertEqual(msg.sd_error, 0)
        self.assertEqual(msg.sd_total_written, 5000)
        self.assertEqual(msg.display_refresh_count, 42)
        self.assertEqual(msg.display_last_refresh_tick, 100)
        self.assertFalse(msg.debug_mode)
        self.assertEqual(msg.current_page, 2)

    def test_csv_ecg_ptt_72_84(self) -> None:
        """列 72-84: ECG (72-77) + PTT (78-79) + ECG 计数器 (80-84)。"""
        cols = [""] * 110
        cols[0] = "M"
        # ECG (72-77)
        cols[72] = "1"     # ecg_valid
        cols[73] = "68"    # ecg_hr
        cols[74] = "882"   # ecg_rr_ms
        cols[75] = "0"     # ecg_lead_off
        cols[76] = "150"   # ecg_r_peak_ms
        cols[77] = "2100"  # ecg_filtered
        # PTT (78-79)
        cols[78] = "1"     # ptt_valid
        cols[79] = "240"   # ptt_ms
        # ECG 计数器 (80-84)
        cols[80] = "100"   # ecg_sample_count
        cols[81] = "2"     # ecg_adc_sat_count
        cols[82] = "1"     # ecg_dma_overflow_count
        cols[83] = "3"     # ecg_lead_off_count
        cols[84] = "0"     # ecg_no_r_peak_timeout_count
        csv_line = ",".join(cols)

        msg = self.dispatcher.dispatch(csv_line)
        self.assertIsNotNone(msg)
        # ECG
        self.assertTrue(msg.ecg_valid)
        self.assertEqual(msg.ecg_hr, 68)
        self.assertEqual(msg.ecg_rr_ms, 882)
        self.assertEqual(msg.ecg_lead_off, 0)
        self.assertEqual(msg.ecg_filtered, 2100)
        # ecg_raw 不是 CSV 列，应为 None
        self.assertIsNone(msg.ecg_raw)
        # PTT
        self.assertTrue(msg.ptt_valid)
        self.assertEqual(msg.ptt_ms, 240)

    def test_csv_heartbeat_100_101(self) -> None:
        """列 100-101: max_task_heartbeat 和 ui_task_heartbeat（非 rx_ms/parse_ok）。"""
        cols = [""] * 110
        cols[0] = "M"
        cols[100] = "12"   # max_task_heartbeat
        cols[101] = "8"    # ui_task_heartbeat
        csv_line = ",".join(cols)

        msg = self.dispatcher.dispatch(csv_line)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.get_int("max_task_heartbeat"), 12)
        self.assertEqual(msg.get_int("ui_task_heartbeat"), 8)
        # rx_ms 不在 CSV 中，CSV 消息应为 None
        self.assertIsNone(msg.rx_ms)
        self.assertIsNone(msg.raw_signal_present)

    def test_csv_rtc_and_ppg_1_7(self) -> None:
        """列 1-7: rtc_valid, date, time + red, ir, baseline_ir, finger。"""
        cols = [""] * 110
        cols[0] = "M"
        cols[1] = "1"           # rtc_valid
        cols[2] = "20260409"    # date (CSV 按 int 解析，get_str 转回字符串)
        cols[3] = "120000"      # time
        cols[4] = "3000"        # red
        cols[5] = "5000"        # ir
        cols[6] = "3200"        # baseline_ir
        cols[7] = "0"           # finger (0 = 离位)
        csv_line = ",".join(cols)

        msg = self.dispatcher.dispatch(csv_line)
        self.assertIsNotNone(msg)
        self.assertTrue(msg.rtc_valid)
        self.assertIsNotNone(msg.date)
        self.assertIsNotNone(msg.time)
        self.assertEqual(msg.red, 3000)
        self.assertEqual(msg.ir, 5000)
        self.assertEqual(msg.baseline_ir, 3200)
        self.assertFalse(msg.finger)  # col 7 = 0

    # ── Schema detection tests ──

    def test_old_schema_detected_via_field_count(self) -> None:
        """field_count < 90 应触发 schema 问题检测。"""
        payload = json.dumps({
            "message": "measurement",
            "bridge": "gw-01",
            "source": "sensor-a",
            "channel": "1",
            "protocol": "mqtt",
            "frame": "100",
            "rtc_valid": True, "date": "20260409", "time": "120000",
            "red": 100, "ir": 200, "baseline_ir": 180, "finger": True,
            "bpm_valid": True, "bpm": 72, "spo2_valid": True, "spo2": 98,
            "field_count": 81,
            "parse_warnings": [],
            "extra_fields": [],
        })
        msg = self.dispatcher.dispatch(payload)
        self.assertIsNotNone(msg)
        issue = msg.detect_schema_issue()
        self.assertIsNotNone(issue)
        self.assertIn("81", issue)
        self.assertIn("疑似", issue)

    def test_schema_v1_x_detected(self) -> None:
        """schema_version starting with '1.' should be detected as old."""
        payload = json.dumps({
            "message": "measurement",
            "rtc_valid": True, "date": "20260409", "time": "120000",
            "red": 100, "ir": 200, "baseline_ir": 180, "finger": True,
            "bpm_valid": True, "bpm": 72, "spo2_valid": True, "spo2": 98,
            "field_count": 110,
            "schema_version": "1.0",
            "parse_warnings": [],
            "extra_fields": [],
        })
        msg = self.dispatcher.dispatch(payload)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.schema_version, "1.0")
        issue = msg.detect_schema_issue()
        self.assertIsNotNone(issue)
        self.assertIn("v1.x", issue)

    def test_legacy_102_schema_detected(self) -> None:
        """field_count=102 / schema_version=2 should trigger legacy 102 warning."""
        payload = json.dumps({
            "message": "measurement",
            "rtc_valid": True, "date": "20260409", "time": "120000",
            "red": 100, "ir": 200, "baseline_ir": 180, "finger": True,
            "bpm_valid": True, "bpm": 72, "spo2_valid": True, "spo2": 98,
            "ecg_valid": True, "ecg_hr": 72,
            "signal_quality": 85,
            "field_count": 102,
            "schema_version": "2.0",
            "parse_warnings": [],
            "extra_fields": [],
        })
        msg = self.dispatcher.dispatch(payload)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.field_count, 102)
        issue = msg.detect_schema_issue()
        self.assertIsNotNone(issue)
        self.assertIn("旧 102 列", issue)

    def test_new_schema_no_issue(self) -> None:
        """New 110-field schema with v3.x should have no issue."""
        payload = json.dumps({
            "message": "measurement",
            "rtc_valid": True, "date": "20260409", "time": "120000",
            "red": 100, "ir": 200, "baseline_ir": 180, "finger": True,
            "bpm_valid": True, "bpm": 72, "spo2_valid": True, "spo2": 98,
            "ecg_valid": True, "ecg_hr": 72, "ecg_rr_ms": 830,
            "signal_quality": 85,
            "field_count": 110,
            "schema_version": "3.0",
            "parse_warnings": [],
            "extra_fields": [],
        })
        msg = self.dispatcher.dispatch(payload)
        self.assertIsNotNone(msg)
        issue = msg.detect_schema_issue()
        self.assertIsNone(issue)

    def test_json_schema_v3_110_core_fields(self) -> None:
        """schema_version=3 / field_count=110 JSON fixture exposes current core fields."""
        payload = json.dumps({
            "message": "measurement",
            "protocol": "mqtt",
            "schema_version": 3,
            "parse_ok": True,
            "field_count": 110,
            "parse_warnings": [],
            "extra_fields": ["col110=tail"],
            "rtc_valid": True, "date": "20260409", "time": "120000",
            "finger": True,
            "bpm_valid": True, "bpm": 72,
            "spo2_valid": True, "spo2": 98,
            "rr_valid": True, "rr": 16,
            "ibi_valid": True, "ibi": 820,
            "hrv_valid": True, "mean_ibi": 818, "sdnn": 42, "rmssd": 31,
            "signal_quality": 85,
            "ecg_valid": True, "ecg_hr": 71, "ecg_rr_ms": 845,
            "ptt_valid": True, "ptt_ms": 246,
            "sd_log_active": True, "sd_state": 2, "sd_error": 0,
            "debug_mode": False, "current_page": 3,
            "crash_flag": False, "crash_source": 0, "crash_task": 1,
            "crash_phase": 2, "crash_tick": 1234, "reset_flags": 8,
            "max_task_phase": 10, "ui_task_phase": 11,
            "sd_task_phase": 12, "wdt_task_phase": 13,
            "max_task_stack_hwm": 1000, "ui_task_stack_hwm": 900,
            "sd_task_stack_hwm": 800, "wdt_task_stack_hwm": 700,
            "max_task_heartbeat": 5, "ui_task_heartbeat": 6,
            # ECG 质量字段 (v3)
            "ecg_signal_quality": 80,
            "ecg_invalid_reason": 0,
            "ecg_peak_snr_x100": 95,
        })
        msg = self.dispatcher.dispatch(payload)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.schema_version, "3")
        self.assertEqual(msg.field_count, 110)
        self.assertIsNone(msg.detect_schema_issue())
        self.assertEqual(msg.bpm, 72)
        self.assertEqual(msg.spo2, 98)
        self.assertEqual(msg.rr, 16)
        self.assertEqual(msg.ibi, 820)
        self.assertEqual(msg.sdnn, 42)
        self.assertEqual(msg.rmssd, 31)
        self.assertTrue(msg.finger)
        self.assertEqual(msg.signal_quality, 85)
        self.assertEqual(msg.ecg_hr, 71)
        self.assertEqual(msg.ecg_rr_ms, 845)
        self.assertEqual(msg.ptt_ms, 246)
        self.assertTrue(msg.sd_log_active)
        self.assertEqual(msg.sd_state, 2)
        self.assertEqual(msg.sd_error, 0)
        self.assertFalse(msg.debug_mode)
        self.assertEqual(msg.current_page, 3)
        self.assertEqual(msg.crash_task, 1)
        self.assertEqual(msg.reset_flags, 8)
        self.assertEqual(msg.max_task_stack_hwm, 1000)
        self.assertEqual(msg.ui_task_heartbeat, 6)
        # ECG 质量字段 (v3)
        self.assertEqual(msg.ecg_signal_quality, 80)
        self.assertEqual(msg.ecg_invalid_reason, 0)
        self.assertEqual(msg.ecg_peak_snr_x100, 95)

    # ── Edge case value handling ──

    def test_sentinel_values_become_none(self) -> None:
        """--, N/A, empty, null strings should become None."""
        payload = json.dumps({
            "message": "measurement",
            "rtc_valid": True, "date": "20260409", "time": "120000",
            "red": "--", "ir": "N/A", "baseline_ir": "NA", "finger": True,
            "bpm_valid": True, "bpm": "", "spo2_valid": True, "spo2": "null",
            "signal_quality": "--",
        })
        msg = self.dispatcher.dispatch(payload)
        self.assertIsNotNone(msg)
        self.assertIsNone(msg.red)
        self.assertIsNone(msg.ir)
        self.assertIsNone(msg.baseline_ir)
        self.assertIsNone(msg.bpm)
        self.assertIsNone(msg.spo2)
        self.assertIsNone(msg.signal_quality)

    def test_nonnumeric_string_does_not_raise(self) -> None:
        """Non-numeric strings for numeric fields should return None, no exception."""
        payload = json.dumps({
            "message": "measurement",
            "rtc_valid": True, "date": "20260409", "time": "120000",
            "red": "abc", "ir": "xyz", "finger": True,
            "bpm_valid": True, "bpm": "hello",
            "spo2_valid": True, "spo2": "---",
        })
        msg = self.dispatcher.dispatch(payload)
        self.assertIsNotNone(msg)
        self.assertIsNone(msg.red)
        self.assertIsNone(msg.ir)
        self.assertIsNone(msg.bpm)
        self.assertIsNone(msg.spo2)

    def test_extra_fields_preserved(self) -> None:
        """Unknown fields in JSON should be preserved in raw dict."""
        payload = json.dumps({
            "message": "measurement",
            "rtc_valid": True, "date": "20260409", "time": "120000",
            "red": 100, "ir": 200, "finger": True,
            "bpm_valid": True, "bpm": 72, "spo2_valid": True, "spo2": 98,
            "extra_fields": ["col95=123", "col96=456"],
            "unknown_sensor_value": 999,
        })
        msg = self.dispatcher.dispatch(payload)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.extra_fields, ["col95=123", "col96=456"])
        self.assertIn("unknown_sensor_value", msg.raw)

    def test_parse_warnings_propagated(self) -> None:
        """ESP32 parse_warnings should be accessible."""
        payload = json.dumps({
            "message": "measurement",
            "rtc_valid": True, "date": "20260409", "time": "120000",
            "red": 100, "ir": 200, "finger": True,
            "bpm_valid": True, "bpm": 72, "spo2_valid": True, "spo2": 98,
            "parse_ok": True,
            "field_count": 110,
            "parse_warnings": ["unexpected trailing data", "CRC mismatch"],
            "extra_fields": [],
        })
        msg = self.dispatcher.dispatch(payload)
        self.assertIsNotNone(msg)
        self.assertEqual(len(msg.parse_warnings), 2)
        self.assertIn("CRC mismatch", msg.parse_warnings)

    def test_parse_warnings_string_detected(self) -> None:
        """Non-empty parse_warnings string should still raise a schema warning."""
        payload = json.dumps({
            "message": "measurement",
            "parse_ok": True,
            "field_count": 110,
            "schema_version": 3,
            "parse_warnings": "CRC mismatch",
        })
        msg = self.dispatcher.dispatch(payload)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.parse_warnings, ["CRC mismatch"])
        self.assertIn("parse_warnings", msg.detect_schema_issue())

    def test_numeric_old_schema_version_detected(self) -> None:
        """schema_version=1 数字形式也应视为旧版。"""
        payload = json.dumps({
            "message": "measurement",
            "parse_ok": True,
            "field_count": 110,
            "schema_version": 1,
            "parse_warnings": [],
        })
        msg = self.dispatcher.dispatch(payload)
        self.assertIsNotNone(msg)
        issue = msg.detect_schema_issue()
        self.assertIsNotNone(issue)
        self.assertIn("schema_version=1", issue)

    def test_parse_error_raw_aliases_to_raw_line(self) -> None:
        """parse_error.raw should be preserved through raw_line for Diagnostics."""
        payload = json.dumps({
            "message": "parse_error",
            "error": "bad field count",
            "raw": "M,old,raw,line",
        })
        msg = self.dispatcher.dispatch(payload)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.message_type, "parse_error")
        self.assertEqual(msg.raw_line, "M,old,raw,line")

    # ── New system property tests ──

    def test_system_diagnostic_properties(self) -> None:
        """Current system diagnostic fields should be parsed correctly."""
        payload = json.dumps({
            "message": "measurement",
            "rtc_valid": True, "date": "20260409", "time": "120000",
            "red": 100, "ir": 200, "finger": True,
            "bpm_valid": True, "bpm": 72, "spo2_valid": True, "spo2": 98,
            "sd_log_active": True,
            "sd_state": 2,
            "sd_error": 0,
            "sd_total_written": 12345,
            "debug_mode": False,
            "current_page": 3,
            "crash_flag": False,
            "crash_source": 0,
            "reboot_count": 5,
        })
        msg = self.dispatcher.dispatch(payload)
        self.assertIsNotNone(msg)
        self.assertTrue(msg.sd_log_active)
        self.assertEqual(msg.sd_state, 2)
        self.assertEqual(msg.sd_error, 0)
        self.assertEqual(msg.sd_total_written, 12345)
        self.assertFalse(msg.debug_mode)
        self.assertEqual(msg.current_page, 3)
        self.assertFalse(msg.crash_flag)
        self.assertEqual(msg.crash_source, 0)
        self.assertEqual(msg.reboot_count, 5)


    # ── Payload normalization tests ──

    def test_missing_message_inferred_from_measurement_fields(self) -> None:
        """缺失 message 但有 bpm/spo2/red/ir 时应推断为 measurement。"""
        payload = json.dumps({
            "bridge": "gw-01",
            "rtc_valid": True, "date": "20260409", "time": "120000",
            "red": 100, "ir": 200, "finger": True,
            "bpm_valid": True, "bpm": 72, "spo2_valid": True, "spo2": 98,
        })
        msg = self.dispatcher.dispatch(payload)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.message_type, "measurement")
        self.assertEqual(msg.bpm, 72)
        self.assertEqual(msg.spo2, 98)

    def test_modules_bpm_value_flattened_to_top_level(self) -> None:
        """modules.bpm.value 应被展平为 bpm，modules.bpm.valid 展平为 bpm_valid。"""
        payload = json.dumps({
            "bridge": "gw-01",
            "rtc_valid": True, "date": "20260409", "time": "120000",
            "red": 100, "ir": 200, "finger": True,
            "message": "measurement",
            "modules": {
                "bpm": {"available": True, "valid": True, "value": 72},
                "spo2": {"available": True, "valid": True, "value": 98},
                "rr": {"available": True, "valid": True, "value": 18},
                "ibi": {"available": True, "valid": True, "value": 833},
            },
        })
        msg = self.dispatcher.dispatch(payload)
        self.assertIsNotNone(msg)
        # 注意：modules 展平不覆盖已有顶层字段，此处顶层无 bpm/spo2 所以用 modules 的
        self.assertEqual(msg.bpm, 72)
        self.assertTrue(msg.bpm_valid)
        self.assertEqual(msg.spo2, 98)
        self.assertTrue(msg.spo2_valid)
        self.assertEqual(msg.rr, 18)
        self.assertTrue(msg.rr_valid)
        self.assertEqual(msg.ibi, 833)
        self.assertTrue(msg.ibi_valid)

    def test_modules_does_not_overwrite_existing_top_level(self) -> None:
        """modules 展平时不应覆盖已有顶层字段。"""
        payload = json.dumps({
            "bridge": "gw-01",
            "message": "measurement",
            "bpm": 72, "bpm_valid": True,
            "modules": {
                "bpm": {"value": 999, "valid": False},
            },
        })
        msg = self.dispatcher.dispatch(payload)
        self.assertIsNotNone(msg)
        # 顶层已有 bpm=72，不应被 modules.bpm.value=999 覆盖
        self.assertEqual(msg.bpm, 72)
        self.assertTrue(msg.bpm_valid)

    def test_field_aliases_are_normalized_to_canonical_fields(self) -> None:
        """常见设备端字段别名应补齐为 GUI 使用的标准字段。"""
        payload = json.dumps({
            "bridge": "gw-01",
            "rtc_valid": False,
            "heartRate": {"value": "74"},
            "bpmValid": "true",
            "SpO2": [96, "97"],
            "spo2Valid": "true",
            "redRaw": {"value": "101"},
            "irRaw": [200, 201],
            "signalQuality": "88",
        })
        msg = self.dispatcher.dispatch(payload)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.message_type, "measurement")
        self.assertEqual(msg.bpm, 74)
        self.assertTrue(msg.bpm_valid)
        self.assertEqual(msg.spo2, 97)
        self.assertTrue(msg.spo2_valid)
        self.assertEqual(msg.red, 101)
        self.assertEqual(msg.ir, 201)
        self.assertEqual(msg.signal_quality, 88)

    def test_module_alias_names_are_normalized(self) -> None:
        """modules 中使用别名模块名时也应映射到标准字段。"""
        payload = json.dumps({
            "bridge": "gw-01",
            "message": "measurement",
            "modules": {
                "heart_rate": {"value": 75, "valid": True},
                "SpO2": {"value": 96, "valid": True},
            },
        })
        msg = self.dispatcher.dispatch(payload)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.bpm, 75)
        self.assertTrue(msg.bpm_valid)
        self.assertEqual(msg.spo2, 96)
        self.assertTrue(msg.spo2_valid)

    def test_esp_status_top_level_fields(self) -> None:
        """ESP32 USB/MQTT 状态字段应支持顶层布尔形式。"""
        payload = json.dumps({
            "message": "esp_status",
            "esp_usb_connected": True,
            "esp_mqtt_connected": False,
            "esp_transport_mode": "usb",
        })
        msg = self.dispatcher.dispatch(payload)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.message_type, "esp_status")
        self.assertTrue(msg.esp_usb_connected)
        self.assertFalse(msg.esp_mqtt_connected)
        self.assertEqual(msg.esp_transport_mode, "USB")

    def test_esp_status_nested_alias_fields(self) -> None:
        """ESP32 USB/MQTT 状态字段应支持嵌套别名形式。"""
        payload = json.dumps({
            "message": "esp_status",
            "esp_status": {
                "usb": {"connected": "connected"},
                "mqtt": {"connected": "offline"},
                "active_transport": "MQTT",
            },
        })
        msg = self.dispatcher.dispatch(payload)
        self.assertIsNotNone(msg)
        self.assertTrue(msg.esp_usb_connected)
        self.assertFalse(msg.esp_mqtt_connected)
        self.assertEqual(msg.esp_transport_mode, "MQTT")

    def test_esp_status_v1_schema_fields(self) -> None:
        """esp-status-v1 状态消息应按新协议解析。"""
        payload = json.dumps({
            "bridge": "esp32c3",
            "source": "esp32",
            "channel": "status",
            "protocol": "esp-status-v1",
            "schema_version": 1,
            "message": "esp_status",
            "online": True,
            "uptime_ms": 123456,
            "transport": {
                "active": "usb",
                "usb_connected": True,
                "usb_active": True,
                "mqtt_connected": True,
            },
            "usb": {
                "connected": True,
                "active": True,
                "session_timeout_ms": 15000,
            },
            "wifi": {
                "started": True,
                "connected": True,
                "state": "ok",
            },
            "mqtt": {
                "started": True,
                "connected": True,
                "subscribed": True,
                "state": "ok",
                "status_topic": "pulseox/status",
                "data_topic": "pulseox/data",
                "command_topic": "pulseox/cmd",
            },
            "stm32": {
                "protocol_state": "ok",
                "last_frame": "M",
                "last_frame_ms": 123000,
            },
            "counters": {
                "protocol_ok": 10,
                "protocol_error": 0,
            },
        })
        msg = self.dispatcher.dispatch(payload)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.message_type, "esp_status")
        self.assertTrue(msg.esp_online)
        self.assertTrue(msg.esp_usb_active)
        self.assertTrue(msg.esp_usb_connected)
        self.assertTrue(msg.esp_mqtt_connected)
        self.assertTrue(msg.esp_mqtt_subscribed)
        self.assertTrue(msg.esp_wifi_connected)
        self.assertEqual(msg.esp_transport_active, "usb")
        self.assertEqual(msg.esp_transport_mode, "USB")
        self.assertEqual(msg.esp_stm32_protocol_state, "ok")
        self.assertEqual(msg.esp_stm32_last_frame, "M")
        self.assertEqual(msg.esp_stm32_last_frame_ms, 123000)
        self.assertEqual(msg.esp_protocol_ok_count, 10)
        self.assertEqual(msg.esp_protocol_error_count, 0)

    def test_esp_status_top_level_protocol_aliases(self) -> None:
        """esp_status should also accept flat protocol fields from bridge firmware."""
        payload = json.dumps({
            "message": "esp_status",
            "online": True,
            "esp_stm32_protocol_state": "ok",
            "esp_stm32_last_frame": "M",
            "esp_stm32_last_frame_ms": 3210,
            "protocol_ok": 12,
            "protocol_error": 2,
        })
        msg = self.dispatcher.dispatch(payload)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.message_type, "esp_status")
        self.assertEqual(msg.esp_stm32_protocol_state, "ok")
        self.assertEqual(msg.esp_stm32_last_frame, "M")
        self.assertEqual(msg.esp_stm32_last_frame_ms, 3210)
        self.assertEqual(msg.esp_protocol_ok_count, 12)
        self.assertEqual(msg.esp_protocol_error_count, 2)

    def test_esp_status_last_will_offline(self) -> None:
        """MQTT LWT retained online=false 应解析为 ESP 离线。"""
        payload = json.dumps({
            "bridge": "esp32c3",
            "source": "esp32",
            "channel": "status",
            "protocol": "esp-status-v1",
            "schema_version": 1,
            "message": "esp_status",
            "online": False,
        })
        msg = self.dispatcher.dispatch(payload)
        self.assertIsNotNone(msg)
        self.assertFalse(msg.esp_online)


if __name__ == "__main__":
    unittest.main()
