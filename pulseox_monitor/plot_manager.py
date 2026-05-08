# 集中管理四路时序曲线的创建与刷新。

from __future__ import annotations

from typing import Any, cast

import pyqtgraph as pg
from PySide6.QtWidgets import QWidget


# 负责创建并刷新 IR、Red、BPM、SpO2 四张曲线图。
class PlotManager:
    def __init__(self) -> None:
        # 初始化绘图控件并建立所有曲线对象。
        pg.setConfigOptions(antialias=True)
        self.widget = pg.GraphicsLayoutWidget()
        self._curves: dict[str, pg.PlotDataItem] = {}
        self._build_plots()

    def _build_plots(self) -> None:
        # 按固定布局创建四张上下对齐的时序图。
        definitions = [
            ("ir", "IR", "#00b894"),
            ("red", "Red", "#d63031"),
            ("bpm", "BPM", "#0984e3"),
            ("spo2", "SpO2", "#6c5ce7"),
        ]

        first_plot: pg.PlotItem | None = None
        last_plot: pg.PlotItem | None = None
        for row, (key, title, color) in enumerate(definitions):
            axis_items = {"bottom": pg.DateAxisItem()}
            plot = cast(
                pg.PlotItem,
                cast(Any, self.widget).addPlot(row=row, col=0, axisItems=axis_items),
            )
            plot.setTitle(title)
            plot.setLabel("left", title)
            plot.showGrid(x=True, y=True, alpha=0.25)
            curve = plot.plot(
                pen=pg.mkPen(color=color, width=2),
                connect="finite",
            )
            self._curves[key] = curve

            # 所有子图共用同一条 X 轴，这样缩放和拖动会联动。
            if first_plot is None:
                first_plot = plot
            else:
                cast(Any, plot).setXLink(first_plot)

            if row < len(definitions) - 1:
                plot.hideAxis("bottom")
            last_plot = plot

        if last_plot is not None:
            last_plot.setLabel("bottom", "Time")

    def update(self, series: dict[str, tuple[list[float], list[float]]]) -> None:
        # 用最新序列刷新所有曲线。
        for key, curve in self._curves.items():
            x_values, y_values = series.get(key, ([], []))
            curve.setData(x=x_values, y=y_values)

    def clear(self) -> None:
        # 清空全部曲线数据。
        for curve in self._curves.values():
            curve.setData([], [])

    def as_widget(self) -> QWidget:
        # 返回可直接嵌入 Qt 布局的绘图控件。
        return self.widget
