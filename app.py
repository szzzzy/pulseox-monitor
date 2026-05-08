# 桌面监护程序的 Qt 启动入口。

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from pulseox_monitor.main_window import MainWindow


def main() -> int:
    # 创建 Qt 应用并显示主窗口。
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
