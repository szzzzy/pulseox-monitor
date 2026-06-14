# =============================================================================
# USBHandler —— USB 串口传输处理器。
#
# 职责：
#   1. 枚举系统可用 COM 口。
#   2. 连接/断开指定串口（默认波特率 115200）。
#   3. 在后台线程中按行读取串口数据。
#   4. 过滤非 JSON 日志行（ESP_LOG 等噪声），仅将有效数据行上抛。
#   5. 通过信号将接收行、连接状态、调试消息传递给 UI 层。
#   6. 支持向串口发送文本命令（如 GUI_USB_START / SETTIME / GUI_USB_PING / GUI_USB_STOP）。
#   7. USB 生命周期管理：
#      - 连接成功后自动发送 GUI_USB_START。
#      - 周期性（默认 5 秒）发送 GUI_USB_PING 保活。
#      - 断开时发送 GUI_USB_STOP。
#
# 线程模型：
#   主线程 —— 创建 USBHandler、发送命令、处理信号。
#   后台 QThread —— 阻塞读取串口行，通过信号发射到主线程。
#   pyserial 的 write() 在主线程调用（由 QTimer 或按钮点击触发）。
# =============================================================================

from __future__ import annotations

from typing import ClassVar

from PySide6.QtCore import QMutex, QObject, QThread, QTimer, Signal

from .usb_protocol import (
    CMD_USB_PING,
    CMD_USB_START,
    CMD_USB_STOP,
    DEFAULT_BAUDRATE,
    DEFAULT_PING_INTERVAL_MS,
    is_data_line,
)


# =============================================================================
# SerialReader —— 后台线程，阻塞读取串口
# =============================================================================

class SerialReader(QObject):
    """后台串口读取工作器。

    在独立 QThread 中运行，阻塞调用 serial.readline()，
    每读到一行就通过 line_read 信号发射到主线程。
    """

    line_read = Signal(str)       # 发射原始行（不含换行符）
    read_error = Signal(str)      # 发射读取错误描述

    def __init__(self, ser, parent: QObject | None = None) -> None:
        """初始化读取器。

        参数：
          ser:    pyserial Serial 实例（已打开）。
          parent: 父 QObject。
        """
        super().__init__(parent)
        self._ser = ser
        self._running = False

    def run(self) -> None:
        """后台线程主循环：持续读取行直到 stop() 被调用。"""
        self._running = True
        while self._running:
            try:
                # readline() 阻塞直到收到换行符或超时
                raw = self._ser.readline()
            except Exception as exc:
                if self._running:
                    self.read_error.emit(str(exc))
                break

            if not raw:
                # 超时返回空 bytes，继续循环
                continue

            try:
                line = raw.decode("utf-8", errors="replace").strip()
            except Exception:
                continue

            if line:
                self.line_read.emit(line)

    def stop(self) -> None:
        """通知后台线程停止运行。"""
        self._running = False


# =============================================================================
# USBHandler —— USB 串口传输处理器（主线程接口）
# =============================================================================

class USBHandler(QObject):
    """USB 串口传输处理器。

    信号：
      line_received(str):         一条经过滤的有效数据行（JSON 或 CSV）。
      connection_state_changed(str): 连接状态描述文本。
      connected_changed(bool):     连接状态变更（True=已连接）。
      debug_message(str):         调试/信息日志。

    使用方式：
      handler = USBHandler()
      handler.line_received.connect(on_data_line)
      ports = handler.available_ports()
      handler.connect_to_port("COM3")
      ...
      handler.disconnect()
    """

    line_received = Signal(str)
    connection_state_changed = Signal(str)
    connected_changed = Signal(bool)
    debug_message = Signal(str)

    # pyserial 超时（秒），readline 超时后返回空 bytes
    READ_TIMEOUT: ClassVar[float] = 0.5

    def __init__(self, parent: QObject | None = None) -> None:
        """初始化 USB 处理器。

        参数：
          parent: 父 QObject。
        """
        super().__init__(parent)
        self._ser = None                    # pyserial Serial 实例
        self._reader: SerialReader | None = None   # 后台读取器
        self._thread: QThread | None = None         # 后台线程
        self._port_name = ""                # 当前连接的端口名
        self._baudrate = DEFAULT_BAUDRATE   # 当前波特率
        self._connected = False

        # 周期性 ping 定时器
        self._ping_timer = QTimer(self)
        self._ping_timer.timeout.connect(self._send_ping)
        self._ping_interval_ms = DEFAULT_PING_INTERVAL_MS

        # 互斥锁保护串口写操作
        self._write_mutex = QMutex()

    # =========================================================================
    # COM 口枚举
    # =========================================================================

    @staticmethod
    def available_ports() -> list[str]:
        """枚举系统当前可用的 COM 口列表。

        返回：
          端口名称列表，按名称排序，如 ["COM3", "COM5", "COM7"]。
          若 pyserial 未安装或无可用端口，返回空列表。
        """
        try:
            import serial.tools.list_ports as list_ports
            ports = [p.device for p in list_ports.comports()]
            ports.sort()
            return ports
        except ImportError:
            return []

    # =========================================================================
    # 连接 / 断开
    # =========================================================================

    def connect_to_port(self, port: str, baudrate: int = DEFAULT_BAUDRATE) -> None:
        """连接到指定串口。

        连接成功后：
          1. 发射 connected_changed(True) 信号。
          2. 发送 GUI_USB_START 命令。
          3. 启动周期性 ping 定时器。
          4. 启动后台读取线程。

        若连接失败：
          发射 debug_message 描述错误，不改变连接状态。

        参数：
          port:     COM 口名称（如 "COM3"）。
          baudrate: 波特率（默认 115200）。
        """
        if self._connected:
            self.debug_message.emit("[USB] 已连接，先断开再重连")
            return

        try:
            import serial
        except ImportError:
            self.debug_message.emit("[USB] pyserial 未安装，无法连接串口")
            return

        try:
            ser = serial.Serial(
                port=port,
                baudrate=baudrate,
                timeout=self.READ_TIMEOUT,
            )
        except Exception as exc:
            self.debug_message.emit(f"[USB] 打开串口失败 {port}: {exc}")
            return

        self._ser = ser
        self._port_name = port
        self._baudrate = baudrate
        self._connected = True

        # 启动后台读取线程
        self._reader = SerialReader(ser)
        self._thread = QThread(self)
        self._reader.moveToThread(self._thread)
        self._thread.started.connect(self._reader.run)
        self._reader.line_read.connect(self._on_line_read)
        self._reader.read_error.connect(self._on_read_error)
        self._thread.start()

        # 发送启动命令
        self.send_command(CMD_USB_START)

        # 启动周期性 ping
        self._ping_timer.start(self._ping_interval_ms)

        self._port_name = port
        self._baudrate = baudrate

        status = f"USB 已连接 {port} @ {baudrate}"
        self.connection_state_changed.emit(status)
        self.connected_changed.emit(True)
        self.debug_message.emit(f"[USB] {status}")

    def disconnect(self) -> None:
        """断开当前串口连接。

        断开前执行：
          1. 发送 GUI_USB_STOP 命令。
          2. 停止 ping 定时器。
          3. 停止后台读取线程。
          4. 关闭串口。

        若当前未连接，此方法为空操作。
        """
        if not self._connected:
            return

        # 发送停止命令（在关闭串口前尽可能发出）
        self.send_command(CMD_USB_STOP)

        # 停止 ping
        self._ping_timer.stop()

        # 停止后台线程
        if self._reader:
            self._reader.stop()
        if self._thread:
            self._thread.quit()
            self._thread.wait(2000)  # 最多等待 2 秒

        self._reader = None
        self._thread = None

        # 关闭串口
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass
        self._ser = None

        self._connected = False
        status = f"USB 已断开 {self._port_name}"
        self.connection_state_changed.emit(status)
        self.connected_changed.emit(False)
        self.debug_message.emit(f"[USB] {status}")
        self._port_name = ""

    # =========================================================================
    # 命令发送
    # =========================================================================

    def send_command(self, text: str) -> bool:
        """向串口发送文本命令。

        自动追加换行符 \\n。
        使用互斥锁保护写操作（防止主线程和 ping 定时器同时写入）。

        参数：
          text: 要发送的命令文本（不含换行符）。

        返回：
          True 表示发送成功，False 表示未连接或发送失败。
        """
        if not self._connected or self._ser is None:
            return False

        self._write_mutex.lock()
        try:
            raw = (text + "\n").encode("utf-8")
            self._ser.write(raw)
            self._ser.flush()
            return True
        except Exception as exc:
            self.debug_message.emit(f"[USB] 发送命令失败: {exc}")
            return False
        finally:
            self._write_mutex.unlock()

    # =========================================================================
    # 查询
    # =========================================================================

    def is_connected(self) -> bool:
        """返回当前是否已连接到串口。"""
        return self._connected

    def port_name(self) -> str:
        """返回当前连接的端口名，未连接时为空字符串。"""
        return self._port_name

    def baudrate(self) -> int:
        """返回当前连接的波特率。"""
        return self._baudrate

    # =========================================================================
    # 内部槽函数
    # =========================================================================

    def _on_line_read(self, line: str) -> None:
        """后台线程发射的行数据到达主线程时的处理槽函数。

        对行进行过滤：
          - 有效数据行（JSON/CSV）→ 通过 line_received 信号上抛。
          - 噪声行（ESP_LOG 等）→ 静默丢弃。

        参数：
          line: 从串口读取的单行文本（已 strip）。
        """
        if is_data_line(line):
            self.line_received.emit(line)

    def _on_read_error(self, error_text: str) -> None:
        """后台线程读取错误时的处理槽函数。

        参数：
          error_text: 错误描述文本。
        """
        self.debug_message.emit(f"[USB] 读取错误: {error_text}")

    def _send_ping(self) -> None:
        """周期性 ping 定时器的槽函数。

        发送 GUI_USB_PING 命令以保持 USB 连接活跃。
        """
        self.send_command(CMD_USB_PING)
