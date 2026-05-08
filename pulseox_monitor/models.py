# 定义 MQTT 上行消息的类型模型与字段校验逻辑。

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


class MessageValidationError(ValueError):
    pass


def parse_device_datetime(date_text: str, time_text: str) -> datetime:
    # 把设备给出的日期和时间文本解析成 datetime。
    return datetime.strptime(f"{date_text}{time_text}", "%Y%m%d%H%M%S")


def _require_field(payload: dict[str, Any], field_name: str) -> Any:
    # 读取必填字段。
    if field_name not in payload:
        raise MessageValidationError(f"缺少字段: {field_name}")
    return payload[field_name]


def _as_optional_str(value: Any, field_name: str) -> str | None:
    # 把可选字段转换成字符串。
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    raise MessageValidationError(f"字段 {field_name} 需要是字符串或数值")


def _as_str(value: Any, field_name: str) -> str:
    # 把必填字段转换成字符串。
    result = _as_optional_str(value, field_name)
    if result is None:
        raise MessageValidationError(f"字段 {field_name} 不能为空")
    return result


def _as_bool(value: Any, field_name: str) -> bool:
    # 把协议中的布尔字段统一转换成 bool。
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
    raise MessageValidationError(f"字段 {field_name} 需要是布尔值")


def _as_optional_int(value: Any, field_name: str) -> int | None:
    # 把可选整数字段转换成 int。
    if value is None:
        return None
    if isinstance(value, bool):
        raise MessageValidationError(f"字段 {field_name} 需要是整数")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError as exc:
            raise MessageValidationError(f"字段 {field_name} 需要是整数") from exc
    raise MessageValidationError(f"字段 {field_name} 需要是整数")


def _as_int(value: Any, field_name: str) -> int:
    # 把必填整数字段转换成 int。
    result = _as_optional_int(value, field_name)
    if result is None:
        raise MessageValidationError(f"字段 {field_name} 不能为空")
    return result


# 三类上行消息共有的头字段。
@dataclass(slots=True)
class BaseMessage:
    bridge: str
    source: str
    channel: str
    protocol: str
    frame: str
    message: str
    received_at: datetime

    @classmethod
    def _parse_common(
        cls,
        payload: dict[str, Any],
        expected_message: str,
        received_at: datetime | None = None,
    ) -> dict[str, Any]:
        # 解析所有消息共有的头字段。
        message = _as_str(_require_field(payload, "message"), "message")
        if message != expected_message:
            raise MessageValidationError(
                f"消息类型不匹配，期望 {expected_message}，实际 {message}"
            )

        return {
            "bridge": _as_str(_require_field(payload, "bridge"), "bridge"),
            "source": _as_str(_require_field(payload, "source"), "source"),
            "channel": _as_str(_require_field(payload, "channel"), "channel"),
            "protocol": _as_str(_require_field(payload, "protocol"), "protocol"),
            "frame": _as_str(_require_field(payload, "frame"), "frame"),
            "message": message,
            "received_at": received_at or datetime.now(),
        }


# measurement 消息的类型化结果。
@dataclass(slots=True)
class MeasurementMessage(BaseMessage):
    rtc_valid: bool
    date: str | None
    time: str | None
    device_datetime: datetime | None
    red: int
    ir: int
    baseline_ir: int
    finger: bool
    bpm_valid: bool
    bpm: int | None
    spo2_valid: bool
    spo2: int | None

    @classmethod
    def from_dict(
        cls,
        payload: dict[str, Any],
        received_at: datetime | None = None,
    ) -> "MeasurementMessage":
        # 从字典中解析 measurement 消息。
        common = cls._parse_common(
            payload,
            expected_message="measurement",
            received_at=received_at,
        )

        rtc_valid = _as_bool(_require_field(payload, "rtc_valid"), "rtc_valid")
        date_text = _as_optional_str(payload.get("date"), "date")
        time_text = _as_optional_str(payload.get("time"), "time")
        device_datetime = None

        # 只有在设备明确声明 RTC 有效时，才把 date 和 time 视为可信时间戳。
        if rtc_valid:
            if not date_text or not time_text:
                raise MessageValidationError("rtc_valid 为 true 时必须提供 date 和 time")
            try:
                device_datetime = parse_device_datetime(date_text, time_text)
            except ValueError as exc:
                raise MessageValidationError(
                    "date/time 格式错误，应为 YYYYMMDD 和 HHMMSS"
                ) from exc

        bpm_valid = _as_bool(_require_field(payload, "bpm_valid"), "bpm_valid")
        bpm = _as_optional_int(payload.get("bpm"), "bpm")
        if bpm_valid and bpm is None:
            raise MessageValidationError("bpm_valid 为 true 时必须提供 bpm")

        spo2_valid = _as_bool(_require_field(payload, "spo2_valid"), "spo2_valid")
        spo2 = _as_optional_int(payload.get("spo2"), "spo2")
        if spo2_valid and spo2 is None:
            raise MessageValidationError("spo2_valid 为 true 时必须提供 spo2")

        return cls(
            **common,
            rtc_valid=rtc_valid,
            date=date_text,
            time=time_text,
            device_datetime=device_datetime,
            red=_as_int(_require_field(payload, "red"), "red"),
            ir=_as_int(_require_field(payload, "ir"), "ir"),
            baseline_ir=_as_int(_require_field(payload, "baseline_ir"), "baseline_ir"),
            finger=_as_bool(_require_field(payload, "finger"), "finger"),
            bpm_valid=bpm_valid,
            bpm=bpm,
            spo2_valid=spo2_valid,
            spo2=spo2,
        )


# rtc_set_ack 消息的类型化结果。
@dataclass(slots=True)
class RtcSetAckMessage(BaseMessage):
    set_ok: bool
    rtc_valid: bool
    date: str | None
    time: str | None
    device_datetime: datetime | None
    reason: str | None

    @classmethod
    def from_dict(
        cls,
        payload: dict[str, Any],
        received_at: datetime | None = None,
    ) -> "RtcSetAckMessage":
        # 从字典中解析 RTC 设置应答消息。
        common = cls._parse_common(
            payload,
            expected_message="rtc_set_ack",
            received_at=received_at,
        )

        rtc_valid = _as_bool(_require_field(payload, "rtc_valid"), "rtc_valid")
        date_text = _as_optional_str(payload.get("date"), "date")
        time_text = _as_optional_str(payload.get("time"), "time")
        device_datetime = None

        # RTC 应答允许 rtc_valid 为假，因此只有字段齐全且 rtc_valid 为真时才解析时间。
        if rtc_valid and date_text and time_text:
            try:
                device_datetime = parse_device_datetime(date_text, time_text)
            except ValueError as exc:
                raise MessageValidationError("rtc_set_ack 的 date/time 格式错误") from exc

        return cls(
            **common,
            set_ok=_as_bool(_require_field(payload, "set_ok"), "set_ok"),
            rtc_valid=rtc_valid,
            date=date_text,
            time=time_text,
            device_datetime=device_datetime,
            reason=_as_optional_str(payload.get("reason"), "reason"),
        )


# parse_error 消息的类型化结果。
@dataclass(slots=True)
class ParseErrorMessage(BaseMessage):
    error: str
    raw: str

    @classmethod
    def from_dict(
        cls,
        payload: dict[str, Any],
        received_at: datetime | None = None,
    ) -> "ParseErrorMessage":
        # 从字典中解析设备侧解析失败消息。
        common = cls._parse_common(
            payload,
            expected_message="parse_error",
            received_at=received_at,
        )
        return cls(
            **common,
            error=_as_str(_require_field(payload, "error"), "error"),
            raw=_as_str(_require_field(payload, "raw"), "raw"),
        )


SupportedMessage = MeasurementMessage | RtcSetAckMessage | ParseErrorMessage
