# 验证消息分发器是否严格遵循 MQTT 协议约定。

from __future__ import annotations

import json
import unittest
from datetime import datetime

from pulseox_monitor.dispatcher import MessageDispatcher
from pulseox_monitor.models import (
    MessageValidationError,
    MeasurementMessage,
    ParseErrorMessage,
    RtcSetAckMessage,
)


# 验证消息分发器是否按协议正确解析不同消息类型。
class DispatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        # 为每个测试准备统一的分发器与固定接收时间。
        self.dispatcher = MessageDispatcher()
        self.received_at = datetime(2026, 4, 9, 12, 0, 0)

    def test_dispatch_measurement(self) -> None:
        # 普通 measurement 消息应被解析成 MeasurementMessage。
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
        self.assertIsInstance(message, MeasurementMessage)
        assert isinstance(message, MeasurementMessage)
        self.assertEqual(message.device_datetime, datetime(2026, 4, 9, 11, 59, 59))
        self.assertEqual(message.bpm, 72)

    def test_dispatch_ack(self) -> None:
        # RTC 设置应答应被解析成 RtcSetAckMessage。
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
        self.assertIsInstance(message, RtcSetAckMessage)
        assert isinstance(message, RtcSetAckMessage)
        self.assertTrue(message.set_ok)
        self.assertEqual(message.device_datetime, datetime(2026, 4, 9, 12, 0, 1))

    def test_dispatch_measurement_with_nested_data(self) -> None:
        # 内外层混合格式的 measurement 负载也应被兼容解析。
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
        self.assertIsInstance(message, MeasurementMessage)
        assert isinstance(message, MeasurementMessage)
        self.assertEqual(message.device_datetime, datetime(2026, 4, 9, 12, 0, 2))

    def test_dispatch_parse_error(self) -> None:
        # parse_error 消息应被解析成 ParseErrorMessage。
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
        self.assertIsInstance(message, ParseErrorMessage)

    def test_unknown_message_raises(self) -> None:
        # 未知消息类型必须触发校验异常。
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
        with self.assertRaises(MessageValidationError):
            self.dispatcher.dispatch(payload, received_at=self.received_at)


if __name__ == "__main__":
    unittest.main()
