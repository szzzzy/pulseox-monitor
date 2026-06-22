"""验证 FlexibleMessage 数据模型和类型转换函数。"""

from __future__ import annotations

import unittest
from datetime import datetime

from pulseox_monitor.models import (
    FlexibleMessage,
    _safe_int,
    _safe_float,
    _safe_str,
    _safe_bool,
    _safe_optional_bool,
    _clean_value,
    parse_device_datetime,
)


class SafeIntTests(unittest.TestCase):
    def test_none_returns_none(self) -> None:
        self.assertIsNone(_safe_int(None))

    def test_int_returns_int(self) -> None:
        self.assertEqual(_safe_int(42), 42)

    def test_float_whole_returns_int(self) -> None:
        self.assertEqual(_safe_int(3.0), 3)

    def test_float_nan_returns_none(self) -> None:
        self.assertIsNone(_safe_int(float("nan")))

    def test_float_fraction_returns_none(self) -> None:
        self.assertIsNone(_safe_int(3.14))

    def test_bool_returns_int(self) -> None:
        self.assertEqual(_safe_int(True), 1)
        self.assertEqual(_safe_int(False), 0)

    def test_string_int_returns_int(self) -> None:
        self.assertEqual(_safe_int("42"), 42)

    def test_string_non_numeric_returns_none(self) -> None:
        self.assertIsNone(_safe_int("hello"))

    def test_sentinels_return_none(self) -> None:
        for sentinel in ("", "--", "N/A", "n/a", "NaN", "nan", "null"):
            self.assertIsNone(_safe_int(sentinel))


class SafeFloatTests(unittest.TestCase):
    def test_none_returns_none(self) -> None:
        self.assertIsNone(_safe_float(None))

    def test_int_returns_float(self) -> None:
        self.assertEqual(_safe_float(42), 42.0)

    def test_float_returns_float(self) -> None:
        self.assertEqual(_safe_float(3.14), 3.14)

    def test_float_nan_returns_none(self) -> None:
        self.assertIsNone(_safe_float(float("nan")))

    def test_bool_returns_none(self) -> None:
        self.assertIsNone(_safe_float(True))
        self.assertIsNone(_safe_float(False))

    def test_string_float_returns_float(self) -> None:
        self.assertEqual(_safe_float("3.14"), 3.14)

    def test_string_non_numeric_returns_none(self) -> None:
        self.assertIsNone(_safe_float("hello"))


class SafeStrTests(unittest.TestCase):
    def test_none_returns_none(self) -> None:
        self.assertIsNone(_safe_str(None))

    def test_str_returns_str(self) -> None:
        self.assertEqual(_safe_str("hello"), "hello")

    def test_int_returns_str(self) -> None:
        self.assertEqual(_safe_str(42), "42")

    def test_float_returns_str(self) -> None:
        self.assertEqual(_safe_str(3.14), "3.14")

    def test_bool_returns_str(self) -> None:
        self.assertEqual(_safe_str(True), "True")

    def test_list_returns_none(self) -> None:
        self.assertIsNone(_safe_str([1, 2, 3]))


class CleanValueTests(unittest.TestCase):
    def test_sentinels_become_none(self) -> None:
        for sentinel in (
            "", "--", "N/A", "n/a", "NA", "na", "NaN", "nan",
            "null", "NULL", "None",
        ):
            self.assertIsNone(_clean_value(sentinel))

    def test_whitespace_sentinels_become_none(self) -> None:
        self.assertIsNone(_clean_value("  --  "))
        self.assertIsNone(_clean_value("  n/a  "))

    def test_normal_string_unchanged(self) -> None:
        self.assertEqual(_clean_value("  hello  "), "hello")

    def test_number_unchanged(self) -> None:
        self.assertEqual(_clean_value(42), 42)
        self.assertEqual(_clean_value(3.14), 3.14)


class ParseDeviceDateTimeTests(unittest.TestCase):
    def test_valid_date_time(self) -> None:
        dt = parse_device_datetime("20260409", "120000")
        self.assertEqual(dt, datetime(2026, 4, 9, 12, 0, 0))

    def test_invalid_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_device_datetime("abcd", "xyz")


class GetIntDefaultTests(unittest.TestCase):
    """验证 get_int 的 default 参数确实生效。"""

    def setUp(self) -> None:
        self.payload = {
            "message": "measurement",
            "rtc_valid": True,
            "date": "20260409",
            "time": "120000",
            "bpm": 72,
            "non_numeric": "hello",
            "nested": {"value": 100},
        }
        self.msg = FlexibleMessage.from_dict(self.payload)

    def test_default_when_path_missing(self) -> None:
        self.assertEqual(self.msg.get_int("nonexistent", default=5), 5)

    def test_default_when_value_not_convertible(self) -> None:
        self.assertEqual(self.msg.get_int("non_numeric", default=5), 5)

    def test_default_zero_when_path_missing(self) -> None:
        self.assertEqual(self.msg.get_int("nonexistent", default=0), 0)

    def test_default_none_when_path_missing(self) -> None:
        self.assertIsNone(self.msg.get_int("nonexistent", default=None))

    def test_value_overrides_default(self) -> None:
        self.assertEqual(self.msg.get_int("bpm", default=5), 72)

    def test_nested_path_with_default(self) -> None:
        self.assertEqual(self.msg.get_int("nested", "value", default=5), 100)
        self.assertEqual(self.msg.get_int("nested", "nonexistent", default=5), 5)


class GetFloatDefaultTests(unittest.TestCase):
    """验证 get_float 的 default 参数确实生效。"""

    def setUp(self) -> None:
        self.payload = {
            "message": "measurement",
            "pi": 3.14,
            "non_numeric": "hello",
        }
        self.msg = FlexibleMessage.from_dict(self.payload)

    def test_default_when_path_missing(self) -> None:
        self.assertEqual(self.msg.get_float("nonexistent", default=1.5), 1.5)

    def test_default_when_value_not_convertible(self) -> None:
        self.assertEqual(self.msg.get_float("non_numeric", default=1.5), 1.5)

    def test_value_overrides_default(self) -> None:
        self.assertEqual(self.msg.get_float("pi", default=1.5), 3.14)


class GetStrDefaultTests(unittest.TestCase):
    """验证 get_str 的 default 参数确实生效。"""

    def setUp(self) -> None:
        self.payload = {
            "message": "measurement",
            "label": "test",
        }
        self.msg = FlexibleMessage.from_dict(self.payload)

    def test_default_when_path_missing(self) -> None:
        self.assertEqual(
            self.msg.get_str("nonexistent", default="fallback"), "fallback"
        )

    def test_default_none_when_path_missing(self) -> None:
        self.assertIsNone(self.msg.get_str("nonexistent", default=None))

    def test_value_overrides_default(self) -> None:
        self.assertEqual(self.msg.get_str("label", default="fallback"), "test")


class FlexibleMessagePropertyTests(unittest.TestCase):
    """验证核心属性的回退行为。"""

    def setUp(self) -> None:
        self.payload = {
            "message": "measurement",
            "bridge": "gw-01",
            "rtc_valid": True,
            "date": "20260409",
            "time": "120000",
            "red": 1200,
            "ir": 2400,
            "finger": True,
            "bpm_valid": True, "bpm": 72,
            "spo2_valid": True, "spo2": 98,
        }
        self.msg = FlexibleMessage.from_dict(self.payload)

    def test_missing_numeric_is_none(self) -> None:
        self.assertIsNone(self.msg.rr)
        self.assertIsNone(self.msg.ibi)
        self.assertIsNone(self.msg.sdnn)

    def test_missing_bool_is_false(self) -> None:
        self.assertFalse(self.msg.rr_valid)
        self.assertFalse(self.msg.hrv_valid)

    def test_timestamp_valid(self) -> None:
        self.assertTrue(self.msg.timestamp_valid())
        self.assertEqual(self.msg.plot_timestamp(), datetime(2026, 4, 9, 12, 0, 0))

    def test_rtc_invalid_falls_back(self) -> None:
        received = datetime(2026, 4, 9, 10, 0, 0)
        payload = {"message": "measurement", "rtc_valid": False, "red": 100, "ir": 200}
        msg = FlexibleMessage.from_dict(payload, received_at=received)
        self.assertFalse(msg.timestamp_valid())
        self.assertEqual(msg.plot_timestamp(), received)
        self.assertIsNone(msg.device_datetime)

    def test_finger_off(self) -> None:
        payload = {"message": "measurement", "rtc_valid": True, "date": "20260409", "time": "120000", "finger": False}
        msg = FlexibleMessage.from_dict(payload)
        self.assertFalse(msg.finger)


if __name__ == "__main__":
    unittest.main()
