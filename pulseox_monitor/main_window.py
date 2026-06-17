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
#   传感器数据（ESP32→GUI）：pulseox/data
#   ESP32 状态（ESP32→GUI，retained）：pulseox/status
#   下行（GUI→ESP32→STM32）：pulseox/cmd
#
# USB 协议约定：
#   连接成功后发送 GUI_USB_START，周期性发送 GUI_USB_PING（5s），
#   断开时发送 GUI_USB_STOP。
#   上行数据为换行分隔的 JSON 行或 STM32 CSV 行。
# 时间同步命令格式（当前 STM32 固件）：
#   SETTIME yyyy-mm-dd HH:MM:SS
#   例如：SETTIME 2026-04-14 12:34:56
# =============================================================================

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import Qt, QTimer, Signal
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
STATUS_TOPIC = "pulseox/status"      # ESP32 状态主题（retained）
DOWNSTREAM_TOPIC = "pulseox/cmd"     # 下行：GUI 发送控制命令
DEFAULT_MQTT_BROKER_URI = os.environ.get("MQTT_BROKER_URI", "172.20.10.4")
ESP_STATUS_TIMEOUT_SECONDS = 8

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
            status_topic=STATUS_TOPIC,
            downstream_topic=DOWNSTREAM_TOPIC,
        )
        # Broker 管理器：负责启动/停止本地 mosquitto Broker
        self.broker_manager = BrokerManager()
        # USB 串口处理器：枚举 COM 口、连接/断开、按行读取、命令发送
        self.usb_handler = USBHandler()

        # 当前激活的 transport 模式
        self._active_transport = TRANSPORT_MQTT
        self._esp_online: bool | None = None
        self._esp_usb_active: bool | None = None
        self._esp_usb_connected: bool | None = None
        self._esp_mqtt_connected: bool | None = None
        self._esp_mqtt_subscribed: bool | None = None
        self._esp_wifi_connected: bool | None = None
        self._esp_transport_active: str | None = None
        self._esp_stm32_protocol_state: str | None = None
        self._esp_stm32_last_frame: str | None = None
        self._esp_stm32_last_frame_ms: int | None = None
        self._esp_protocol_ok_count: int | None = None
        self._esp_protocol_error_count: int | None = None
        self._last_esp_status_at: datetime | None = None
        self._esp_status_timed_out = False

        # 多 Tab 图窗管理器：创建并管理所有 Tab 页面
        # 必须在 _build_ui 之前创建
        self.tab_manager = TabPlotManager(self.data_manager)

        # ---- 构建 UI ----
        self._build_ui()
        self._connect_signals()
        self._apply_initial_state()

        # DataManager 收到新数据后通知 TabPlotManager 刷新
        self.data_manager.data_received.connect(self._on_data_received)

        # USB COM 口自动刷新定时器（每 2 秒，仅在 USB 模式且未连接时生效）
        self._com_refresh_timer = QTimer(self)
        self._com_refresh_timer.timeout.connect(self._auto_refresh_com_ports)
        self._com_refresh_timer.start(2000)

        # ESP32 状态超时检查：状态主题约 6-8 秒内未刷新时置灰/离线
        self._esp_status_timeout_timer = QTimer(self)
        self._esp_status_timeout_timer.timeout.connect(self._check_esp_status_timeout)
        self._esp_status_timeout_timer.start(1000)

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
        self.host_input.setText(DEFAULT_MQTT_BROKER_URI)
        self.host_input.setFixedWidth(130)
        self.host_input.setToolTip("Broker host 或 mqtt://host:port；默认读取环境变量 MQTT_BROKER_URI")
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

        # COM 口选择（可编辑，支持手动输入端口名）
        com_label = QLabel("COM:")
        self.com_port_combo = QComboBox()
        self.com_port_combo.setEditable(True)
        self.com_port_combo.setMinimumWidth(180)
        self.com_port_combo.setToolTip("选择或手动输入 USB 串口（如 COM3）")
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
        self.time_sync_button = QPushButton("Send PC Time")
        self.time_sync_button.setToolTip("发送 PC 当前时间给设备，仅用于设备日志/显示，不影响 GUI 曲线时间")
        self.time_sync_button.setMinimumWidth(100)

        layout.addWidget(self.connect_button)
        layout.addWidget(self.disconnect_button)
        layout.addWidget(self.time_sync_button)

        # =====================================================================
        # 5. 连接状态标签
        # =====================================================================
        self.connection_status_label = QLabel("未连接")
        self.connection_status_label.setStyleSheet("color: #f00; font-weight: bold;")
        layout.addWidget(self.connection_status_label)

        self.manual_transport_label = QLabel("逻辑: MQTT")
        self.manual_transport_label.setStyleSheet("color: #0ff; font-weight: bold;")
        self.manual_transport_label.setToolTip("当前 GUI 手动选择的通信逻辑")
        layout.addWidget(self.manual_transport_label)

        self.esp_online_status_label = QLabel("ESP 在线: 未知")
        self.esp_online_status_label.setStyleSheet("color: #888; font-weight: bold;")
        self.esp_online_status_label.setToolTip("最近状态主题刷新时间判断；超时或 online=false 为离线")
        layout.addWidget(self.esp_online_status_label)

        self.esp_usb_status_label = QLabel("USB 会话: 未知")
        self.esp_usb_status_label.setStyleSheet("color: #888; font-weight: bold;")
        self.esp_usb_status_label.setToolTip("status.usb.active；括号内为 USB 物理连接状态")
        layout.addWidget(self.esp_usb_status_label)

        self.esp_mqtt_status_label = QLabel("MQTT: 未知")
        self.esp_mqtt_status_label.setStyleSheet("color: #888; font-weight: bold;")
        self.esp_mqtt_status_label.setToolTip("status.mqtt.connected；括号内显示 subscribed 状态")
        layout.addWidget(self.esp_mqtt_status_label)

        self.esp_wifi_status_label = QLabel("WiFi: 未知")
        self.esp_wifi_status_label.setStyleSheet("color: #888; font-weight: bold;")
        self.esp_wifi_status_label.setToolTip("status.wifi.connected")
        layout.addWidget(self.esp_wifi_status_label)

        self.esp_channel_status_label = QLabel("通道: 未知")
        self.esp_channel_status_label.setStyleSheet("color: #888; font-weight: bold;")
        self.esp_channel_status_label.setToolTip("status.transport.active")
        layout.addWidget(self.esp_channel_status_label)

        self.esp_stm32_status_label = QLabel("STM32: 未知")
        self.esp_stm32_status_label.setStyleSheet("color: #888; font-weight: bold;")
        self.esp_stm32_status_label.setToolTip("STM32 协议状态、最近帧和成功/错误计数")
        layout.addWidget(self.esp_stm32_status_label)

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
        self._refresh_manual_transport_label()
        self._refresh_esp_status_labels()
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
          4. 若切换到 USB，自动刷新 COM 口列表。
          5. 更新 active transport 标记。

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
        self._refresh_manual_transport_label()

        # 更新按钮状态
        self._set_connected_widgets(False)
        self._set_connection_status(f"{transport} 未连接")

        self._append_debug_log(f"[TRANSPORT] 切换到 {transport} 模式")

        # 切换到 USB 时立即刷新 COM 口列表
        if transport == TRANSPORT_USB:
            self._refresh_com_ports()

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
        if message.message_type == "esp_status":
            self._update_esp_status_from_message(message)

        elif message.message_type == "measurement":
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

        优先使用 itemData（原始设备名如 "COM3"），
        若用户手动输入则使用 currentText()。
        """
        # 优先取 itemData（原始设备名），用户手动输入时回退到 currentText
        device_data = self.com_port_combo.currentData()
        if device_data and isinstance(device_data, str):
            port = device_data.strip()
        else:
            port = self.com_port_combo.currentText().strip()

        if not port:
            QMessageBox.warning(self, "参数错误", "请先选择或输入一个 COM 口。")
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
        """刷新 COM 口下拉框列表（含详细诊断和描述信息）。

        调用 USBHandler.available_ports() 枚举系统可用串口，
        格式化为 "COM5 - USB-SERIAL CH340" 显示在下拉框中。
        若无端口，根据原因给出明确诊断。
        """
        current = self.com_port_combo.currentText()

        # 检查 pyserial 是否可用
        if not USBHandler.check_pyserial_available():
            self.com_port_combo.clear()
            self.com_port_combo.addItem(self._NO_PORT_PLACEHOLDER)
            self.com_port_combo.setToolTip("pyserial 未安装 — 请运行: pip install pyserial")
            self._append_debug_log("[USB] pyserial 未安装，无法枚举 COM 口。请运行: pip install pyserial")
            return

        try:
            port_infos = USBHandler.available_ports()
        except Exception as exc:
            self.com_port_combo.clear()
            self.com_port_combo.addItem(self._NO_PORT_PLACEHOLDER)
            self.com_port_combo.setToolTip(f"COM 口枚举异常: {exc}")
            self._append_debug_log(f"[USB] COM 口枚举失败: {exc}")
            return

        self.com_port_combo.clear()

        if port_infos:
            # 构建显示文本：COM5 - USB-SERIAL CH340
            for info in port_infos:
                device = info["device"]
                desc = info["description"]
                if desc and desc != device:
                    display = f"{device} - {desc}"
                else:
                    display = device
                self.com_port_combo.addItem(display, device)

            # 尝试恢复上次选中项
            restored = False
            for i in range(self.com_port_combo.count()):
                item_device = self.com_port_combo.itemData(i)
                item_text = self.com_port_combo.itemText(i)
                if current and (item_device == current or item_text == current):
                    self.com_port_combo.setCurrentIndex(i)
                    restored = True
                    break
            if not restored and current:
                # 用户可能手动输入了端口名，保留文本
                self.com_port_combo.setCurrentText(current)

            self.com_port_combo.setToolTip(
                f"检测到 {len(port_infos)} 个 COM 口"
            )
        else:
            self.com_port_combo.addItem(self._NO_PORT_PLACEHOLDER)
            self.com_port_combo.setToolTip(
                "未检测到 COM 口 — 请检查 USB 连接和驱动"
            )
            self._append_debug_log(
                "[USB] 未检测到 COM 口（pyserial 已安装，但系统无可用串口）。"
                " 请检查：1) USB 线缆是否连接  2) 驱动是否安装 (CH340/CP210x)"
            )

    def _auto_refresh_com_ports(self) -> None:
        """自动刷新 COM 口列表（定时器回调）。

        仅在 USB 模式且未连接时刷新，用于热插拔检测。
        已连接时不刷新以避免干扰当前连接的下拉框状态。
        """
        if self._active_transport != TRANSPORT_USB:
            return
        if self.usb_handler.is_connected():
            return
        self._refresh_com_ports()

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

    def _refresh_manual_transport_label(self) -> None:
        """刷新 GUI 手动选择的 transport 逻辑标签。"""
        if hasattr(self, "manual_transport_label"):
            self.manual_transport_label.setText(f"逻辑: {self._active_transport}")

    @staticmethod
    def _tri_state_text(value: bool | None) -> tuple[str, str]:
        """将三态连接状态转换为显示文本和颜色。"""
        if value is True:
            return "已连接", "#0f0"
        if value is False:
            return "未连接", "#f00"
        return "未知", "#888"

    def _refresh_esp_status_labels(self) -> None:
        """刷新 ESP32 状态显示。"""
        fresh = self._esp_status_is_fresh()
        online = self._effective_esp_online()
        online_text, online_color = self._online_text(online)

        usb_active = self._fresh_value(self._esp_usb_active)
        usb_physical = self._fresh_value(self._esp_usb_connected)
        mqtt_connected = self._fresh_value(self._esp_mqtt_connected)
        mqtt_subscribed = self._fresh_value(self._esp_mqtt_subscribed)
        wifi_connected = self._fresh_value(self._esp_wifi_connected)

        usb_text, usb_color = self._tri_state_text(usb_active)
        usb_physical_text, _ = self._tri_state_text(usb_physical)
        mqtt_text, mqtt_color = self._tri_state_text(mqtt_connected)
        mqtt_sub_text, _ = self._tri_state_text(mqtt_subscribed)
        wifi_text, wifi_color = self._tri_state_text(wifi_connected)

        channel_text = self._channel_display_text(
            self._esp_transport_active if fresh else None
        )
        channel_color = "#0ff" if fresh and self._esp_transport_active else "#888"

        stm32_text = self._stm32_status_text(fresh)
        stm32_state = (self._esp_stm32_protocol_state or "").lower()
        stm32_color = "#0f0" if fresh and stm32_state == "ok" else "#888"
        if fresh and stm32_state and stm32_state != "ok":
            stm32_color = "#f80"

        if hasattr(self, "esp_online_status_label"):
            self.esp_online_status_label.setText(f"ESP 在线: {online_text}")
            self.esp_online_status_label.setStyleSheet(
                f"color: {online_color}; font-weight: bold;"
            )

        if hasattr(self, "esp_usb_status_label"):
            self.esp_usb_status_label.setText(
                f"USB 会话: {usb_text} (物理: {usb_physical_text})"
            )
            self.esp_usb_status_label.setStyleSheet(
                f"color: {usb_color}; font-weight: bold;"
            )
        if hasattr(self, "esp_mqtt_status_label"):
            self.esp_mqtt_status_label.setText(
                f"MQTT: {mqtt_text} (订阅: {mqtt_sub_text})"
            )
            self.esp_mqtt_status_label.setStyleSheet(
                f"color: {mqtt_color}; font-weight: bold;"
            )
        if hasattr(self, "esp_wifi_status_label"):
            self.esp_wifi_status_label.setText(f"WiFi: {wifi_text}")
            self.esp_wifi_status_label.setStyleSheet(
                f"color: {wifi_color}; font-weight: bold;"
            )
        if hasattr(self, "esp_channel_status_label"):
            self.esp_channel_status_label.setText(f"通道: {channel_text}")
            self.esp_channel_status_label.setStyleSheet(
                f"color: {channel_color}; font-weight: bold;"
            )
        if hasattr(self, "esp_stm32_status_label"):
            self.esp_stm32_status_label.setText(f"STM32: {stm32_text}")
            self.esp_stm32_status_label.setStyleSheet(
                f"color: {stm32_color}; font-weight: bold;"
            )

        summary = (
            f"ESP: {online_text}  |  USB会话: {usb_text} / 物理: {usb_physical_text}"
            f"  |  MQTT: {mqtt_text} / 订阅: {mqtt_sub_text}"
            f"  |  WiFi: {wifi_text}  |  通道: {channel_text}"
            f"  |  STM32: {stm32_text}"
        )
        if self._last_esp_status_at:
            summary += f"  |  {self._last_esp_status_at.strftime('%H:%M:%S')}"
        self.tab_manager.set_esp_status_text(summary)

    def _update_esp_status_from_message(self, message: FlexibleMessage) -> None:
        """从 esp_status 消息中提取 ESP32/链路/STM32 状态。"""
        self._esp_online = message.esp_online
        self._esp_usb_active = message.esp_usb_active
        self._esp_usb_connected = message.esp_usb_connected
        self._esp_mqtt_connected = message.esp_mqtt_connected
        self._esp_mqtt_subscribed = message.esp_mqtt_subscribed
        self._esp_wifi_connected = message.esp_wifi_connected
        self._esp_transport_active = message.esp_transport_active
        self._esp_stm32_protocol_state = message.esp_stm32_protocol_state
        self._esp_stm32_last_frame = message.esp_stm32_last_frame
        self._esp_stm32_last_frame_ms = message.esp_stm32_last_frame_ms
        self._esp_protocol_ok_count = message.esp_protocol_ok_count
        self._esp_protocol_error_count = message.esp_protocol_error_count
        self._last_esp_status_at = datetime.now()
        self._esp_status_timed_out = False
        self._refresh_esp_status_labels()
        online_text, _ = self._online_text(self._effective_esp_online())
        channel_text = self._channel_display_text(self._esp_transport_active)
        self._append_debug_log(
            f"[ESP32] online={online_text} channel={channel_text} "
            f"STM32={self._stm32_status_text(True)}"
        )

    def _esp_status_is_fresh(self) -> bool:
        if self._last_esp_status_at is None:
            return False
        return datetime.now() - self._last_esp_status_at <= timedelta(
            seconds=ESP_STATUS_TIMEOUT_SECONDS
        )

    def _effective_esp_online(self) -> bool | None:
        if self._last_esp_status_at is None:
            return None
        if self._esp_online is False:
            return False
        return self._esp_status_is_fresh()

    def _fresh_value(self, value: bool | None) -> bool | None:
        return value if self._esp_status_is_fresh() else None

    @staticmethod
    def _online_text(value: bool | None) -> tuple[str, str]:
        if value is True:
            return "在线", "#0f0"
        if value is False:
            return "离线", "#f00"
        return "未知", "#888"

    @staticmethod
    def _channel_display_text(value: str | None) -> str:
        if not value:
            return "未知"
        normalized = value.strip().lower()
        labels = {
            "usb": "USB",
            "mqtt": "MQTT",
            "usb_idle": "USB 空闲",
            "offline": "离线",
        }
        return labels.get(normalized, value)

    def _stm32_status_text(self, fresh: bool) -> str:
        if not fresh:
            return "未知"
        state = self._esp_stm32_protocol_state or "未知"
        frame = self._esp_stm32_last_frame or "--"
        frame_ms = "--" if self._esp_stm32_last_frame_ms is None else str(self._esp_stm32_last_frame_ms)
        ok = "--" if self._esp_protocol_ok_count is None else str(self._esp_protocol_ok_count)
        err = "--" if self._esp_protocol_error_count is None else str(self._esp_protocol_error_count)
        return f"{state} frame={frame} {frame_ms}ms ok/err={ok}/{err}"

    def _check_esp_status_timeout(self) -> None:
        """定时检查 ESP 状态是否超时，超时后置灰链路状态。"""
        if self._last_esp_status_at is None:
            return
        if self._esp_status_is_fresh():
            return
        if not self._esp_status_timed_out:
            self._esp_status_timed_out = True
            self._append_debug_log("[ESP32] 状态超时，ESP 标记为离线")
        self._refresh_esp_status_labels()

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
          - Transport 选择器保持启用，允许用户手动切换 MQTT/USB 逻辑。
            切换时会先断开当前通道。

        参数：
          connected: True 表示任一 transport 已连接。
        """
        self.connect_button.setEnabled(not connected)
        self.disconnect_button.setEnabled(connected)
        self.time_sync_button.setEnabled(connected)
        self.transport_combo.setEnabled(True)

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
