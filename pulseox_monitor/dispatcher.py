# 按 message 字段把 MQTT 上行 JSON 分发成具体消息模型。

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable

from .models import (
    MessageValidationError,
    MeasurementMessage,
    ParseErrorMessage,
    RtcSetAckMessage,
    SupportedMessage,
)


# 负责把 MQTT 上行文本解析并分发成类型化消息。
class MessageDispatcher:
    def __init__(self) -> None:
        # 建立 message 到解析函数的映射表。
        self._factories: dict[str, Callable[[dict[str, Any], datetime | None], SupportedMessage]] = {
            "measurement": MeasurementMessage.from_dict,
            "rtc_set_ack": RtcSetAckMessage.from_dict,
            "parse_error": ParseErrorMessage.from_dict,
        }

    def dispatch(
        self,
        payload_text: str | bytes,
        received_at: datetime | None = None,
    ) -> SupportedMessage:
        # 按 message 字段分发 MQTT 上行负载。
        if isinstance(payload_text, bytes):
            try:
                payload_text = payload_text.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise MessageValidationError("MQTT 负载不是有效的 UTF-8 文本") from exc

        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError as exc:
            raise MessageValidationError("MQTT 上行负载不是合法 JSON") from exc

        if not isinstance(payload, dict):
            raise MessageValidationError("MQTT 上行 JSON 必须是对象")

        nested_data = payload.get("data")
        if isinstance(nested_data, dict):
            # 有些桥接程序会把公共字段放在外层，把业务字段放进 data 子对象。
            payload = {**payload, **nested_data}

        message_name = payload.get("message")
        if not isinstance(message_name, str):
            raise MessageValidationError("JSON 字段 message 缺失或类型错误")

        factory = self._factories.get(message_name)
        if factory is None:
            raise MessageValidationError(f"不支持的消息类型: {message_name}")

        # 协议层明确要求按 message 字段分发，这里不做额外猜测。
        return factory(payload, received_at)
