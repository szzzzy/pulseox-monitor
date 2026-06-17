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

    def test_csv_happy_path_102_columns(self) -> None:
        """102-column STM32 CSV should parse into a measurement message."""
        cols = ["0"] * 102  # "0" filler avoids trailing-comma artifact in join
        cols[0] = "M"
        cols[8] = "72"
        cols[9] = "1"
        cols[10] = "98"
        cols[11] = "1"
        cols[12] = "16"
        cols[13] = "1"
        cols[14] = "820"
        cols[15] = "1"
        cols[30] = "85"
        cols[72] = "1"
        cols[73] = "72"
        cols[74] = "830"
        cols[77] = "2100"
        cols[78] = "1"
        cols[79] = "250"
        cols[80] = "1"
        cols[86] = "0"
        cols[87] = "1"
        csv_line = ",".join(cols)

        msg = self.dispatcher.dispatch(csv_line)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.message_type, "measurement")
        self.assertEqual(msg.protocol, "csv")
        self.assertEqual(msg.bpm, 72)
        self.assertTrue(msg.bpm_valid)
        self.assertEqual(msg.spo2, 98)
        self.assertTrue(msg.spo2_valid)
        self.assertEqual(msg.rr, 16)
        self.assertEqual(msg.ibi, 820)
        self.assertEqual(msg.signal_quality, 85)
        self.assertEqual(msg.ecg_hr, 72)
        self.assertEqual(msg.ecg_rr_ms, 830)
        self.assertEqual(msg.ecg_filtered, 2100)
        self.assertTrue(msg.ecg_valid)
        self.assertEqual(msg.ptt_ms, 250)
        self.assertTrue(msg.ptt_valid)
        self.assertEqual(msg.field_count, 102)
        self.assertTrue(msg.parse_ok)

    def test_csv_wrong_column_count_warns(self) -> None:
        """CSV with wrong column count should still parse but with warnings."""
        cols = ["0"] * 81  # "0" filler avoids trailing-comma artifact in join
        cols[0] = "M"
        cols[8] = "70"
        cols[9] = "1"
        cols[10] = "96"
        cols[11] = "1"
        cols[30] = "50"
        csv_line = ",".join(cols)

        msg = self.dispatcher.dispatch(csv_line)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.field_count, 81)
        self.assertFalse(msg.parse_ok)
        self.assertIn("columns=81", str(msg.parse_warnings))

    def test_csv_with_dash_sentinels_ignored(self) -> None:
        """-- and empty values in CSV should be treated as missing."""
        cols = ["0"] * 102  # "0" filler avoids trailing-comma artifact in join
        cols[0] = "M"
        cols[8] = "--"
        cols[9] = "0"
        cols[10] = ""
        cols[11] = "0"
        cols[30] = "--"
        csv_line = ",".join(cols)

        msg = self.dispatcher.dispatch(csv_line)
        self.assertIsNotNone(msg)
        self.assertIsNone(msg.bpm)
        self.assertFalse(msg.bpm_valid)
        self.assertIsNone(msg.spo2)
        self.assertIsNone(msg.signal_quality)

    def test_csv_not_starting_with_m_raises(self) -> None:
        """CSV not starting with M, prefix should fall through to JSON parser."""
        with self.assertRaises(MessageValidationError):
            self.dispatcher.dispatch("X,1,2,3")

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
            "field_count": 102,
            "schema_version": "1.0",
            "parse_warnings": [],
            "extra_fields": [],
        })
        msg = self.dispatcher.dispatch(payload)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.schema_version, "1.0")
        issue = msg.detect_schema_issue()
        self.assertIsNotNone(issue)
        self.assertIn("v1.x", issue)  # 中英文均包含 "v1.x"

    def test_new_schema_no_issue(self) -> None:
        """New 102-field schema with v2.x should have no issue."""
        payload = json.dumps({
            "message": "measurement",
            "rtc_valid": True, "date": "20260409", "time": "120000",
            "red": 100, "ir": 200, "baseline_ir": 180, "finger": True,
            "bpm_valid": True, "bpm": 72, "spo2_valid": True, "spo2": 98,
            "ecg_valid": True, "ecg_hr": 72, "ecg_rr_ms": 830,
            "signal_quality": 85,
            "field_count": 102,
            "schema_version": "2.0",
            "parse_warnings": [],
            "extra_fields": [],
        })
        msg = self.dispatcher.dispatch(payload)
        self.assertIsNotNone(msg)
        issue = msg.detect_schema_issue()
        self.assertIsNone(issue)

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
            "field_count": 102,
            "parse_warnings": ["unexpected trailing data", "CRC mismatch"],
            "extra_fields": [],
        })
        msg = self.dispatcher.dispatch(payload)
        self.assertIsNotNone(msg)
        self.assertEqual(len(msg.parse_warnings), 2)
        self.assertIn("CRC mismatch", msg.parse_warnings)

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
