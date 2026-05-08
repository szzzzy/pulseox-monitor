# 定义桌面监护程序的主窗口和界面交互逻辑。

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .broker_manager import (
    BrokerManager,
    broker_port_is_open,
    is_local_broker_host,
    parse_broker_endpoint,
)
from .data_manager import DataManager, MeasurementSample
from .dispatcher import MessageDispatcher
from .models import (
    MessageValidationError,
    MeasurementMessage,
    ParseErrorMessage,
    RtcSetAckMessage,
)
from .mqtt_handler import MQTTHandler
from .plot_manager import PlotManager

UPSTREAM_TOPIC = "pulseox/data"
DOWNSTREAM_TOPIC = "pulseox/cmd"
DEFAULT_MOSQUITTO_EXE = r"D:\MOSQUITTO\mosquitto.exe"
DEFAULT_MOSQUITTO_CONF = r"D:\MOSQUITTO\my_mosquitto.conf"


# 整合 broker、MQTT、消息分发、缓存和界面的主窗口。
class MainWindow(QMainWindow):
    def __init__(self) -> None:
        # 初始化核心模块并构建整套 UI。
        super().__init__()
        self.setWindowTitle("Pulse Oximeter MQTT Monitor")
        self.resize(1450, 920)

        self.dispatcher = MessageDispatcher()
        self.data_manager = DataManager(max_history=1500)
        self.plot_manager = PlotManager()
        self.mqtt_handler = MQTTHandler(
            upstream_topic=UPSTREAM_TOPIC,
            downstream_topic=DOWNSTREAM_TOPIC,
        )
        self.broker_manager = BrokerManager()

        self._build_ui()
        self._connect_signals()
        self._apply_initial_state()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        # 窗口关闭时清理 MQTT 连接和本应用启动的 broker。
        self.mqtt_handler.disconnect_from_broker(silent=True)
        if self.broker_manager.started_by_app:
            self.broker_manager.stop_local_broker()
        super().closeEvent(event)

    def _build_ui(self) -> None:
        # 创建主界面布局，包括控制区、曲线区和调试日志区。
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(10)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_side_panel())
        splitter.addWidget(self.plot_manager.as_widget())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        root_layout.addWidget(splitter, stretch=4)

        self.debug_log = QPlainTextEdit()
        self.debug_log.setReadOnly(True)
        self.debug_log.document().setMaximumBlockCount(800)
        self.debug_log.setPlaceholderText("MQTT、Broker、解析失败和调试日志会显示在这里。")
        root_layout.addWidget(self.debug_log, stretch=1)

        self.setCentralWidget(root)

    def _build_side_panel(self) -> QWidget:
        # 创建左侧控制面板。
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        layout.addWidget(self._build_connection_group())
        layout.addWidget(self._build_status_group())
        layout.addStretch(1)
        return panel

    def _build_connection_group(self) -> QGroupBox:
        # 创建 MQTT 连接参数与本地 broker 管理区域。
        group = QGroupBox("MQTT 与 Broker")
        layout = QGridLayout(group)

        self.host_input = QLineEdit()
        self.host_input.setPlaceholderText("172.20.10.4")
        self.host_input.setToolTip("只需填写主机名或 IP；程序会按 MQTT 协议连接。")
        self.port_input = QSpinBox()
        self.port_input.setRange(1, 65535)
        self.port_input.setValue(1883)
        self.username_input = QLineEdit()
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.client_id_input = QLineEdit(f"pulseox-monitor-{uuid4().hex[:8]}")

        self.broker_path_input = QLineEdit()
        self.broker_path_input.setPlaceholderText(DEFAULT_MOSQUITTO_EXE)
        self.broker_conf_input = QLineEdit()
        self.broker_conf_input.setPlaceholderText(DEFAULT_MOSQUITTO_CONF)
        self.broker_browse_button = QPushButton("浏览")
        self.broker_conf_browse_button = QPushButton("浏览配置")
        self.broker_start_button = QPushButton("启动 Broker")
        self.broker_stop_button = QPushButton("停止 Broker")
        self.auto_start_broker_checkbox = QCheckBox("连接本地 Broker 时自动拉起 mosquitto")
        self.auto_start_broker_checkbox.setChecked(True)

        self.connect_button = QPushButton("连接")
        self.disconnect_button = QPushButton("断开")
        self.time_sync_button = QPushButton("时间同步")

        host_row = QHBoxLayout()
        host_row.setContentsMargins(0, 0, 0, 0)
        host_row.addWidget(QLabel("mqtt://"))
        host_row.addWidget(self.host_input, stretch=1)

        broker_path_row = QHBoxLayout()
        broker_path_row.setContentsMargins(0, 0, 0, 0)
        broker_path_row.addWidget(self.broker_path_input, stretch=1)
        broker_path_row.addWidget(self.broker_browse_button)

        broker_conf_row = QHBoxLayout()
        broker_conf_row.setContentsMargins(0, 0, 0, 0)
        broker_conf_row.addWidget(self.broker_conf_input, stretch=1)
        broker_conf_row.addWidget(self.broker_conf_browse_button)

        broker_button_row = QHBoxLayout()
        broker_button_row.setContentsMargins(0, 0, 0, 0)
        broker_button_row.addWidget(self.broker_start_button)
        broker_button_row.addWidget(self.broker_stop_button)

        mqtt_button_row = QHBoxLayout()
        mqtt_button_row.setContentsMargins(0, 0, 0, 0)
        mqtt_button_row.addWidget(self.connect_button)
        mqtt_button_row.addWidget(self.disconnect_button)
        mqtt_button_row.addWidget(self.time_sync_button)

        layout.addWidget(QLabel("Host"), 0, 0)
        layout.addLayout(host_row, 0, 1)
        layout.addWidget(QLabel("Port"), 1, 0)
        layout.addWidget(self.port_input, 1, 1)
        layout.addWidget(QLabel("Username"), 2, 0)
        layout.addWidget(self.username_input, 2, 1)
        layout.addWidget(QLabel("Password"), 3, 0)
        layout.addWidget(self.password_input, 3, 1)
        layout.addWidget(QLabel("Client ID"), 4, 0)
        layout.addWidget(self.client_id_input, 4, 1)
        layout.addWidget(QLabel("Mosquitto"), 5, 0)
        layout.addLayout(broker_path_row, 5, 1)
        layout.addWidget(QLabel("Broker Conf"), 6, 0)
        layout.addLayout(broker_conf_row, 6, 1)
        layout.addWidget(self.auto_start_broker_checkbox, 7, 0, 1, 2)
        layout.addLayout(broker_button_row, 8, 0, 1, 2)
        layout.addLayout(mqtt_button_row, 9, 0, 1, 2)
        return group

    def _build_status_group(self) -> QGroupBox:
        # 创建实时状态面板。
        group = QGroupBox("状态面板")
        layout = QFormLayout(group)

        self.connection_status_label = QLabel("未连接")
        self.broker_status_label = QLabel("未启动")
        self.upstream_topic_label = QLabel(UPSTREAM_TOPIC)
        self.downstream_topic_label = QLabel(DOWNSTREAM_TOPIC)
        self.history_count_label = QLabel("0")
        self.timestamp_label = QLabel("-")
        self.timestamp_source_label = QLabel("-")
        self.finger_label = QLabel("-")
        self.ir_label = QLabel("-")
        self.red_label = QLabel("-")
        self.bpm_label = QLabel("-")
        self.spo2_label = QLabel("-")
        self.rtc_ack_label = QLabel("尚未收到 RTC 设置应答")

        for label in (
            self.connection_status_label,
            self.broker_status_label,
            self.upstream_topic_label,
            self.downstream_topic_label,
            self.history_count_label,
            self.timestamp_label,
            self.timestamp_source_label,
            self.finger_label,
            self.ir_label,
            self.red_label,
            self.bpm_label,
            self.spo2_label,
            self.rtc_ack_label,
        ):
            label.setWordWrap(True)

        layout.addRow("连接状态", self.connection_status_label)
        layout.addRow("Broker 状态", self.broker_status_label)
        layout.addRow("上行主题", self.upstream_topic_label)
        layout.addRow("下行主题", self.downstream_topic_label)
        layout.addRow("历史点数", self.history_count_label)
        layout.addRow("最新时间", self.timestamp_label)
        layout.addRow("时间来源", self.timestamp_source_label)
        layout.addRow("Finger", self.finger_label)
        layout.addRow("IR", self.ir_label)
        layout.addRow("Red", self.red_label)
        layout.addRow("BPM", self.bpm_label)
        layout.addRow("SpO2", self.spo2_label)
        layout.addRow("RTC 设置应答", self.rtc_ack_label)
        return group

    def _connect_signals(self) -> None:
        # 连接界面事件和业务模块信号。
        self.broker_browse_button.clicked.connect(self._browse_broker_path)
        self.broker_conf_browse_button.clicked.connect(self._browse_broker_conf)
        self.broker_start_button.clicked.connect(self._start_local_broker)
        self.broker_stop_button.clicked.connect(self._stop_local_broker)
        self.connect_button.clicked.connect(self._connect_to_broker)
        self.disconnect_button.clicked.connect(self._disconnect_from_broker)
        self.time_sync_button.clicked.connect(self._publish_time_sync)

        self.mqtt_handler.connection_state_changed.connect(self._set_connection_status)
        self.mqtt_handler.connected_changed.connect(self._set_connected_widgets)
        self.mqtt_handler.upstream_payload_received.connect(self._handle_upstream_payload)
        self.mqtt_handler.debug_message.connect(self._append_debug_log)

        self.broker_manager.status_changed.connect(self._set_broker_status)
        self.broker_manager.running_changed.connect(self._set_broker_running_widgets)
        self.broker_manager.debug_message.connect(self._append_debug_log)

    def _apply_initial_state(self) -> None:
        # 设置控件初始状态，并填入固定的 mosquitto 路径。
        self.broker_path_input.setText(DEFAULT_MOSQUITTO_EXE)
        self.broker_conf_input.setText(DEFAULT_MOSQUITTO_CONF)

        self._set_connected_widgets(False)
        self._set_broker_running_widgets(False)

        # 如果当前 host:port 已经可连，优先提示用户 broker 已就绪。
        host, port = self._normalize_broker_target()
        if host and broker_port_is_open(host, port):
            self._set_broker_status("检测到外部 Broker")

    def _normalize_broker_target(self, fallback_host: str = "") -> tuple[str, int]:
        # 读取并规范化 broker 地址输入，兼容误贴完整 URI 的情况。
        raw_host = self.host_input.text().strip() or fallback_host
        host, port = parse_broker_endpoint(raw_host, self.port_input.value())
        if raw_host:
            self.host_input.setText(host)
        self.port_input.setValue(port)
        return host, port

    def _browse_broker_path(self) -> None:
        # 让用户手动选择 mosquitto 可执行文件。
        current_dir = self.broker_path_input.text().strip() or str(Path.cwd())
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择 mosquitto 可执行文件",
            current_dir,
            "Executable (*.exe);;All Files (*)",
        )
        if file_path:
            self.broker_path_input.setText(file_path)

    def _browse_broker_conf(self) -> None:
        # 让用户手动选择 mosquitto 配置文件。
        current_dir = self.broker_conf_input.text().strip() or str(Path.cwd())
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择 mosquitto 配置文件",
            current_dir,
            "Config Files (*.conf);;All Files (*)",
        )
        if file_path:
            self.broker_conf_input.setText(file_path)

    def _start_local_broker(self) -> None:
        # 手动启动当前 host 对应的本地 broker。
        try:
            host, port = self._normalize_broker_target(fallback_host="127.0.0.1")
        except ValueError as exc:
            QMessageBox.warning(self, "参数错误", str(exc))
            return
        started = self.broker_manager.ensure_broker_running(
            executable_hint=self.broker_path_input.text(),
            config_hint=self.broker_conf_input.text(),
            host=host,
            port=port,
        )
        if not started:
            QMessageBox.warning(
                self,
                "启动失败",
                "未能启动本地 mosquitto。请确认可执行文件和配置文件路径都正确。",
            )

    def _stop_local_broker(self) -> None:
        # 停止由本应用拉起的本地 broker。
        self.broker_manager.stop_local_broker()

    def _connect_to_broker(self) -> None:
        # 读取界面参数并发起 MQTT 连接。
        try:
            host, port = self._normalize_broker_target()
        except ValueError as exc:
            QMessageBox.warning(self, "参数错误", str(exc))
            return
        if not host:
            QMessageBox.warning(self, "参数错误", "Broker host 不能为空。")
            return

        # 仅当目标是本机且用户允许时，才尝试自动拉起本地 mosquitto。
        if self.auto_start_broker_checkbox.isChecked() and is_local_broker_host(host):
            ready = self.broker_manager.ensure_broker_running(
                executable_hint=self.broker_path_input.text(),
                config_hint=self.broker_conf_input.text(),
                host=host,
                port=port,
            )
            if not ready and not broker_port_is_open(host, port):
                QMessageBox.warning(
                    self,
                    "Broker 未就绪",
                    "未检测到本地 Broker，也无法自动启动 mosquitto。\n"
                    "请检查 mosquitto.exe 与配置文件路径，或先手动启动 Broker。",
                )
                return

        self.mqtt_handler.connect_to_broker(
            host=host,
            port=port,
            username=self.username_input.text().strip(),
            password=self.password_input.text(),
            client_id=self.client_id_input.text().strip(),
        )

    def _disconnect_from_broker(self) -> None:
        # 主动断开当前 MQTT 连接。
        self.mqtt_handler.disconnect_from_broker()

    def _publish_time_sync(self) -> None:
        # 用当前 PC 时间生成并发布 RTC 同步命令。
        now = datetime.now()
        command_text = f"T,{now:%Y%m%d},{now:%H%M%S}"
        sent = self.mqtt_handler.publish_command(command_text)
        if sent:
            self.rtc_ack_label.setText(f"已发送时间同步命令，等待应答: {command_text}")
        else:
            QMessageBox.warning(
                self,
                "发送失败",
                "当前未连接到 MQTT Broker，无法发送时间同步命令。",
            )

    def _handle_upstream_payload(self, payload_text: str) -> None:
        # 处理所有 MQTT 上行消息，并按类型更新不同界面区域。
        try:
            message = self.dispatcher.dispatch(payload_text)
        except MessageValidationError as exc:
            self._append_debug_log(f"[DISPATCH] 解析失败: {exc} | payload={payload_text}")
            return

        # 只有 measurement 会进入 DataManager，这是协议层面的硬约束。
        if isinstance(message, MeasurementMessage):
            sample = self.data_manager.add_measurement(message)
            self.plot_manager.update(self.data_manager.plot_series())
            self._update_measurement_status(sample)
            return

        if isinstance(message, RtcSetAckMessage):
            self._update_rtc_ack_status(message)
            return

        if isinstance(message, ParseErrorMessage):
            self._append_debug_log(
                f"[DEVICE_PARSE_ERROR] error={message.error} | raw={message.raw}"
            )

    def _update_measurement_status(self, sample: MeasurementSample) -> None:
        # 用最新测量样本刷新状态面板。
        self.history_count_label.setText(str(len(self.data_manager)))
        self.finger_label.setText("在位" if sample.finger else "离位")
        self.ir_label.setText(str(sample.ir))
        self.red_label.setText(str(sample.red))
        self.bpm_label.setText(str(sample.bpm) if sample.bpm_valid and sample.bpm is not None else "无效")
        self.spo2_label.setText(
            str(sample.spo2) if sample.spo2_valid and sample.spo2 is not None else "无效"
        )

        # 时间显示拆成“当前展示时间”和“时间是否可信”两部分，便于联调时判断 RTC 状态。
        self.timestamp_label.setText(sample.plot_timestamp.strftime("%Y-%m-%d %H:%M:%S"))
        if sample.timestamp_valid:
            self.timestamp_source_label.setText("设备 RTC 有效")
        else:
            self.timestamp_source_label.setText("设备 RTC 无效，已回退到 PC 接收时间")

    def _update_rtc_ack_status(self, message: RtcSetAckMessage) -> None:
        # 把 RTC 设置应答展示到状态面板。
        status = "成功" if message.set_ok else "失败"
        time_text = (
            message.device_datetime.strftime("%Y-%m-%d %H:%M:%S")
            if message.device_datetime is not None
            else "无有效 RTC 时间"
        )
        reason_text = f" | reason={message.reason}" if message.reason else ""
        self.rtc_ack_label.setText(
            f"{status} | rtc_valid={message.rtc_valid} | time={time_text}{reason_text}"
        )

    def _set_connection_status(self, status_text: str) -> None:
        # 更新连接状态标签。
        self.connection_status_label.setText(status_text)

    def _set_broker_status(self, status_text: str) -> None:
        # 更新 broker 状态标签。
        self.broker_status_label.setText(status_text)

    def _set_connected_widgets(self, connected: bool) -> None:
        # 根据 MQTT 连接状态启用或禁用相关按钮。
        self.connect_button.setEnabled(not connected)
        self.disconnect_button.setEnabled(connected)
        self.time_sync_button.setEnabled(connected)

    def _set_broker_running_widgets(self, running: bool) -> None:
        # 根据 broker 运行状态调整 broker 控件。
        self.broker_start_button.setEnabled(not running)
        self.broker_stop_button.setEnabled(running and self.broker_manager.started_by_app)

    def _append_debug_log(self, message: str) -> None:
        # 向底部调试日志面板追加一行文本。
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.debug_log.appendPlainText(f"[{timestamp}] {message}")
        self.debug_log.moveCursor(QTextCursor.MoveOperation.End)
