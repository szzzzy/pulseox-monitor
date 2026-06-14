# =============================================================================
# 桌面监护程序主窗口 —— 7 Tab 布局，支持 MQTT 和 USB 双传输模式。
#
# 职责：
#   1. 管理 Transport 选择（MQTT / USB），运行时仅一个通道 active。
#   2. MQTT 模式：管理 Broker 连接，接收 ESP32 pulseox/data JSON。
#   3. USB 模式：枚举/连接 COM 口，按行读取 JSON，过滤 ESP_LOG 噪声。
#   4. 通过 MessageDispatcher 将上行数据解析为 FlexibleMessage。
#   5. 将解析后的数据写入 DataManager 环形缓冲。
#   6. 通过 TabPlotManager 驱动 7 个 Tab 页面的实时刷新。
#   7. 提供 Sync Time 按钮，根据当前 Transport 分别走 MQTT 或 USB。
#   8. 在底部 Debug Log 区域显示连接、解析、错误等日志。
#
# MQTT 主题约定：
#   上行（ESP32→GUI）：pulseox/data
#   下行（GUI→ESP32→STM32）：pulseox/cmd
#
# USB 协议约定：
#   连接成功后发送 GUI_USB_START，周期性发送 GUI_USB_PING（5s），
#   断开时发送 GUI_USB_STOP。
#   上行数据为换行分隔的 JSON 行或 STM32 CSV 行。
#
# 时间同步命令格式（当前 STM32 固件）：
#   SETTIME yyyy-mm-dd HH:MM:SS
#   例如：SETTIME 2026-04-14 12:34:56
# =============================================================================

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .broker_manager import (
    BrokerManager,
    broker_port_is_open,
    is_local_broker_host,
    parse_broker_endpoint,
)
from .data_manager import DataManager
from .dispatcher import MessageDispatcher
from .models import FlexibleMessage, MessageValidationError
from .mqtt_handler import MQTTHandler
from .plot_manager import TabPlotManager
from .usb_handler import USBHandler, DEFAULT_BAUDRATE

# ---- MQTT 主题常量 ----
UPSTREAM_TOPIC = "pulseox/data"      # 上行：ESP32 发布测量数据
DOWNSTREAM_TOPIC = "pulseox/cmd"     # 下行：GUI 发送控制命令

# ---- 本地 mosquitto Broker 路径 ----
DEFAULT_MOSQUITTO_EXE = r"D:\MOSQUITTO\mosquitto.exe"
DEFAULT_MOSQUITTO_CONF = r"D:\MOSQUITTO\my_mosquitto.conf"

# ---- Transport 模式 ----
TRANSPORT_MQTT = "MQTT"
TRANSPORT_USB = "USB"
TRANSPORT_MODES = [TRANSPORT_MQTT, TRANSPORT_USB]


class MainWindow(QMainWindow):
    """桌面监护程序主窗口。

    整合了 Transport 选择（MQTT/USB）、Broker 管理、USB 串口通信、
    消息解析、数据存储、多 Tab 可视化。

    窗口布局自上而下：
      1. 连接栏
         ├── Transport 选择器 (MQTT / USB)
         ├── MQTT 面板（Host/Port/User/Pass + Auto Mosquitto + Broker 按钮）
         ├── USB 面板（COM 口选择 + 波特率 + Refresh 按钮）
         ├── Connect / Disconnect / Sync Time 按钮
         └── 连接状态标签
      2. Tab 页区域（Overview / Vitals / SQ / PPG / HRV / ECG-PTT / Diagnostics）
      3. Debug Log 文本框
    """

    # 自定义信号：用于从非 UI 线程安全地写入 Debug Log
    debug_log_signal = Signal(str)

    # =========================================================================
    # 初始化和生命周期
    # =========================================================================

    def __init__(self) -> None:
        """初始化主窗口：创建各子系统、构建 UI、连接信号、应用初始状态。"""
        super().__init__()
        self.setWindowTitle("Pulse Oximeter MQTT/USB Monitor")
        self.resize(1600, 950)

        # ---- 核心子系统 ----
        # 消息分发器：负责将上行 JSON/CSV 解析为 FlexibleMessage
        self.dispatcher = MessageDispatcher()
        # 数据管理器：维护 5000 条的环形缓冲，提供历史序列查询
        self.data_manager = DataManager(max_history=5000)
        # MQTT 客户端处理器：封装 paho-mqtt，管理连接和订阅
        self.mqtt_handler = MQTTHandler(
            upstream_topic=UPSTREAM_TOPIC,
            downstream_topic=DOWNSTREAM_TOPIC,
        )
        # Broker 管理器：负责启动/停止本地 mosquitto Broker
        self.broker_manager = BrokerManager()
        # USB 串口处理器：枚举 COM 口、连接/断开、按行读取、命令发送
        self.usb_handler = USBHandler()

        # 当前激活的 transport 模式
        self._active_transport = TRANSPORT_MQTT

        # 多 Tab 图窗管理器：创建并管理所有 Tab 页面
        # 必须在 _build_ui 之前创建
        self.tab_manager = TabPlotManager(self.data_manager)

        # ---- 构建 UI ----
        self._build_ui()
        self._connect_signals()
        self._apply_initial_state()

        # DataManager 收到新数据后通知 TabPlotManager 刷新
        self.data_manager.data_received.connect(self._on_data_received)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        """窗口关闭时的清理逻辑。

        1. 断开 MQTT 连接（若已连接）。
        2. 断开 USB 连接（若已连接）。
        3. 如果是本程序启动的本地 Broker，则停止它。
        """
        self.mqtt_handler.disconnect_from_broker(silent=True)
        self.usb_handler.disconnect()
        if self.broker_manager.started_by_app:
            self.broker_manager.stop_local_broker()
        super().closeEvent(event)

    # =========================================================================
    # UI 构建
    # =========================================================================

    def _build_ui(self) -> None:
        """构建完整的窗口 UI 布局。

        布局结构：
          root_layout (垂直)
          ├── 连接栏 (_build_connection_bar)
          ├── Tab 页区域 (tab_manager)
          └── Debug Log 文本框 (固定 130px 高度)
        """
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(8, 6, 8, 6)
        root_layout.setSpacing(6)

        # 连接栏：Transport 选择 + MQTT/USB 面板 + 按钮 + 状态标签
        root_layout.addWidget(self._build_connection_bar())

        # 多 Tab 图窗区域（拉伸填充）
        root_layout.addWidget(self.tab_manager, stretch=1)

        # Debug Log 文本框（只读，最多 800 行）
        self.debug_log = QPlainTextEdit()
        self.debug_log.setReadOnly(True)
        self.debug_log.setMaximumBlockCount(800)
        self.debug_log.setPlaceholderText(
            "MQTT/USB、Broker、解析失败和调试日志。"
        )
        self.debug_log.setFixedHeight(130)
        root_layout.addWidget(self.debug_log, stretch=0)

        self.setCentralWidget(root)

    def _build_connection_bar(self) -> QWidget:
        """构建顶部连接栏。

        包含三个区域：
          1. Transport 选择器（MQTT / USB 下拉框）。
          2. MQTT 面板（仅在 MQTT 模式下显示）：
             Host / Port / User / Pass / Auto Mosquitto / Broker Start/Stop。
          3. USB 面板（仅在 USB 模式下显示）：
             COM 口下拉框 / 波特率输入 / Refresh 按钮。

        公共按钮（两种模式共用）：
          Connect / Disconnect / Sync Time。
        """
        bar = QWidget()
        bar.setStyleSheet(
            "QWidget#connectionBar { background: #1a1a2e; border: 1px solid #333; border-radius: 6px; }"
        )
        bar.setObjectName("connectionBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(8)

        # =====================================================================
        # 1. Transport 选择器
        # =====================================================================
        layout.addWidget(QLabel("Transport:"))
        self.transport_combo = QComboBox()
        self.transport_combo.addItems(TRANSPORT_MODES)
        self.transport_combo.setCurrentText(TRANSPORT_MQTT)
        self.transport_combo.setFixedWidth(70)
        layout.addWidget(self.transport_combo)

        # =====================================================================
        # 2. MQTT 面板（widget 组，随 transport 选择显示/隐藏）
        # =====================================================================
        self._mqtt_panel_widgets: list[QWidget] = []

        # Host
        host_label = QLabel("Host:")
        self.host_input = QLineEdit()
        self.host_input.setText("172.20.10.4")
        self.host_input.setFixedWidth(130)
        layout.addWidget(host_label)
        layout.addWidget(self.host_input)
        self._mqtt_panel_widgets.extend([host_label, self.host_input])

        # Port
        port_label = QLabel("Port:")
        self.port_input = QSpinBox()
        self.port_input.setRange(1, 65535)
        self.port_input.setValue(1883)
        self.port_input.setFixedWidth(70)
        layout.addWidget(port_label)
        layout.addWidget(self.port_input)
        self._mqtt_panel_widgets.extend([port_label, self.port_input])

        # User / Pass
        user_label = QLabel("User:")
        self.username_input = QLineEdit()
        self.username_input.setFixedWidth(80)
        layout.addWidget(user_label)
        layout.addWidget(self.username_input)
        self._mqtt_panel_widgets.extend([user_label, self.username_input])

        pass_label = QLabel("Pass:")
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.setFixedWidth(80)
        layout.addWidget(pass_label)
        layout.addWidget(self.password_input)
        self._mqtt_panel_widgets.extend([pass_label, self.password_input])

        # Auto Mosquitto
        self.auto_start_broker_checkbox = QCheckBox("Auto Mosquitto")
        self.auto_start_broker_checkbox.setChecked(True)
        layout.addWidget(self.auto_start_broker_checkbox)
        self._mqtt_panel_widgets.append(self.auto_start_broker_checkbox)

        # Broker 按钮
        self.broker_start_button = QPushButton("Broker Start")
        self.broker_stop_button = QPushButton("Broker Stop")
        layout.addWidget(self.broker_start_button)
        layout.addWidget(self.broker_stop_button)
        self._mqtt_panel_widgets.extend([self.broker_start_button, self.broker_stop_button])

        # =====================================================================
        # 3. USB 面板（widget 组，随 transport 选择显示/隐藏）
        # =====================================================================
        self._usb_panel_widgets: list[QWidget] = []

        # COM 口选择
        com_label = QLabel("COM:")
        self.com_port_combo = QComboBox()
        self.com_port_combo.setFixedWidth(80)
        self.com_port_combo.setToolTip("选择 USB 串口")
        layout.addWidget(com_label)
        layout.addWidget(self.com_port_combo)
        self._usb_panel_widgets.extend([com_label, self.com_port_combo])

        # 波特率输入
        baud_label = QLabel("Baud:")
        self.baud_input = QSpinBox()
        self.baud_input.setRange(9600, 921600)
        self.baud_input.setValue(DEFAULT_BAUDRATE)
        self.baud_input.setFixedWidth(80)
        self.baud_input.setToolTip("波特率（默认 115200）")
        layout.addWidget(baud_label)
        layout.addWidget(self.baud_input)
        self._usb_panel_widgets.extend([baud_label, self.baud_input])

        # Refresh 按钮（重新扫描 COM 口）
        self.com_refresh_button = QPushButton("Refresh")
        self.com_refresh_button.setToolTip("重新扫描可用 COM 口")
        layout.addWidget(self.com_refresh_button)
        self._usb_panel_widgets.append(self.com_refresh_button)

        # =====================================================================
        # 4. 公共操作按钮
        # =====================================================================
        self.connect_button = QPushButton("Connect")
        self.disconnect_button = QPushButton("Disconnect")
        self.time_sync_button = QPushButton("Sync Time")

        layout.addWidget(self.connect_button)
        layout.addWidget(self.disconnect_button)
        layout.addWidget(self.time_sync_button)

        # =====================================================================
        # 5. 连接状态标签
        # =====================================================================
        self.connection_status_label = QLabel("未连接")
        self.connection_status_label.setStyleSheet("color: #f00; font-weight: bold;")
        layout.addWidget(self.connection_status_label)

        layout.addStretch(1)

        # 初始状态：MQTT 模式，USB 面板隐藏
        self._set_transport_panels(TRANSPORT_MQTT)

        return bar

    def _set_transport_panels(self, transport: str) -> None:
        """根据选中的 transport 模式显示/隐藏对应的连接面板。

        MQTT 模式：显示 Host/Port/User/Pass/Auto/Broker 按钮，隐藏 USB 控件。
        USB 模式：显示 COM/Baud/Refresh，隐藏 MQTT 控件。

        参数：
          transport: "MQTT" 或 "USB"。
        """
        is_mqtt = transport == TRANSPORT_MQTT
        for w in self._mqtt_panel_widgets:
            w.setVisible(is_mqtt)
        for w in self._usb_panel_widgets:
            w.setVisible(not is_mqtt)

    # =========================================================================
    # 信号连接
    # =========================================================================

    def _connect_signals(self) -> None:
        """将所有按钮点击事件和子系统信号连接到对应的处理槽函数。

        按钮 → 槽函数：
          transport_combo.currentTextChanged → _on_transport_changed
          Connect        → _on_connect
          Disconnect     → _on_disconnect
          Sync Time      → _publish_time_sync
          Broker Start   → _start_local_broker
          Broker Stop    → _stop_local_broker
          Refresh (COM)  → _refresh_com_ports

        MQTT Handler 信号 → 槽函数：
          connection_state_changed  → _set_connection_status
          connected_changed         → _set_connected_widgets
          upstream_payload_received → _handle_data_line
          debug_message             → _append_debug_log

        USB Handler 信号 → 槽函数：
          line_received             → _handle_data_line
          connection_state_changed  → _set_connection_status
          connected_changed         → _set_connected_widgets
          debug_message             → _append_debug_log

        Broker Manager 信号 → 槽函数：
          status_changed  → _set_broker_status
          running_changed → _set_broker_running_widgets
          debug_message   → _append_debug_log
        """
        # Transport 选择器
        self.transport_combo.currentTextChanged.connect(self._on_transport_changed)

        # 公共按钮
        self.connect_button.clicked.connect(self._on_connect)
        self.disconnect_button.clicked.connect(self._on_disconnect)
        self.time_sync_button.clicked.connect(self._publish_time_sync)

        # MQTT 专用按钮
        self.broker_start_button.clicked.connect(self._start_local_broker)
        self.broker_stop_button.clicked.connect(self._stop_local_broker)

        # USB 专用按钮
        self.com_refresh_button.clicked.connect(self._refresh_com_ports)

        # MQTT Handler 状态信号
        self.mqtt_handler.connection_state_changed.connect(self._set_connection_status)
        self.mqtt_handler.connected_changed.connect(self._set_connected_widgets)
        self.mqtt_handler.upstream_payload_received.connect(self._handle_data_line)
        self.mqtt_handler.debug_message.connect(self._append_debug_log)

        # USB Handler 状态信号
        self.usb_handler.line_received.connect(self._handle_data_line)
        self.usb_handler.connection_state_changed.connect(self._set_connection_status)
        self.usb_handler.connected_changed.connect(self._set_connected_widgets)
        self.usb_handler.debug_message.connect(self._append_debug_log)

        # Broker Manager 状态信号
        self.broker_manager.status_changed.connect(self._set_broker_status)
        self.broker_manager.running_changed.connect(self._set_broker_running_widgets)
        self.broker_manager.debug_message.connect(self._append_debug_log)

    def _apply_initial_state(self) -> None:
        """应用初始 UI 状态。

        - 连接相关按钮：connect 启用，disconnect/time_sync 禁用。
        - Broker 相关按钮：start 启用，stop 禁用。
        - 刷新 COM 口列表。
        - 若检测到外部 Broker 已就绪，输出日志提示。
        """
        self._set_connected_widgets(False)
        self._set_broker_running_widgets(False)
        self._refresh_com_ports()
        host, port = self._normalize_broker_target()
        if host and broker_port_is_open(host, port):
            self._append_debug_log("[BROKER] 检测到外部 Broker 已就绪")

    # =========================================================================
    # Transport 切换
    # =========================================================================

    def _on_transport_changed(self, transport: str) -> None:
        """Transport 下拉框切换时的回调。

        切换规则（运行时仅允许一个通道 active）：
          1. 若当前 MQTT 已连接 → 先断开 MQTT。
          2. 若当前 USB 已连接 → 先断开 USB。
          3. 显示对应面板，隐藏另一个。
          4. 更新 active transport 标记。

        参数：
          transport: 新选择的 transport（"MQTT" 或 "USB"）。
        """
        # 先断开当前连接
        if self._active_transport == TRANSPORT_MQTT:
            self.mqtt_handler.disconnect_from_broker(silent=True)
        else:
            self.usb_handler.disconnect()

        # 切换面板显示
        self._set_transport_panels(transport)

        # 更新 active transport
        self._active_transport = transport

        # 更新按钮状态
        self._set_connected_widgets(False)

        self._append_debug_log(f"[TRANSPORT] 切换到 {transport} 模式")

    # =========================================================================
    # 连接 / 断开（统一入口，根据当前 transport 分发）
    # =========================================================================

    def _on_connect(self) -> None:
        """Connect 按钮点击回调 —— 根据当前 transport 分发连接逻辑。"""
        if self._active_transport == TRANSPORT_MQTT:
            self._connect_mqtt()
        else:
            self._connect_usb()

    def _on_disconnect(self) -> None:
        """Disconnect 按钮点击回调 —— 根据当前 transport 分发断开逻辑。"""
        if self._active_transport == TRANSPORT_MQTT:
            self._disconnect_mqtt()
        else:
            self._disconnect_usb()

    # =========================================================================
    # DataManager → TabPlotManager 数据流
    # =========================================================================

    def _on_data_received(self) -> None:
        """DataManager 发出 data_received 信号时，通知 TabPlotManager 刷新所有 Tab。

        此方法是 DataManager → UI 的数据驱动桥梁。
        实际刷新由 TabPlotManager.update_all() 统一执行，
        它会遍历所有 BaseTab 子类并调用各自的 refresh()。
        """
        self.tab_manager.update_all()

    # =========================================================================
    # 上行数据解析与路由（MQTT 和 USB 共用）
    # =========================================================================

    def _handle_data_line(self, payload_text: str) -> None:
        """处理上行数据行（来自 MQTT 或 USB）。

        MQTT 和 USB 两种 transport 的上行数据统一走此方法处理。

        处理流程：
          1. 通过 MessageDispatcher.dispatch() 解析消息（支持 JSON 和 CSV）。
          2. 若解析为 measurement → 写入 DataManager 环形缓冲，
             触发 data_received 信号，驱动 UI 刷新。
          3. 若解析为 rtc_set_ack → 输出 RTC 对时确认日志。
          4. 若解析为 parse_error → 输出设备端解析错误日志。
          5. 未知消息类型 → 输出警告日志。

        参数：
          payload_text: 上行数据行文本（JSON 字符串或 STM32 CSV 行）。
        """
        # ---- 步骤 1: 解析消息 ----
        try:
            message = self.dispatcher.dispatch(payload_text)
        except MessageValidationError as exc:
            # JSON/CSV 解析失败，记录日志但不崩溃
            self._append_debug_log(f"[DISPATCH] 解析失败: {exc}")
            return

        if message is None:
            # 不可恢复的解析失败
            self._append_debug_log(f"[DISPATCH] 无法解析: {payload_text[:200]}")
            return

        # ---- 步骤 2: 按消息类型路由 ----
        if message.message_type == "measurement":
            # 测量数据：写入缓冲，触发 UI 刷新
            self.data_manager.add_message(message)
            # 简短日志，避免刷屏
            self._append_debug_log(
                f"[DATA] measurement | points={len(self.data_manager)}"
            )

        elif message.message_type == "rtc_set_ack":
            # RTC 对时确认：显示设置结果
            ok = "OK" if message.rtc_set_ok else "FAIL"
            reason = message.rtc_set_reason or ""
            dt = message.device_datetime
            dt_text = dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "无有效RTC"
            self._append_debug_log(
                f"[RTC_ACK] set={ok} rtc_valid={message.rtc_valid} time={dt_text} reason={reason}"
            )

        elif message.message_type == "parse_error":
            # 设备端解析错误：ESP32/STM32 上报自身遇到的错误
            err = message.error_text or "unknown"
            raw_line = message.raw_line or ""
            self._append_debug_log(f"[DEVICE_PARSE_ERROR] error={err} | raw={raw_line}")

        else:
            # 非 measurement / rtc_set_ack / parse_error 的消息：
            # 输出类型和前 20 个 key 便于联调排查
            keys = list(message.raw.keys())[:20]
            self._append_debug_log(
                f"[MSG] type={message.message_type} "
                f"keys={keys}"
            )

    # =========================================================================
    # MQTT 连接逻辑
    # =========================================================================

    def _normalize_broker_target(self, fallback_host: str = "") -> tuple[str, int]:
        """规范化 Broker 地址和端口。

        从 Host 输入框读取原始文本，解析为 (host, port) 元组。
        若输入框为空且指定了 fallback_host，则使用回退地址。
        解析成功后更新输入框的显示值。

        参数：
          fallback_host: 输入为空时使用的回退主机名。

        返回：
          (host, port) 元组。

        异常：
          ValueError —— 输入格式不正确时抛出。
        """
        raw_host = self.host_input.text().strip() or fallback_host
        host, port = parse_broker_endpoint(raw_host, self.port_input.value())
        if raw_host:
            self.host_input.setText(host)
        self.port_input.setValue(port)
        return host, port

    def _connect_mqtt(self) -> None:
        """连接到 MQTT Broker。

        流程：
          1. 规范化地址参数。
          2. 若启用了 "Auto Mosquitto" 且目标为本地地址，
             尝试确保本地 Broker 已运行。
          3. 使用 MQTT Handler 建立连接（含认证）。

        异常处理：
          参数错误 → QMessageBox 警告。
          本地 Broker 无法启动 → QMessageBox 警告。
        """
        # 规范化地址
        try:
            host, port = self._normalize_broker_target()
        except ValueError as exc:
            QMessageBox.warning(self, "参数错误", str(exc))
            return
        if not host:
            QMessageBox.warning(self, "参数错误", "Broker host 不能为空。")
            return

        # 自动启动本地 Broker
        if self.auto_start_broker_checkbox.isChecked() and is_local_broker_host(host):
            ready = self.broker_manager.ensure_broker_running(
                executable_hint=r"D:\MOSQUITTO\mosquitto.exe",
                config_hint=r"D:\MOSQUITTO\my_mosquitto.conf",
                host=host,
                port=port,
            )
            if not ready and not broker_port_is_open(host, port):
                QMessageBox.warning(
                    self,
                    "Broker 未就绪",
                    "未检测到本地 Broker，也无法自动启动 mosquitto。",
                )
                return

        # 建立 MQTT 连接
        self.mqtt_handler.connect_to_broker(
            host=host,
            port=port,
            username=self.username_input.text().strip(),
            password=self.password_input.text(),
            client_id=f"pulseox-monitor-{uuid4().hex[:8]}",
        )

    def _disconnect_mqtt(self) -> None:
        """断开 MQTT Broker 连接。

        调用 MQTT Handler 执行断开操作，释放网络资源。
        """
        self.mqtt_handler.disconnect_from_broker()

    # =========================================================================
    # USB 连接逻辑
    # =========================================================================

    _NO_PORT_PLACEHOLDER = "(无可用端口)"

    def _connect_usb(self) -> None:
        """连接到选中的 USB 串口。

        从 COM 口下拉框和波特率输入框读取参数，
        调用 USBHandler.connect_to_port() 建立连接。

        异常处理：
          COM 口未选择 → QMessageBox 警告。
          COM 口为占位值 → QMessageBox 警告。
          连接失败 → USBHandler 内部通过 debug_message 信号通知。
        """
        port = self.com_port_combo.currentText().strip()
        if not port:
            QMessageBox.warning(self, "参数错误", "请先选择一个 COM 口。")
            return
        if port == self._NO_PORT_PLACEHOLDER:
            QMessageBox.warning(
                self, "无可用端口",
                "未检测到可用 COM 口，请检查硬件连接后点击 Refresh 刷新。",
            )
            return
        baudrate = self.baud_input.value()
        self.usb_handler.connect_to_port(port, baudrate)

    def _disconnect_usb(self) -> None:
        """断开 USB 串口连接。

        调用 USBHandler.disconnect() 执行断开操作，
        包括发送 GUI_USB_STOP、停止 ping、关闭串口。
        """
        self.usb_handler.disconnect()

    def _refresh_com_ports(self) -> None:
        """刷新 COM 口下拉框列表。

        调用 USBHandler.available_ports() 枚举系统可用串口，
        更新下拉框选项，保留当前选中项（若仍存在）。
        """
        current = self.com_port_combo.currentText()
        ports = USBHandler.available_ports()
        self.com_port_combo.clear()
        if ports:
            self.com_port_combo.addItems(ports)
            if current in ports:
                self.com_port_combo.setCurrentText(current)
        else:
            self.com_port_combo.addItem(self._NO_PORT_PLACEHOLDER)
            self._append_debug_log("[USB] 未检测到可用 COM 口")

    # =========================================================================
    # 时间同步（根据当前 transport 路由）
    # =========================================================================

    def _publish_time_sync(self) -> None:
        """向 STM32 发送 RTC 时间同步命令。

        根据当前激活的 transport 选择发送路径：
          - MQTT 模式：通过 MQTT 下行主题 pulseox/cmd 发送。
          - USB 模式：通过 USB 串口直接发送。

        当前 STM32 固件识别的命令格式：
          SETTIME yyyy-mm-dd HH:MM:SS

        若当前未连接，弹出警告对话框。
        """
        now = datetime.now()
        # 构造 SETTIME 命令（STM32 当前协议格式）
        command_text = f"SETTIME {now:%Y-%m-%d} {now:%H:%M:%S}"

        # 根据当前 transport 选择发送路径
        if self._active_transport == TRANSPORT_MQTT:
            sent = self.mqtt_handler.publish_command(command_text)
            transport_label = "MQTT"
        else:
            sent = self.usb_handler.send_command(command_text)
            transport_label = "USB"

        if sent:
            self._append_debug_log(
                f"[TIME_SYNC] [{transport_label}] 已发送: {command_text}"
            )
        else:
            QMessageBox.warning(
                self,
                "发送失败",
                f"当前未连接到 {transport_label} 通道。",
            )

    # =========================================================================
    # 本地 Broker 管理
    # =========================================================================

    def _start_local_broker(self) -> None:
        """手动启动本地 mosquitto Broker。

        使用 _build_connection_bar 中配置的 Host/Port 作为 Broker 地址。
        若 Host 为空，回退到 127.0.0.1。

        启动失败时弹出警告对话框。
        """
        try:
            host, port = self._normalize_broker_target(fallback_host="127.0.0.1")
        except ValueError as exc:
            QMessageBox.warning(self, "参数错误", str(exc))
            return
        started = self.broker_manager.ensure_broker_running(
            executable_hint=r"D:\MOSQUITTO\mosquitto.exe",
            config_hint=r"D:\MOSQUITTO\my_mosquitto.conf",
            host=host,
            port=port,
        )
        if not started:
            QMessageBox.warning(self, "启动失败", "未能启动本地 mosquitto。")

    def _stop_local_broker(self) -> None:
        """停止本地 mosquitto Broker。

        仅停止由本程序启动的 Broker（started_by_app=True）。
        外部或手动启动的 Broker 不受影响。
        """
        self.broker_manager.stop_local_broker()

    # =========================================================================
    # UI 状态更新
    # =========================================================================

    def _set_connection_status(self, status_text: str) -> None:
        """更新连接状态标签的显示。

        根据 status_text 内容判断连接状态：
          - 包含 "已连接" → 绿色
          - 其他 → 红色

        同时将状态文本传递给 TabPlotManager，
        供 Overview Tab 的状态标签使用。

        参数：
          status_text: 连接状态描述文本（由 MQTT Handler 或 USB Handler 发射）。
        """
        self.connection_status_label.setText(status_text)
        connected = "已连接" in status_text
        color = "#0f0" if connected else "#f00"
        self.connection_status_label.setStyleSheet(f"color: {color}; font-weight: bold;")
        self.tab_manager.set_connection_text(status_text)

    def _set_broker_status(self, status_text: str) -> None:
        """更新 Broker 状态显示。

        当前 Broker 状态信息在 Debug Log 中展示，
        此槽函数预留用于未来扩展（如在状态栏显示 Broker PID）。
        """
        pass

    def _set_connected_widgets(self, connected: bool) -> None:
        """根据连接状态更新按钮的启用/禁用。

        连接后：
          - Connect 按钮禁用（已连接，无需重复连接）
          - Disconnect 按钮启用
          - Sync Time 按钮启用（只有连接后才能对时）
          - Transport 选择器禁用（连接中不允许切换）

        参数：
          connected: True 表示任一 transport 已连接。
        """
        self.connect_button.setEnabled(not connected)
        self.disconnect_button.setEnabled(connected)
        self.time_sync_button.setEnabled(connected)
        # 连接中禁止切换 transport
        self.transport_combo.setEnabled(not connected)

    def _set_broker_running_widgets(self, running: bool) -> None:
        """根据本地 Broker 运行状态更新按钮的启用/禁用。

        Broker 运行中：
          - Start 按钮禁用
          - Stop 按钮启用（仅当 Broker 是本程序启动的）

        参数：
          running: True 表示本地 Broker 正在运行。
        """
        self.broker_start_button.setEnabled(not running)
        self.broker_stop_button.setEnabled(running and self.broker_manager.started_by_app)

    def _append_debug_log(self, message: str) -> None:
        """向底部 Debug Log 文本框追加一条带时间戳的日志。

        格式：[HH:MM:SS] 消息内容

        日志自动滚动到最底部（MoveCursor to End）。

        参数：
          message: 日志消息文本。
        """
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.debug_log.appendPlainText(f"[{timestamp}] {message}")
        self.debug_log.moveCursor(QTextCursor.MoveOperation.End)
