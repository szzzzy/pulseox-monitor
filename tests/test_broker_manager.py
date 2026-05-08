# 验证 broker 管理辅助逻辑的本地判定与路径解析。

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from pulseox_monitor.broker_manager import (
    is_local_broker_host,
    parse_broker_endpoint,
    resolve_existing_file,
    resolve_mosquitto_executable,
)


# 验证 broker 管理模块中的基础辅助逻辑。
class BrokerManagerHelperTests(unittest.TestCase):
    def test_is_local_broker_host(self) -> None:
        # 回环地址和 localhost 应被识别为本机 broker。
        self.assertTrue(is_local_broker_host("127.0.0.1"))
        self.assertTrue(is_local_broker_host("localhost"))
        self.assertTrue(is_local_broker_host("::1"))
        self.assertFalse(is_local_broker_host("192.168.1.10"))

    def test_is_local_broker_host_accepts_local_hotspot_ip(self) -> None:
        # 本机热点 IP 也应被视为本地 broker 地址。
        with patch(
            "pulseox_monitor.broker_manager._resolve_host_addresses",
            return_value={"172.20.10.4"},
        ):
            with patch(
                "pulseox_monitor.broker_manager.get_local_ip_addresses",
                return_value={"127.0.0.1", "172.20.10.4"},
            ):
                self.assertTrue(is_local_broker_host("172.20.10.4"))

    def test_resolve_mosquitto_executable_with_existing_file(self) -> None:
        # 真实存在的可执行文件路径应直接被解析成绝对路径。
        fake_path = r"D:\tools\mosquitto\mosquitto.exe"
        with patch("pulseox_monitor.broker_manager.Path.exists", return_value=True):
            resolved = resolve_mosquitto_executable(fake_path)
        self.assertEqual(resolved, str(Path(fake_path).resolve()))

    def test_resolve_mosquitto_executable_uses_path_lookup(self) -> None:
        # 命令名输入应允许通过 PATH 查找可执行文件。
        with patch("pulseox_monitor.broker_manager.shutil.which", return_value=r"C:\bin\mosquitto.exe"):
            resolved = resolve_mosquitto_executable("mosquitto")
        self.assertEqual(resolved, r"C:\bin\mosquitto.exe")

    def test_resolve_existing_file_requires_real_path(self) -> None:
        # 配置文件解析只接受真实存在的文件路径。
        fake_path = r"D:\MOSQUITTO\my_mosquitto.conf"
        with patch("pulseox_monitor.broker_manager.Path.exists", return_value=True):
            resolved = resolve_existing_file(fake_path)
        self.assertEqual(resolved, str(Path(fake_path).resolve()))

    def test_parse_broker_endpoint_accepts_plain_host(self) -> None:
        # 纯 host 输入应沿用默认端口。
        host, port = parse_broker_endpoint("172.20.10.4", default_port=1883)
        self.assertEqual((host, port), ("172.20.10.4", 1883))

    def test_parse_broker_endpoint_accepts_full_mqtt_uri(self) -> None:
        # 完整 mqtt:// URI 应被拆解成 host 和 port。
        host, port = parse_broker_endpoint("mqtt://172.20.10.4:1884", default_port=1883)
        self.assertEqual((host, port), ("172.20.10.4", 1884))

    def test_parse_broker_endpoint_rejects_non_mqtt_scheme(self) -> None:
        # 非 mqtt 协议应直接报错，避免误连到不支持的地址格式。
        with self.assertRaises(ValueError):
            parse_broker_endpoint("tcp://172.20.10.4", default_port=1883)


if __name__ == "__main__":
    unittest.main()
