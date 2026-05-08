# 管理本地 Mosquitto 进程及其可达性判断。

from __future__ import annotations

import ipaddress
import shutil
import socket
import time
from pathlib import Path
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, QProcess, Signal


def _normalize_host_text(host: str) -> str:
    # 规范化 host 文本，去掉空白和 IPv6 zone id。
    normalized = host.strip().lower()
    if "%" in normalized:
        normalized = normalized.split("%", 1)[0]
    return normalized


def _resolve_host_addresses(host: str) -> set[str]:
    # 把 host 解析成一组 IP 地址。
    normalized = _normalize_host_text(host)
    if not normalized:
        return set()

    try:
        return {str(ipaddress.ip_address(normalized))}
    except ValueError:
        pass

    addresses: set[str] = set()
    try:
        infos = socket.getaddrinfo(normalized, None)
    except OSError:
        return addresses

    for info in infos:
        sockaddr = info[4]
        if sockaddr:
            addresses.add(_normalize_host_text(str(sockaddr[0])))
    return addresses


def get_local_ip_addresses() -> set[str]:
    # 收集当前主机可视为“本机”的 IP 地址集合。
    addresses = {"127.0.0.1", "::1"}

    for name in {socket.gethostname(), socket.getfqdn(), "localhost"}:
        addresses.update(_resolve_host_addresses(name))

    # 通过 UDP 选路结果补充当前主要网卡的实际出站地址。
    probe_targets: list[tuple[int, str, int]] = [
        (socket.AF_INET, "8.8.8.8", 80),
        (socket.AF_INET, "1.1.1.1", 80),
        (socket.AF_INET6, "2001:4860:4860::8888", 80),
    ]
    for family, target, port in probe_targets:
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as probe_socket:
                probe_socket.connect((target, port))
                local_ip = probe_socket.getsockname()[0]
                addresses.add(_normalize_host_text(str(local_ip)))
        except OSError:
            continue

    return {address for address in addresses if address}


def is_local_broker_host(host: str) -> bool:
    # 判断给定 host 是否指向当前这台机器。
    normalized = _normalize_host_text(host)
    if normalized in {"127.0.0.1", "localhost", "::1"}:
        return True

    host_addresses = _resolve_host_addresses(normalized)
    if not host_addresses:
        return False

    return bool(host_addresses & get_local_ip_addresses())


def parse_broker_endpoint(host_text: str, default_port: int) -> tuple[str, int]:
    # 兼容纯 host、host:port 以及 mqtt://host[:port] 三种输入形式。
    normalized = host_text.strip()
    if not normalized:
        return "", default_port

    candidate = normalized if "://" in normalized else f"mqtt://{normalized}"
    parsed = urlsplit(candidate)
    if parsed.scheme and parsed.scheme != "mqtt":
        raise ValueError("Broker 地址仅支持 mqtt:// 协议。")
    if parsed.username or parsed.password:
        raise ValueError("Broker 地址中不支持用户名或密码。")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("Broker 地址中不支持路径、查询参数或片段。")

    host = parsed.hostname
    if not host:
        raise ValueError("Broker host 不能为空。")

    try:
        port = parsed.port or default_port
    except ValueError as exc:
        raise ValueError("Broker 端口格式不正确。") from exc

    return _normalize_host_text(host), port


def resolve_mosquitto_executable(path_hint: str) -> str:
    # 把输入的路径或命令名解析成可执行文件绝对路径。
    hint = path_hint.strip()
    if not hint:
        return ""

    candidate = Path(hint).expanduser()
    if candidate.exists():
        return str(candidate.resolve())

    found = shutil.which(hint)
    return found or ""


def resolve_existing_file(path_hint: str) -> str:
    # 把输入的文件路径解析成真实存在的绝对路径。
    hint = path_hint.strip()
    if not hint:
        return ""

    candidate = Path(hint).expanduser()
    if candidate.exists():
        return str(candidate.resolve())
    return ""


def broker_port_is_open(host: str, port: int, timeout: float = 0.3) -> bool:
    # 检测目标 host:port 是否已有进程在监听。
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# 负责按需启动、停止和观察本地 Mosquitto 进程。
class BrokerManager(QObject):
    status_changed = Signal(str)
    running_changed = Signal(bool)
    debug_message = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        # 创建 broker 进程管理器。
        super().__init__(parent)
        self._process = QProcess(self)
        self._started_by_app = False

        self._process.started.connect(self._on_process_started)
        self._process.finished.connect(self._on_process_finished)
        self._process.errorOccurred.connect(self._on_process_error)
        self._process.readyReadStandardOutput.connect(self._drain_stdout)
        self._process.readyReadStandardError.connect(self._drain_stderr)

    @property
    def is_running(self) -> bool:
        # 返回 broker 子进程当前是否仍在运行。
        return self._process.state() != QProcess.ProcessState.NotRunning

    @property
    def started_by_app(self) -> bool:
        # 返回当前 broker 是否由本应用启动。
        return self._started_by_app

    def ensure_broker_running(
        self,
        executable_hint: str,
        config_hint: str,
        host: str,
        port: int,
    ) -> bool:
        # 确保目标 broker 已处于可连接状态。
        if broker_port_is_open(host, port):
            self.status_changed.emit("Broker 已就绪")
            self.running_changed.emit(True)
            return True

        if not is_local_broker_host(host):
            self.status_changed.emit("远程 Broker 需自行启动")
            self.running_changed.emit(False)
            return False

        started = self.start_local_broker(
            executable_hint=executable_hint,
            config_hint=config_hint,
            host=host,
            port=port,
        )
        if not started:
            return False

        return self.wait_until_broker_ready(host=host, port=port, timeout_s=4.0)

    def start_local_broker(
        self,
        executable_hint: str,
        config_hint: str,
        host: str,
        port: int,
    ) -> bool:
        # 启动本地 Mosquitto 进程。
        if broker_port_is_open(host, port):
            self.status_changed.emit("Broker 已在本地运行")
            self.running_changed.emit(True)
            return True

        if self.is_running:
            self.status_changed.emit("Broker 启动中")
            return True

        executable_path = resolve_mosquitto_executable(executable_hint)
        if not executable_path:
            self.status_changed.emit("未找到 mosquitto.exe")
            self.debug_message.emit("[BROKER] 未配置 mosquitto 可执行文件路径")
            self.running_changed.emit(False)
            return False

        config_path = resolve_existing_file(config_hint)
        if config_hint.strip() and not config_path:
            self.status_changed.emit("配置文件不存在")
            self.debug_message.emit(f"[BROKER] 未找到配置文件: {config_hint}")
            self.running_changed.emit(False)
            return False

        self._started_by_app = True
        self._process.setWorkingDirectory(str(Path(executable_path).parent))
        self._process.setProgram(executable_path)

        # 优先按用户提供的配置文件启动，仅在没有配置文件时回退到端口模式。
        if config_path:
            arguments = ["-c", config_path, "-v"]
        else:
            arguments = ["-p", str(port), "-v"]

        self._process.setArguments(arguments)
        self.status_changed.emit(f"正在启动本地 Broker ({Path(executable_path).name}) ...")
        self.debug_message.emit(f"[BROKER] 启动命令: {executable_path} {' '.join(arguments)}")
        self._process.start()

        if not self._process.waitForStarted(3000):
            self._started_by_app = False
            self.status_changed.emit("Broker 启动失败")
            self.running_changed.emit(False)
            self.debug_message.emit("[BROKER] mosquitto 进程未能成功启动")
            return False

        return True

    def wait_until_broker_ready(self, host: str, port: int, timeout_s: float) -> bool:
        # 等待 broker 端口真正开始监听。
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if broker_port_is_open(host, port):
                self.status_changed.emit("Broker 已就绪")
                self.running_changed.emit(True)
                return True
            time.sleep(0.1)

        self.status_changed.emit("Broker 未就绪")
        self.running_changed.emit(False)
        self.debug_message.emit("[BROKER] mosquitto 进程已启动，但监听端口未就绪")
        return False

    def stop_local_broker(self) -> None:
        # 停止由本应用启动的 broker 子进程。
        if not self.is_running:
            self.status_changed.emit("Broker 未运行")
            self.running_changed.emit(False)
            return

        if not self._started_by_app:
            self.status_changed.emit("外部 Broker 运行中")
            self.debug_message.emit("[BROKER] 当前 Broker 并非由本应用启动，未执行停止")
            return

        self.status_changed.emit("正在停止本地 Broker ...")
        self._process.terminate()
        if not self._process.waitForFinished(3000):
            self._process.kill()
            self._process.waitForFinished(1000)

    def _on_process_started(self) -> None:
        # 处理 broker 进程真正启动后的状态变化。
        self.running_changed.emit(True)
        self.debug_message.emit("[BROKER] mosquitto 进程已启动")

    def _on_process_finished(self, exit_code: int, exit_status: QProcess.ExitStatus) -> None:
        # 处理 broker 子进程退出事件。
        status_name = "正常退出" if exit_status == QProcess.ExitStatus.NormalExit else "异常退出"
        self.debug_message.emit(
            f"[BROKER] mosquitto 已退出: code={exit_code}, status={status_name}"
        )
        self.status_changed.emit("Broker 已停止")
        self.running_changed.emit(False)
        self._started_by_app = False

    def _on_process_error(self, error: QProcess.ProcessError) -> None:
        # 处理 broker 子进程启动或运行错误。
        self.status_changed.emit("Broker 进程错误")
        self.running_changed.emit(False)
        self._started_by_app = False
        self.debug_message.emit(f"[BROKER] 进程错误: {error}")

    def _drain_stdout(self) -> None:
        # 读取 Mosquitto 标准输出并转发到调试面板。
        output = bytes(self._process.readAllStandardOutput().data()).decode(
            "utf-8", errors="replace"
        ).strip()
        if output:
            self.debug_message.emit(f"[BROKER][OUT] {output}")

    def _drain_stderr(self) -> None:
        # 读取 Mosquitto 标准错误并转发到调试面板。
        output = bytes(self._process.readAllStandardError().data()).decode(
            "utf-8", errors="replace"
        ).strip()
        if output:
            self.debug_message.emit(f"[BROKER][ERR] {output}")
