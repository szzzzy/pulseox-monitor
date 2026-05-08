# 封装 paho-mqtt，并把网络事件转成 Qt 信号。

from __future__ import annotations

from typing import Any

import paho.mqtt.client as mqtt
from PySide6.QtCore import QObject, Signal


# 管理 MQTT 连接、订阅、发布和重连状态。
class MQTTHandler(QObject):
    connection_state_changed = Signal(str)
    connected_changed = Signal(bool)
    upstream_payload_received = Signal(str)
    debug_message = Signal(str)

    def __init__(self, upstream_topic: str, downstream_topic: str, parent: QObject | None = None) -> None:
        # 保存主题配置并初始化 MQTT 客户端状态。
        super().__init__(parent)
        self.upstream_topic = upstream_topic
        self.downstream_topic = downstream_topic
        self._client: mqtt.Client | None = None
        self._connected = False
        self._intentional_disconnect = False

    @property
    def is_connected(self) -> bool:
        # 返回当前是否已经连上 Broker。
        return self._connected

    def connect_to_broker(
        self,
        host: str,
        port: int,
        username: str = "",
        password: str = "",
        client_id: str = "",
        keepalive: int = 60,
    ) -> None:
        # 连接到 Broker，并启动 paho 的后台网络循环。
        self.disconnect_from_broker(silent=True)
        self._intentional_disconnect = False

        try:
            self._client = self._build_client(client_id=client_id)
            if username:
                self._client.username_pw_set(username=username, password=password or None)

            # 开启指数退避式自动重连，避免网络闪断后必须手动重连。
            self._client.reconnect_delay_set(min_delay=1, max_delay=30)
            self._client.on_connect = self._on_connect
            self._client.on_disconnect = self._on_disconnect
            self._client.on_message = self._on_message
            self._client.on_subscribe = self._on_subscribe
            self._client.on_log = self._on_log
            if hasattr(self._client, "on_connect_fail"):
                self._client.on_connect_fail = self._on_connect_fail

            self.connection_state_changed.emit(f"正在连接 {host}:{port} ...")
            self.connected_changed.emit(False)
            self._client.connect_async(host=host, port=port, keepalive=keepalive)
            self._client.loop_start()
        except Exception as exc:  # pragma: no cover - 运行期网络异常很难稳定复现。
            self._connected = False
            self.connection_state_changed.emit(f"连接失败: {exc}")
            self.connected_changed.emit(False)
            self.debug_message.emit(f"[MQTT] 连接失败: {exc}")

    def disconnect_from_broker(self, silent: bool = False) -> None:
        # 主动断开 Broker 连接，并停止后台网络循环。
        if self._client is None:
            if not silent:
                self._connected = False
                self.connection_state_changed.emit("已断开")
                self.connected_changed.emit(False)
            return

        self._intentional_disconnect = True
        client = self._client
        self._client = None
        try:
            client.disconnect()
        except Exception as exc:  # pragma: no cover - 这里只做日志兜底。
            self.debug_message.emit(f"[MQTT] 主动断开时出现异常: {exc}")
        finally:
            client.loop_stop()
            self._connected = False
            if not silent:
                self.connection_state_changed.emit("已断开")
                self.connected_changed.emit(False)

    def publish_command(self, command_text: str) -> bool:
        # 向下行主题发布纯文本命令。
        if self._client is None or not self._connected:
            self.debug_message.emit("[MQTT] 当前未连接，命令未发送")
            return False

        result = self._client.publish(self.downstream_topic, payload=command_text, qos=1)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            self.debug_message.emit(f"[MQTT] 发布失败，返回码: {result.rc}")
            return False

        self.debug_message.emit(
            f"[MQTT] 已发布下行命令到 {self.downstream_topic}: {command_text}"
        )
        return True

    def _build_client(self, client_id: str) -> mqtt.Client:
        # 根据 paho 版本差异创建兼容的 MQTT Client。
        callback_api_version = getattr(mqtt, "CallbackAPIVersion", None)
        if callback_api_version is not None:
            return mqtt.Client(
                callback_api_version=callback_api_version.VERSION2,
                client_id=client_id,
            )
        return mqtt.Client(client_id=client_id)

    def _reason_code_to_int(self, reason_code: Any) -> int:
        # 把不同版本 paho 的原因码对象统一转换成整数。
        value = getattr(reason_code, "value", reason_code)
        try:
            return int(value)
        except (TypeError, ValueError):
            return -1

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any = None,
    ) -> None:
        # 处理连接建立后的订阅和状态通知。
        code = self._reason_code_to_int(reason_code)
        if code == 0:
            self._connected = True
            self.connection_state_changed.emit(f"已连接，正在订阅 {self.upstream_topic}")
            self.connected_changed.emit(True)
            client.subscribe(self.upstream_topic, qos=1)
            self.debug_message.emit(f"[MQTT] 连接成功，订阅上行主题 {self.upstream_topic}")
        else:
            self._connected = False
            self.connection_state_changed.emit(f"连接被拒绝，返回码: {reason_code}")
            self.connected_changed.emit(False)
            self.debug_message.emit(f"[MQTT] Broker 拒绝连接: {reason_code}")

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags_or_reason: Any,
        reason_code: Any = None,
        properties: Any = None,
    ) -> None:
        # 处理断开事件，并区分主动断开与异常中断。
        self._connected = False
        self.connected_changed.emit(False)

        actual_reason = reason_code if reason_code is not None else flags_or_reason
        code = self._reason_code_to_int(actual_reason)
        if self._intentional_disconnect:
            self.connection_state_changed.emit("已断开")
            self.debug_message.emit("[MQTT] 已主动断开连接")
            return

        self.connection_state_changed.emit(f"连接中断，自动重连中 (code={code})")
        self.debug_message.emit(f"[MQTT] 连接中断，paho 将自动重连: {actual_reason}")

    def _on_message(self, client: mqtt.Client, userdata: Any, message: mqtt.MQTTMessage) -> None:
        # 把目标上行主题收到的文本透传给 Qt 层。
        if message.topic != self.upstream_topic:
            return
        payload_text = message.payload.decode("utf-8", errors="replace")
        self.upstream_payload_received.emit(payload_text)

    def _on_subscribe(
        self,
        client: mqtt.Client,
        userdata: Any,
        mid: int,
        granted_qos: Any,
        properties: Any = None,
    ) -> None:
        # 记录订阅成功日志，便于定位 Broker 侧问题。
        self.debug_message.emit(f"[MQTT] 订阅确认 mid={mid}, qos={granted_qos}")

    def _on_log(self, client: mqtt.Client, userdata: Any, level: int, message: str) -> None:
        # 只把错误级别日志转发到界面，避免调试面板过于嘈杂。
        if level >= mqtt.MQTT_LOG_ERR:
            self.debug_message.emit(f"[MQTT] {message}")

    def _on_connect_fail(self, client: mqtt.Client, userdata: Any) -> None:
        # 处理异步连接失败回调。
        self._connected = False
        self.connection_state_changed.emit("连接失败，等待重试")
        self.connected_changed.emit(False)
        self.debug_message.emit("[MQTT] connect_async 失败，等待自动重试")
