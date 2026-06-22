# =============================================================================
# USB Handler 测试 —— 覆盖行过滤、命令常量、基本行为。
#
# 注意：
#   - _is_data_line 函数不依赖 PySide6，可以在任何环境下运行。
#   - USBHandler 类依赖 PySide6 QObject，需要 PySide6 环境。
#   - Transport 切换逻辑依赖 MainWindow，需要 PySide6 环境。
#   - 当前测试环境无 PySide6，因此仅测试纯函数和常量。
#   完整 GUI 集成测试需要在安装了 PySide6 的环境中进行。
# =============================================================================

from __future__ import annotations

from datetime import datetime
import unittest

# 检测 PySide6 是否可用
try:
    from PySide6.QtCore import QObject  # noqa: F401
    _HAS_PYSIDE6 = True
except ImportError:
    _HAS_PYSIDE6 = False


# =============================================================================
# 测试 1: _is_data_line —— 数据行过滤
# =============================================================================

class DataLineFilterTests(unittest.TestCase):
    """测试 _is_data_line 函数的过滤逻辑。

    核心规则：
      - JSON 对象/数组 → 保留
      - STM32 CSV (M,...) → 保留
      - ESP_LOG 行 (I/W/E/D/V + 空格) → 丢弃
      - 空白行 → 丢弃
      - 无法识别的行 → 尝试 JSON 解析，成功则保留
    """

    @classmethod
    def setUpClass(cls) -> None:
        from pulseox_monitor.usb_protocol import is_data_line as _filter
        cls._filter = staticmethod(_filter)

    def assertData(self, line: str) -> None:
        self.assertTrue(self._filter(line))  # type: ignore[arg-type]

    def assertNoise(self, line: str) -> None:
        self.assertFalse(self._filter(line))  # type: ignore[arg-type]

    # ── JSON 数据行 ──

    def test_json_object_is_data(self) -> None:
        """标准的 JSON 对象应被识别为数据行。"""
        self.assertData(
            '{"message":"measurement","bpm":72,"spo2":98}'
        )

    def test_json_array_is_data(self) -> None:
        """JSON 数组应被识别为数据行。"""
        self.assertData('[{"bpm":72},{"bpm":73}]')

    def test_json_with_whitespace_is_data(self) -> None:
        """带前导空格的 JSON 应被识别（strip 由调用方处理）。
        注意：_is_data_line 假设输入已 strip。
        """
        self.assertData('{"bpm": 72}')
        self.assertData(
            '{"message":"measurement","bpm":72,"spo2":98,"rr":16}'
        )

    def test_json_bool_number_string(self) -> None:
        """有效的 JSON 标量值也应被保留（尝试解析成功）。"""
        self.assertData("true")
        self.assertData("false")
        self.assertData("null")
        self.assertData("42")
        self.assertData("3.14")
        self.assertData('"hello"')

    # ── STM32 CSV 数据行 ──

    def test_stm32_csv_is_data(self) -> None:
        """以 M, 开头的 STM32 raw CSV 行应被识别为数据行。"""
        self.assertData("M,72,1,98,1,16,1")
        self.assertData(
            "M," + ",".join(["0"] * 109)
        )

    # ── ESP_LOG 噪声 ──

    def test_esp_info_log_is_noise(self) -> None:
        """ESP-IDF I 级别日志应被过滤。"""
        self.assertNoise("I (12345) wifi: connected")

    def test_esp_warn_log_is_noise(self) -> None:
        """ESP-IDF W 级别日志应被过滤。"""
        self.assertNoise("W (12346) main: heap min free: 123456")

    def test_esp_error_log_is_noise(self) -> None:
        """ESP-IDF E 级别日志应被过滤。"""
        self.assertNoise("E (12347) sensor: read failed")

    def test_esp_debug_log_is_noise(self) -> None:
        """ESP-IDF D 级别日志应被过滤。"""
        self.assertNoise("D (12348) i2c: transfer done")

    def test_esp_verbose_log_is_noise(self) -> None:
        """ESP-IDF V 级别日志应被过滤。"""
        self.assertNoise("V (12349) adc: sample=2048")

    # ── 空白行 ──

    def test_empty_string_is_noise(self) -> None:
        """空字符串应被过滤。"""
        self.assertNoise("")

    def test_whitespace_only_is_noise(self) -> None:
        """纯空格行应被过滤（调用方应 strip 后再传入）。"""
        self.assertNoise("   ")

    # ── 边界情况 ──

    def test_random_text_is_noise(self) -> None:
        """无法识别为 JSON 的随机文本应被过滤。"""
        self.assertNoise("Some random text")
        self.assertNoise("hello world")
        self.assertNoise("GET / HTTP/1.1")

    def test_single_letter_not_log_is_checked(self) -> None:
        """单个大写字母但不是日志格式的，尝试 JSON 解析。
        "X" 不是有效 JSON → 应丢弃。
        """
        self.assertNoise("X")

    def test_letter_without_space_not_log(self) -> None:
        """单个字母后无空格 → 不是日志格式，尝试 JSON。
        "ABC" 不是有效 JSON → 应丢弃。
        """
        self.assertNoise("ABC")

    def test_partial_json_passes_filter_fails_in_dispatcher(self) -> None:
        """不完整的 JSON 以 '{' 开头，通过快速过滤但会在 dispatcher 中解析失败。

        is_data_line 的快速过滤只检查首字符 '{'/'['/'M,'，
        不做完整 JSON 验证（性能原因）。
        格式错误的 JSON 会在 MessageDispatcher 中触发 MessageValidationError。
        """
        self.assertData('{"bpm": 72')  # 缺少 }，但以 { 开头 → 通过过滤

    def test_malformed_json_passes_filter_fails_in_dispatcher(self) -> None:
        """格式错误的 JSON 以 '{' 开头，通过快速过滤但会在 dispatcher 中解析失败。

        同样，首字符规则优先于有效性检查。
        """
        self.assertData("{bpm: 72}")  # 键没有引号，但以 { 开头 → 通过过滤

    def test_utf8_json_is_data(self) -> None:
        """包含 UTF-8 字符的 JSON 应被识别。"""
        self.assertData(
            '{"message":"测量","bpm":72}'
        )

    def test_nested_json_is_data(self) -> None:
        """嵌套 JSON 对象应被识别。"""
        self.assertData(
            '{"modules":{"bpm":{"value":72}},"timestamp":1234567890}'
        )


# =============================================================================
# 测试 2: USB 命令常量
# =============================================================================

class USBCommandConstantsTests(unittest.TestCase):
    """测试 USB 生命周期命令常量的值。"""

    def test_lifecycle_commands_defined(self) -> None:
        """GUI_USB_START / PING / STOP 常量应有预期值。"""
        from pulseox_monitor.usb_protocol import (
            CMD_USB_START,
            CMD_USB_STOP,
            CMD_USB_PING,
        )
        self.assertEqual(CMD_USB_START, "GUI_USB_START")
        self.assertEqual(CMD_USB_STOP, "GUI_USB_STOP")
        self.assertEqual(CMD_USB_PING, "GUI_USB_PING")

    def test_default_baudrate(self) -> None:
        """默认波特率应为 115200。"""
        from pulseox_monitor.usb_protocol import DEFAULT_BAUDRATE, DEFAULT_PING_INTERVAL_MS
        self.assertEqual(DEFAULT_BAUDRATE, 115200)
        self.assertEqual(DEFAULT_PING_INTERVAL_MS, 5000)


# =============================================================================
# 测试 3: _is_data_line 与 MessageDispatcher 的串联行为
# =============================================================================

class USBLineToDispatcherIntegrationTests(unittest.TestCase):
    """测试 USB 数据行经过滤后能成功被 MessageDispatcher 解析。

    这些测试验证 USB → Dispatcher → FlexibleMessage 的完整串联路径。
    """

    def test_filtered_json_line_dispatches(self) -> None:
        """过滤通过的 JSON 行应被 dispatcher 成功解析。"""
        from pulseox_monitor.usb_protocol import is_data_line as _is_data_line
        from pulseox_monitor.dispatcher import MessageDispatcher

        dispatcher = MessageDispatcher()
        line = '{"message":"measurement","rtc_valid":true,"date":"20260409","time":"120000","red":100,"ir":200,"finger":true,"bpm_valid":true,"bpm":72,"spo2_valid":true,"spo2":98}'

        # 1. 过滤通过
        self.assertTrue(_is_data_line(line))

        # 2. 解析成功
        msg = dispatcher.dispatch(line)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.message_type, "measurement")
        self.assertEqual(msg.bpm, 72)

    def test_filtered_csv_line_dispatches(self) -> None:
        """过滤通过的 CSV 行应被 dispatcher 成功解析。"""
        from pulseox_monitor.usb_protocol import is_data_line as _is_data_line
        from pulseox_monitor.dispatcher import MessageDispatcher

        dispatcher = MessageDispatcher()
        # 构造一个简单的 CSV 帧（110 列）
        cols = ["0"] * 110
        cols[0] = "M"
        cols[8] = "1"     # bpm_valid
        cols[9] = "75"    # bpm
        cols[10] = "1"    # spo2_valid
        cols[11] = "99"   # spo2
        line = ",".join(cols)

        # 1. 过滤通过
        self.assertTrue(_is_data_line(line))

        # 2. 解析成功
        msg = dispatcher.dispatch(line)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.message_type, "measurement")
        self.assertEqual(msg.bpm, 75)
        self.assertEqual(msg.spo2, 99)

    def test_noise_line_not_dispatched(self) -> None:
        """ESP_LOG 噪声行应被过滤，且即使强制递给 dispatcher 也不会解析。"""
        from pulseox_monitor.usb_protocol import is_data_line as _is_data_line
        from pulseox_monitor.dispatcher import MessageDispatcher

        # ESP_LOG 应被过滤
        self.assertFalse(_is_data_line("I (12345) wifi: connected"))
        self.assertFalse(_is_data_line("W (12346) main: heap min free: 123456"))
        self.assertFalse(_is_data_line("E (12347) sensor: read failed"))

    def test_usb_path_dispatches_all_supported_message_types(self) -> None:
        """USB 数据路径应能接收 measurement/status/ack/parse_error。"""
        from pulseox_monitor.usb_protocol import is_data_line as _is_data_line
        from pulseox_monitor.dispatcher import MessageDispatcher

        dispatcher = MessageDispatcher()
        lines = [
            '{"message":"measurement","schema_version":3,"field_count":110,"parse_ok":true,"parse_warnings":[],"bpm":72}',
            '{"message":"esp_status","online":true,"usb":{"active":true},"mqtt":{"connected":true}}',
            '{"message":"rtc_set_ack","set_ok":true,"rtc_valid":true,"date":"20260409","time":"120001"}',
            '{"message":"parse_error","error":"bad columns","raw_line":"M,old"}',
        ]

        messages = []
        for line in lines:
            self.assertTrue(_is_data_line(line))
            messages.append(dispatcher.dispatch(line))

        self.assertEqual(
            [msg.message_type for msg in messages],
            ["measurement", "esp_status", "rtc_set_ack", "parse_error"],
        )


@unittest.skipUnless(_HAS_PYSIDE6, "需要 PySide6 环境")
class MQTTReceivePathTests(unittest.TestCase):
    """验证 MQTT 数据主题和状态主题都会上抛给统一 dispatcher 入口。"""

    def test_mqtt_topics_emit_supported_message_types(self) -> None:
        from pulseox_monitor.mqtt_handler import MQTTHandler

        handler = MQTTHandler(
            upstream_topic="pulseox/data",
            status_topic="pulseox/status",
            downstream_topic="pulseox/cmd",
        )
        received: list[str] = []
        handler.upstream_payload_received.connect(received.append)

        class Msg:
            def __init__(self, topic: str, payload: str) -> None:
                self.topic = topic
                self.payload = payload.encode("utf-8")

        payloads = [
            ("pulseox/data", '{"message":"measurement","schema_version":3,"field_count":110}'),
            ("pulseox/status", '{"message":"esp_status","online":true}'),
            ("pulseox/data", '{"message":"rtc_set_ack","set_ok":true}'),
            ("pulseox/data", '{"message":"parse_error","error":"bad columns"}'),
        ]
        for topic, payload in payloads:
            handler._on_message(None, None, Msg(topic, payload))

        handler._on_message(None, None, Msg("pulseox/ignored", '{"message":"ignored"}'))

        self.assertEqual([payload for _topic, payload in payloads], received)


@unittest.skipUnless(_HAS_PYSIDE6, "需要 PySide6 环境")
class CommandSendPathTests(unittest.TestCase):
    """验证 SETTIME 格式和 MQTT/USB 命令发送路径。"""

    def test_build_settime_command_uses_current_protocol_format(self) -> None:
        from pulseox_monitor.main_window import build_settime_command

        command = build_settime_command(datetime(2026, 4, 14, 12, 34, 56))
        self.assertEqual(command, "SETTIME 2026-04-14 12:34:56")

    def test_mqtt_publish_command_sends_plain_settime(self) -> None:
        import paho.mqtt.client as mqtt
        from pulseox_monitor.mqtt_handler import MQTTHandler

        class Result:
            rc = mqtt.MQTT_ERR_SUCCESS

        class FakeClient:
            def __init__(self) -> None:
                self.calls = []

            def publish(self, topic, payload, qos):
                self.calls.append((topic, payload, qos))
                return Result()

        handler = MQTTHandler("pulseox/data", "pulseox/cmd")
        fake = FakeClient()
        handler._client = fake
        handler._connected = True

        sent = handler.publish_command("SETTIME 2026-04-14 12:34:56")

        self.assertTrue(sent)
        self.assertEqual(
            fake.calls,
            [("pulseox/cmd", "SETTIME 2026-04-14 12:34:56", 0)],
        )

    def test_mqtt_subscriptions_use_qos0(self) -> None:
        from pulseox_monitor.mqtt_handler import MQTTHandler

        handler = MQTTHandler(
            upstream_topic="pulseox/data",
            status_topic="pulseox/status",
            downstream_topic="pulseox/cmd",
        )

        self.assertEqual(
            handler._subscription_topics(),
            [("pulseox/data", 0), ("pulseox/status", 0)],
        )

    def test_mqtt_connect_default_keepalive_is_30(self) -> None:
        from pulseox_monitor.mqtt_handler import MQTTHandler

        class FakeClient:
            def __init__(self) -> None:
                self.connect_calls = []
                self.loop_started = False

            def reconnect_delay_set(self, min_delay, max_delay) -> None:
                pass

            def connect_async(self, host, port, keepalive) -> None:
                self.connect_calls.append((host, port, keepalive))

            def loop_start(self) -> None:
                self.loop_started = True

        handler = MQTTHandler("pulseox/data", "pulseox/cmd")
        fake = FakeClient()
        handler._build_client = lambda client_id: fake  # type: ignore[method-assign]

        handler.connect_to_broker("172.20.10.4", 1883)

        self.assertEqual(fake.connect_calls, [("172.20.10.4", 1883, 30)])
        self.assertTrue(fake.loop_started)

    def test_usb_send_command_appends_newline(self) -> None:
        from pulseox_monitor.usb_handler import USBHandler

        class FakeSerial:
            def __init__(self) -> None:
                self.writes = []
                self.flushed = False

            def write(self, raw: bytes) -> None:
                self.writes.append(raw)

            def flush(self) -> None:
                self.flushed = True

        handler = USBHandler()
        fake = FakeSerial()
        handler._connected = True
        handler._ser = fake

        sent = handler.send_command("SETTIME 2026-04-14 12:34:56")

        self.assertTrue(sent)
        self.assertEqual(fake.writes, [b"SETTIME 2026-04-14 12:34:56\n"])
        self.assertTrue(fake.flushed)


# =============================================================================
# 测试 4: Transport 模式常量
# =============================================================================

@unittest.skipUnless(_HAS_PYSIDE6, "需要 PySide6 环境")
class TransportModeTests(unittest.TestCase):
    """测试 Transport 模式常量和切换逻辑的纯函数部分。"""

    def test_transport_constants(self) -> None:
        """TRANSPORT_MQTT 和 TRANSPORT_USB 应有预期值。"""
        from pulseox_monitor.main_window import (
            TRANSPORT_MQTT,
            TRANSPORT_USB,
            TRANSPORT_MODES,
        )
        self.assertEqual(TRANSPORT_MQTT, "MQTT")
        self.assertEqual(TRANSPORT_USB, "USB")
        self.assertEqual(len(TRANSPORT_MODES), 2)
        self.assertIn(TRANSPORT_MQTT, TRANSPORT_MODES)
        self.assertIn(TRANSPORT_USB, TRANSPORT_MODES)


# =============================================================================
# 测试 5: USBHandler 基本行为（不依赖串口硬件）
#
# 注意：这些测试需要 PySide6 环境。
# 运行: python -m pytest tests/test_usb_handler.py -v -k "USBHandler"
# 或:   python -m unittest tests.test_usb_handler.USBHandlerBasicTests
# =============================================================================

@unittest.skipUnless(_HAS_PYSIDE6, "需要 PySide6 环境")
class USBHandlerBasicTests(unittest.TestCase):
    """测试 USBHandler 的基本行为（不涉及真实串口硬件）。"""

    def setUp(self) -> None:
        from pulseox_monitor.usb_handler import USBHandler
        self.handler = USBHandler()

    def test_initial_state_disconnected(self) -> None:
        """新建的 USBHandler 应处于未连接状态。"""
        self.assertFalse(self.handler.is_connected())
        self.assertEqual(self.handler.port_name(), "")

    def test_send_command_when_disconnected_returns_false(self) -> None:
        """未连接时 send_command 应返回 False。"""
        self.assertFalse(self.handler.send_command("test"))
        self.assertFalse(self.handler.send_command("GUI_USB_START"))

    def test_disconnect_when_not_connected_is_noop(self) -> None:
        """未连接时调用 disconnect 应不抛异常。"""
        # 不应抛出任何异常
        self.handler.disconnect()
        self.assertFalse(self.handler.is_connected())

    def test_available_ports_returns_list(self) -> None:
        """available_ports 静态方法应返回列表。"""
        from pulseox_monitor.usb_handler import USBHandler
        ports = USBHandler.available_ports()
        self.assertIsInstance(ports, list)

    def test_signals_are_connectable(self) -> None:
        """信号的 connect 方法应可调用（不抛出异常）。"""
        received = []

        def on_line(line):
            received.append(line)

        self.handler.line_received.connect(on_line)
        self.handler.line_received.emit('{"test":1}')
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0], '{"test":1}')

    def test_multiple_lines_emitted_and_collected(self) -> None:
        """连续发射多行数据应全部被收集。"""
        received = []

        def on_line(line):
            received.append(line)

        self.handler.line_received.connect(on_line)
        for i in range(10):
            self.handler.line_received.emit(f'{{"seq":{i}}}')

        self.assertEqual(len(received), 10)
        self.assertEqual(received[0], '{"seq":0}')
        self.assertEqual(received[9], '{"seq":9}')


if __name__ == "__main__":
    unittest.main()


# =============================================================================
# 测试 6: PlotGroup 独立 item 探针（offscreen）
# =============================================================================

@unittest.skipUnless(_HAS_PYSIDE6, "需要 PySide6 环境")
class PlotGroupItemIndependenceTests(unittest.TestCase):
    """验证 marker / region / crosshair 在每个 subplot 中都是独立 item。"""

    def setUp(self) -> None:
        from PySide6.QtWidgets import QApplication
        import sys
        # 确保 QApplication 存在（offscreen）
        self._app = QApplication.instance() or QApplication(sys.argv)

        from pulseox_monitor.data_manager import DataManager
        from pulseox_monitor.plot_manager import PlotGroup, SubplotDef, CurveDef
        self._dm = DataManager(max_history=100)

        subplots = [
            SubplotDef(
                title="Plot A", y_label="a",
                curves=[CurveDef("bpm", "BPM", "#f00")],
            ),
            SubplotDef(
                title="Plot B", y_label="b",
                curves=[CurveDef("spo2", "SpO2", "#0f0")],
            ),
            SubplotDef(
                title="Plot C", y_label="c",
                curves=[CurveDef("rr", "RR", "#00f")],
            ),
        ]
        self._pg = PlotGroup(self._dm, subplots)

    def test_set_time_markers_creates_independent_items(self) -> None:
        """每个 subplot 获得独立的 InfiniteLine marker item。"""
        markers = [
            {"position": 100.0, "color": "#0f0", "label": "Test"},
        ]
        self._pg.set_time_markers(markers)
        # 每个 marker 定义对应 len(subplots) 个 item
        self.assertEqual(len(self._pg._time_markers), 1)
        # 3 个 subplot，所以内层有 3 个 InfiniteLine
        self.assertEqual(len(self._pg._time_markers[0]), 3)
        # 验证它们不是同一个对象
        a, b, c = self._pg._time_markers[0]
        self.assertIsNot(a, b)
        self.assertIsNot(b, c)

    def test_set_unfilled_region_creates_independent_items(self) -> None:
        """每个 subplot 获得独立的 LinearRegionItem。"""
        self._pg.set_unfilled_region(100.0, 200.0)
        self.assertEqual(len(self._pg._unfilled_regions), 3)
        a, b, c = self._pg._unfilled_regions
        self.assertIsNot(a, b)
        self.assertIsNot(b, c)

    def test_crosshair_items_are_independent(self) -> None:
        """每个 subplot 的 crosshair vline 是独立 item。"""
        self.assertEqual(len(self._pg._crosshair_vlines), 3)
        a, b, c = self._pg._crosshair_vlines
        self.assertIsNot(a, b)
        self.assertIsNot(b, c)
        # tooltip labels 也是独立的
        self.assertEqual(len(self._pg._tooltip_labels), 3)
        ta, tb, tc = self._pg._tooltip_labels
        self.assertIsNot(ta, tb)
        self.assertIsNot(tb, tc)

    def test_clear_time_markers_removes_all(self) -> None:
        """清除 marker 后列表为空。"""
        self._pg.set_time_markers([
            {"position": 100.0, "color": "#0f0", "label": "M1"},
            {"position": 200.0, "color": "#f00", "label": "M2"},
        ])
        self.assertEqual(len(self._pg._time_markers), 2)
        self._pg.clear_time_markers()
        self.assertEqual(len(self._pg._time_markers), 0)
        self.assertEqual(len(self._pg._unfilled_regions), 0)

    def test_refresh_expands_y_axis_to_visible_data(self) -> None:
        """Fixed default ranges expand when visible data falls outside them."""
        from pulseox_monitor.models import FlexibleMessage
        from pulseox_monitor.plot_manager import CurveDef, PlotGroup, SubplotDef

        plot_group = PlotGroup(
            self._dm,
            [
                SubplotDef(
                    title="Quality",
                    y_label="SQ",
                    curves=[CurveDef("signal_quality", "SQ", "#0f0")],
                    y_range=(0, 100),
                )
            ],
        )
        self._dm.add_message(
            FlexibleMessage.from_dict(
                {
                    "message": "measurement",
                    "signal_quality": 250,
                    "field_count": 110,
                    "parse_ok": True,
                    "parse_warnings": [],
                    "extra_fields": [],
                },
                received_at=datetime.now(),
            )
        )

        plot_group.refresh()
        y_min, y_max = plot_group._plots[0].getViewBox().viewRange()[1]

        self.assertLessEqual(y_min, 0.0)
        self.assertGreaterEqual(y_max, 250.0)


@unittest.skipUnless(_HAS_PYSIDE6, "需要 PySide6 环境")
class DiagnosticsTabCurrentSchemaTests(unittest.TestCase):
    """验证 Diagnostics 固定显示当前 102 字段诊断名。"""

    def setUp(self) -> None:
        from PySide6.QtWidgets import QApplication
        import sys
        self._app = QApplication.instance() or QApplication(sys.argv)

    def test_system_table_contains_current_schema_fields_and_missing_markers(self) -> None:
        from pulseox_monitor.data_manager import DataManager
        from pulseox_monitor.models import FlexibleMessage
        from pulseox_monitor.plot_manager import DiagnosticsTab

        manager = DataManager(max_history=10)
        manager.add_message(FlexibleMessage.from_dict({
            "message": "measurement",
            "schema_version": 3,
            "field_count": 110,
            "parse_ok": True,
            "parse_warnings": ["late field"],
            "extra_fields": ["col110=tail"],
            "raw_line": "M," + ",".join(["0"] * 109),
            "sd_log_active": True,
            "sd_state": 2,
            "sd_error": 0,
            "debug_mode": False,
            "current_page": 3,
            "crash_task": 7,
            "max_task_stack_hwm": 1000,
            "ui_task_heartbeat": 9,
            "ecg_signal_quality": 80,
            "ecg_peak_snr_x100": 95,
        }))

        tab = DiagnosticsTab(manager)
        tab.refresh()

        rows = {}
        table = tab._system_table
        self.assertIsNotNone(table)
        for row in range(table.rowCount()):
            key_item = table.item(row, 0)
            value_item = table.item(row, 1)
            rows[key_item.text()] = value_item.text()

        for key in (
            "sd_log_active", "sd_state", "sd_error", "debug_mode",
            "current_page", "crash_task", "max_task_stack_hwm",
            "ui_task_heartbeat", "raw_line", "extra_fields",
            "parse_warnings", "ecg_signal_quality", "ecg_peak_snr_x100",
        ):
            self.assertIn(key, rows)

        self.assertEqual(rows["sd_log_active"], "True")
        self.assertEqual(rows["sd_state"], "2")
        self.assertEqual(rows["crash_task"], "7")
        self.assertEqual(rows["ecg_signal_quality"], "80")
        self.assertEqual(rows["ecg_peak_snr_x100"], "95")
        self.assertEqual(rows["max_task_heartbeat"], "--")
        self.assertIn("late field", rows["parse_warnings"])

    def test_esp_status_updates_diagnostics_without_entering_data_history(self) -> None:
        from pulseox_monitor.data_manager import DataManager
        from pulseox_monitor.models import FlexibleMessage
        from pulseox_monitor.plot_manager import DiagnosticsTab

        manager = DataManager(max_history=10)
        tab = DiagnosticsTab(manager)
        status = FlexibleMessage.from_dict({
            "message": "esp_status",
            "online": True,
            "usb": {"active": True, "connected": True},
            "mqtt": {"connected": True, "subscribed": True},
            "wifi": {"connected": True},
            "transport": {"active": "mqtt"},
            "esp_stm32_protocol_state": "ok",
            "esp_stm32_last_frame": "M",
            "esp_stm32_last_frame_ms": 1234,
            "protocol_ok": 7,
            "protocol_error": 1,
        })

        tab.set_esp_status_message(status)

        rows = {}
        table = tab._system_table
        self.assertIsNotNone(table)
        for row in range(table.rowCount()):
            key_item = table.item(row, 0)
            value_item = table.item(row, 1)
            rows[key_item.text()] = value_item.text()

        self.assertEqual(len(manager), 0)
        self.assertEqual(rows["esp_online"], "True")
        self.assertEqual(rows["esp_mqtt_connected"], "True")
        self.assertEqual(rows["esp_mqtt_subscribed"], "True")
        self.assertEqual(rows["esp_wifi_connected"], "True")
        self.assertEqual(rows["esp_stm32_protocol_state"], "ok")
        self.assertEqual(rows["esp_stm32_last_frame_ms"], "1234")
        self.assertEqual(rows["protocol_ok"], "7")
        self.assertEqual(rows["protocol_error"], "1")
