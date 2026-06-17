# Pulse Oximeter MQTT Monitor

基于 `Python + PySide6 + pyqtgraph + paho-mqtt` 的桌面监护工具，用于通过 MQTT 接收脉搏血氧设备数据、绘制实时曲线、管理本地 Mosquitto Broker，并下发 RTC 时间同步命令。

## 功能概览

- PC 端仅通过 MQTT 与设备桥接，不包含任何串口逻辑。
- 上行主题固定为 `pulseox/data`，下行主题固定为 `pulseox/cmd`。
- 使用 `MessageDispatcher` 按 JSON 字段 `message` 路由为三类类型化消息：`measurement`、`rtc_set_ack`、`parse_error`。
- 只有 `measurement` 会进入 `DataManager` 的环形缓存，并驱动 IR / Red / BPM / SpO2 四条曲线。
- 设备时间会在内部解析为 `datetime`；若 `rtc_valid=false`，则保留样本但绘图回退到 PC 接收时间，同时在状态面板中标明 RTC 无效。
- “时间同步”按钮会向 `pulseox/cmd` 发布纯文本命令：`SETTIME YYYY-MM-DD HH:MM:SS`。
- `rtc_set_ack` 更新状态面板，`parse_error` 进入调试日志面板。
- `MQTTHandler` 提供自动重连和错误日志，便于在 Broker 重启或网络短暂波动时恢复。
- `BrokerManager` 支持检测本地 Broker、按配置启动 Mosquitto，并在应用退出时清理由本应用拉起的进程。

## 项目结构

```text
app.py
launch_monitor.cmd
create_desktop_shortcut.ps1
requirements.txt
pyrightconfig.json
pulseox_monitor/
  broker_manager.py
  data_manager.py
  dispatcher.py
  main_window.py
  models.py
  mqtt_handler.py
  plot_manager.py
tests/
  test_broker_manager.py
  test_data_manager.py
  test_dispatcher.py
```

## 安装

建议使用本地虚拟环境隔离依赖：

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

## 运行

```bash
.venv\Scripts\python app.py
```

Windows 下也可以直接运行启动脚本：

```bat
launch_monitor.cmd
```

如需创建桌面快捷方式：

```powershell
powershell -ExecutionPolicy Bypass -File .\create_desktop_shortcut.ps1
```

## Broker 配置

界面默认使用以下本地 Mosquitto 路径，可在界面中手动修改：

```text
D:\MOSQUITTO\mosquitto.exe
D:\MOSQUITTO\my_mosquitto.conf
```

当连接目标是本机地址且启用自动启动选项时，应用会优先检测端口是否已有 Broker 监听；如果没有，会尝试按界面中的 Mosquitto 路径和配置文件启动本地 Broker。

## MQTT 协议

- 上行主题：`pulseox/data`
- 下行主题：`pulseox/cmd`
- 支持的上行 `message` 类型：`measurement`、`rtc_set_ack`、`parse_error`
- RTC 同步命令格式：`SETTIME YYYY-MM-DD HH:MM:SS`

## 测试

```bash
.venv\Scripts\python -m unittest discover -s tests -v
```
