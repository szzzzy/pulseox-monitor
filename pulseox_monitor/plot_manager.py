# =============================================================================
# 多 Tab 图窗管理系统 —— Overview 卡片 + 6 个趋势图窗 + 诊断页。
#
# 模块结构：
#   CurveDef         —— 单条曲线的配置定义（字段路径、颜色、线宽等）。
#   SubplotDef       —— 单个子图的配置定义（标题、Y轴标签、曲线列表等）。
#   PlotGroup        —— 一组 X 轴联动的子图，负责从 DataManager 拉取数据并渲染。
#   StatusCard       —— Overview 页面的单个指标卡片控件。
#   BaseTab          —— 所有 Tab 页的抽象基类。
#   OverviewTab      —— 总览页：核心生命体征卡片 + 连接/解析状态。
#   VitalsTrendsTab  —— 生命体征曲线：BPM、RR、SpO2、IBI。
#   SignalQualityTab —— 信号质量曲线：SQ、Motion、PI、R Ratio。
#   PPGRawTab        —— 原始 PPG 曲线：Red、IR、AC RMS、Span/Delta。
#   HRVTab           —— HRV 曲线：时域、庞加莱图、频域、LF/HF。
#   ECGIPTTTab       —— ECG/PTT 曲线：ECG 波形、ECG HR、ECG RR、PTT。
#   DiagnosticsTab   —— 诊断页：传感器表 + 系统表 + 额外字段 + 解析警告 + 原始帧 + 原始 JSON。
#   TabPlotManager   —— 顶层 Tab 容器，管理时间窗口、暂停/恢复、清除。
#
# 数据流：
#   DataManager.add_message() → data_received 信号 → TabPlotManager.update_all()
#                                                      └── 遍历所有 Tab.refresh()
#                                                           └── 各自从 DataManager 读取最新数据
# =============================================================================

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .data_manager import DataManager
from .models import FlexibleMessage

# =============================================================================
# CurveDef —— 单条曲线的配置定义
# =============================================================================


@dataclass
class CurveDef:
    """单条绘图曲线的完整配置。

    属性：
      field_path:   DataManager 中存储该数据的字段路径（如 "bpm"、"ecg_hr"）。
      label:        图例和 tooltip 中显示的曲线名称。
      color:        曲线颜色（十六进制字符串，如 "#0984e3"）。
      width:        线宽（像素），默认 2。
      dashed:       是否为虚线，默认 False。
      valid_check:  可选的有效性检查字段路径（如 "bpm_valid"）。
                    无效点用灰色虚线和浅色绘制。
      scale:        显示缩放因子，原始值乘以此系数后显示。默认 1.0。
      unit:         显示单位（如 "bpm"、"ms"），用于 tooltip。
    """
    field_path: str
    label: str
    color: str
    width: int = 2
    dashed: bool = False
    valid_check: str | None = None
    scale: float = 1.0
    unit: str = ""


# =============================================================================
# SubplotDef —— 单个子图的配置定义
# =============================================================================

@dataclass
class SubplotDef:
    """单个子图的完整配置。

    属性：
      title:            子图标题（显示在子图左侧）。
      y_label:          Y 轴标签。
      curves:           此子图中的曲线列表。
      y_range:          可选的 Y 轴范围 (min, max)。
      motion_background: 是否叠加运动伪影橙色背景带。
      finger_background: 是否叠加手指离位灰色背景带。
      log_scale:        是否使用对数 Y 轴。
    """
    title: str
    y_label: str
    curves: list[CurveDef] = field(default_factory=list)
    y_range: tuple[float, float] | None = None
    motion_background: bool = False
    finger_background: bool = False
    log_scale: bool = False


# =============================================================================
# PlotGroup —— 一组 X 轴联动的子图
# =============================================================================

class PlotGroup:
    """一组 X 轴联动的时间序列子图。

    特性：
      - 第一个子图作为 X 轴主机，其余子图的 X 轴与第一个联动。
      - 自动处理无效数据点（灰显虚线）。
      - 支持运动伪影/手指离位背景带叠加。
      - 跨子图 crosshair（垂直线 + 数值 tooltip）。
    """

    def __init__(
        self,
        data_manager: DataManager,
        subplots: list[SubplotDef],
        show_x_label: bool = True,
    ) -> None:
        """初始化绘图组。

        参数：
          data_manager: 数据管理器，提供 series() 方法获取曲线数据。
          subplots:     子图配置列表，从上到下排列。
          show_x_label: 是否在最后一个子图底部显示 X 轴标签 "Time"。
        """
        self._data_manager = data_manager
        self._subplot_defs = subplots

        # 内部状态
        self._plots: list[pg.PlotItem] = []            # 所有子图对象
        self._curve_items: dict[str, pg.PlotDataItem] = {}   # 曲线对象 (key → curve)
        self._curve_defs: dict[str, CurveDef] = {}           # 曲线定义 (key → def)
        self._motion_regions: list[pg.LinearRegionItem] = []  # 运动伪影叠加区域
        self._finger_regions: list[pg.LinearRegionItem] = []  # 手指离位叠加区域
        self._crosshair_vline: pg.InfiniteLine | None = None   # crosshair 垂直线
        self._tooltip_label: pg.TextItem | None = None          # crosshair tooltip 文本
        self._active_valid_checks: dict[str, str] = {}         # 活跃的有效性检查

        # ---- 创建 GraphicsLayoutWidget 作为画布 ----
        self.widget = pg.GraphicsLayoutWidget()
        self.widget.setBackground("k")

        # ---- 逐行创建子图 ----
        first_plot: pg.PlotItem | None = None
        for row, sub_def in enumerate(subplots):
            # X 轴使用 DateAxisItem（时间戳自动格式化为时间）
            axis_items: dict[str, Any] = {"bottom": pg.DateAxisItem()}
            plot: pg.PlotItem = self.widget.addPlot(
                row=row, col=0, axisItems=axis_items
            )
            # 子图外观配置
            plot.setTitle(sub_def.title, color="w", size="11pt")
            plot.setLabel("left", sub_def.y_label, color="w")
            plot.showGrid(x=True, y=True, alpha=0.2)
            if sub_def.y_range:
                plot.setYRange(*sub_def.y_range, padding=0.05)
            if sub_def.log_scale:
                plot.setLogMode(y=True)

            # 仅允许 X 轴鼠标交互（平移/缩放），Y 轴始终自适应
            plot.setMouseEnabled(x=True, y=False)

            # X 轴联动：第一个子图为主机，后续链接到第一个
            if first_plot is None:
                first_plot = plot
            else:
                plot.setXLink(first_plot)

            # 非最后一个子图（且需要显示 X 标签时）隐藏底部 X 轴
            if row < len(subplots) - 1 or not show_x_label:
                plot.hideAxis("bottom")

            # ---- 在此子图中创建曲线 ----
            for cdef in sub_def.curves:
                pen = pg.mkPen(color=cdef.color, width=cdef.width)
                if cdef.dashed:
                    pen.setStyle(Qt.PenStyle.DashLine)
                # connect="finite" 表示只在有限（非 NaN）点之间连线
                curve = plot.plot(pen=pen, connect="finite", name=cdef.label)
                # 用 "子图标题/字段路径" 作为曲线唯一键
                key = f"{sub_def.title}/{cdef.field_path}"
                self._curve_items[key] = curve
                self._curve_defs[key] = cdef
                if cdef.valid_check:
                    self._active_valid_checks[key] = cdef.valid_check

            self._plots.append(plot)

        # 在最后一个子图底部显示 X 轴标签
        if show_x_label and self._plots:
            self._plots[-1].setLabel("bottom", "Time")

        # ---- 安装跨子图 crosshair ----
        self._install_crosshair()

    @property
    def first_plot(self) -> pg.PlotItem | None:
        """返回第一个子图对象（用于外部访问坐标范围等）。"""
        return self._plots[0] if self._plots else None

    # -------------------------------------------------------------------------
    # 数据刷新
    # -------------------------------------------------------------------------

    def refresh(self) -> None:
        """从 DataManager 拉取最新序列数据并刷新所有曲线。

        遍历所有注册的曲线，调用 data_manager.series() 提取 (x, y) 数据，
        设置到对应的 PlotDataItem，然后更新曲线样式和背景带。
        """
        # 更新每条曲线的数据
        for key, curve in self._curve_items.items():
            cdef = self._curve_defs[key]
            x, y, valid_mask, rx = self._data_manager.series(
                cdef.field_path,
                valid_check_path=cdef.valid_check,
            )
            # 应用显示缩放因子
            y_scaled = [v * cdef.scale if not math.isnan(v) else v for v in y]
            curve.setData(x=x, y=y_scaled)
            # 根据有效性更新曲线样式
            self._update_curve_style(key, x, y, valid_mask)

        # 更新背景带（运动伪影、手指离位）
        self._update_background_bands()

    def _update_curve_style(
        self, key: str, x: list[float], y: list[float], valid: list[bool]
    ) -> None:
        """根据数据有效性更新曲线的画笔样式。

        全部无效 → 灰色虚线（让用户视觉上感知数据质量下降）。
        全部有效 → 恢复原始颜色和线宽。
        """
        curve = self._curve_items.get(key)
        if curve is None:
            return
        cdef = self._curve_defs.get(key)
        if cdef is None:
            return

        any_invalid = not all(valid) if valid else False
        if any_invalid:
            pen = pg.mkPen(color=(150, 150, 150), width=1)
            pen.setStyle(Qt.PenStyle.DashLine)
            curve.setPen(pen)
        else:
            pen = pg.mkPen(color=cdef.color, width=cdef.width)
            if cdef.dashed:
                pen.setStyle(Qt.PenStyle.DashLine)
            curve.setPen(pen)

    def _update_background_bands(self) -> None:
        """更新运动伪影和手指离位的背景叠加带。

        运动伪影 (motion_artifact=True) → 橙色半透明背景。
        手指离位 (finger=False) → 灰色半透明背景。

        遍历历史消息，检测状态变化点，构造连续区间并用 LinearRegionItem 绘制。
        """
        # 清除旧的叠加区域
        for r in self._motion_regions + self._finger_regions:
            for plot in self._plots:
                plot.removeItem(r)
        self._motion_regions.clear()
        self._finger_regions.clear()

        # 检查是否有子图需要运动/手指背景
        has_motion = any(s.motion_background for s in self._subplot_defs)
        has_finger = any(s.finger_background for s in self._subplot_defs)

        if not has_motion and not has_finger:
            return

        messages = self._data_manager.messages()
        if not messages:
            return

        # ---- 计算运动伪影区间 ----
        motion_intervals: list[tuple[float, float]] = []
        finger_off_intervals: list[tuple[float, float]] = []

        in_motion = False
        motion_start = 0.0
        in_finger_off = False
        finger_start = 0.0

        for msg in messages:
            t = msg.plot_timestamp().timestamp()

            # 检测运动伪影变化
            if has_motion:
                mot = msg.motion_artifact
                if mot and not in_motion:
                    # 开始运动
                    motion_start = t
                    in_motion = True
                elif not mot and in_motion:
                    # 运动结束
                    motion_intervals.append((motion_start, t))
                    in_motion = False

            # 检测手指在位变化
            if has_finger:
                fng = msg.finger
                if not fng and not in_finger_off:
                    # 手指离开
                    finger_start = t
                    in_finger_off = True
                elif fng and in_finger_off:
                    # 手指重新在位
                    finger_off_intervals.append((finger_start, t))
                    in_finger_off = False

        # 处理未闭合的区间（状态持续到最新消息）
        if in_motion:
            motion_intervals.append(
                (motion_start, messages[-1].plot_timestamp().timestamp())
            )
        if in_finger_off:
            finger_off_intervals.append(
                (finger_start, messages[-1].plot_timestamp().timestamp())
            )

        # ---- 绘制运动伪影橙色背景带（应用于所有子图） ----
        for start_t, end_t in motion_intervals:
            region = pg.LinearRegionItem(
                values=(start_t, end_t),
                orientation=pg.LinearRegionItem.Vertical,
                movable=False,
                brush=pg.mkBrush(255, 165, 0, 30),  # 橙色，透明度 30
                pen=pg.mkPen(None),
            )
            self._motion_regions.append(region)
            for plot in self._plots:
                plot.addItem(region)

        # ---- 绘制手指离位灰色背景带（应用于所有子图） ----
        for start_t, end_t in finger_off_intervals:
            region = pg.LinearRegionItem(
                values=(start_t, end_t),
                orientation=pg.LinearRegionItem.Vertical,
                movable=False,
                brush=pg.mkBrush(128, 128, 128, 40),  # 灰色，透明度 40
                pen=pg.mkPen(None),
            )
            self._finger_regions.append(region)
            for plot in self._plots:
                plot.addItem(region)

    def clear(self) -> None:
        """清除所有曲线数据（保留子图框架）。"""
        for curve in self._curve_items.values():
            curve.setData([], [])

    def reset_views(self, x_min: float | None = None, x_max: float | None = None) -> None:
        """重置所有子图视图。先 autoRange Y 轴，再按指定范围设置 X 轴。"""
        for plot in self._plots:
            plot.autoRange()
            if x_min is not None and x_max is not None:
                plot.setXRange(x_min, x_max, padding=0.0)

    # -------------------------------------------------------------------------
    # Crosshair（跨子图十字光标）
    # -------------------------------------------------------------------------

    def _install_crosshair(self) -> None:
        """安装跨子图的 crosshair（垂直线 + 浮动 tooltip）。

        在所有子图中添加一条垂直虚线（InfiniteLine）和一个文本标签（TextItem），
        绑定到第一个子图 scene 的鼠标移动信号，
        实现鼠标悬停时在所有子图中显示对应 X 位置的数值。
        """
        # 垂直虚线
        self._crosshair_vline = pg.InfiniteLine(
            angle=90,
            movable=False,
            pen=pg.mkPen("w", width=1, style=Qt.PenStyle.DashLine),
        )
        # 浮动 tooltip 文本
        self._tooltip_label = pg.TextItem(
            "",
            anchor=(0, 1),
            color=(255, 255, 255),
            fill=(0, 0, 0, 180),
        )

        # 在所有子图中添加
        for plot in self._plots:
            plot.addItem(self._crosshair_vline, ignoreBounds=True)
            plot.addItem(self._tooltip_label, ignoreBounds=True)

        # 绑定鼠标移动 — 使用第一个子图 scene 的信号
        if self._plots:
            self._plots[0].scene().sigMouseMoved.connect(self._on_mouse_moved)

    def _on_mouse_moved(self, pos) -> None:
        """鼠标移动时更新 crosshair 位置和 tooltip 内容。

        将 scene 坐标转换为第一个子图的视图坐标，
        定位 crosshair 垂直线到鼠标 X 位置，
        并收集所有曲线在附近的数值显示在 tooltip 中。
        """
        if not self._plots or not self._crosshair_vline or not self._tooltip_label:
            return

        # 坐标转换：scene → 视图
        view_box = self._plots[0].getViewBox()
        if view_box is None:
            return
        mouse_point = view_box.mapSceneToView(pos)
        mx = mouse_point.x()

        # 移动垂直线
        self._crosshair_vline.setPos(mx)

        # ---- 收集 tooltip 内容 ----
        tooltip_lines: list[str] = []

        # 第一行：时间
        try:
            ts = datetime.fromtimestamp(mx)
            tooltip_lines.append(ts.strftime("%H:%M:%S"))
        except (ValueError, OSError):
            tooltip_lines.append(f"t={mx:.1f}")

        # 后续行：各曲线在 mx 处的值
        for key, curve in self._curve_items.items():
            cdef = self._curve_defs[key]
            x_data, y_data = curve.getData()
            if x_data is None or len(x_data) == 0:
                continue
            # 在 X 轴数据中查找最近邻索引
            idx = _nearest_idx(x_data, mx)
            if idx < 0 or idx >= len(y_data):
                continue
            yv = y_data[idx]
            if math.isnan(yv):
                continue
            # 反向缩放，显示原始值
            raw = yv / cdef.scale if cdef.scale != 1.0 else yv
            if cdef.unit:
                tooltip_lines.append(f"{cdef.label}: {raw:.1f}{cdef.unit}")
            else:
                tooltip_lines.append(f"{cdef.label}: {raw:.0f}")

        # 显示/隐藏 tooltip
        if len(tooltip_lines) > 1:
            self._tooltip_label.setText("\n".join(tooltip_lines))
            self._tooltip_label.setPos(
                mx, self._plots[0].getViewBox().viewRange()[1][1]
            )
            self._tooltip_label.setVisible(True)
        else:
            self._tooltip_label.setVisible(False)


def _nearest_idx(arr, target: float) -> int:
    """在已排序数组 arr 中查找与 target 最近的元素索引。

    使用二分查找加速（O(log n)）。

    参数：
      arr:    已排序的数值数组。
      target: 目标值。

    返回：
      最近元素的索引（0 到 len(arr)-1）。
    """
    import bisect

    idx = bisect.bisect_left(arr, target)
    if idx == 0:
        return 0
    if idx == len(arr):
        return len(arr) - 1
    before = arr[idx - 1]
    after = arr[idx]
    if abs(after - target) < abs(before - target):
        return idx
    return idx - 1


# =============================================================================
# StatusCard —— Overview 页面的单个指标卡片控件
# =============================================================================

class StatusCard(QFrame):
    """Overview Tab 中的单个指标卡片。

    每个卡片显示：
      - 标题（如 "BPM"、"SpO2"）
      - 大号数值（如 "72"、"98"）
      - 单位（如 "bpm"、"%"）
      - 状态文本（如 "无效"、"运动干扰"）

    值的有效/无效通过颜色区分：
      - 有效：亮白色
      - 无效：暗灰色
    """

    def __init__(
        self, title: str, unit: str = "", parent: QWidget | None = None
    ) -> None:
        """初始化卡片。

        参数：
          title: 卡片标题。
          unit:  单位文本。
          parent: 父控件。
        """
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(
            "StatusCard { background: #1e1e2e; border: 1px solid #333; border-radius: 8px; padding: 8px; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(4)

        # 标题标签（小号灰色文本）
        self._title_label = QLabel(title)
        self._title_label.setStyleSheet("color: #888; font-size: 10pt;")
        self._title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # 数值标签（大号加粗）
        self._value_label = QLabel("--")
        self._value_label.setStyleSheet(
            "color: #eee; font-size: 22pt; font-weight: bold;"
        )
        self._value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # 单位标签
        self._unit_label = QLabel(unit)
        self._unit_label.setStyleSheet("color: #666; font-size: 9pt;")
        self._unit_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # 状态标签
        self._status_label = QLabel("")
        self._status_label.setStyleSheet("color: #666; font-size: 8pt;")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout.addWidget(self._title_label)
        layout.addWidget(self._value_label)
        layout.addWidget(self._unit_label)
        layout.addWidget(self._status_label)

    def update_value(
        self, value_text: str, valid: bool = True, status: str = ""
    ) -> None:
        """更新卡片显示的值。

        参数：
          value_text: 显示的值文本（如 "72" 或 "--"）。
          valid:      值是否有效（控制颜色）。
          status:     额外的状态文本（如 "无效"、"运动干扰"）。
        """
        self._value_label.setText(value_text)
        if not valid:
            self._value_label.setStyleSheet(
                "color: #666; font-size: 22pt; font-weight: bold;"
            )
        else:
            self._value_label.setStyleSheet(
                "color: #eee; font-size: 22pt; font-weight: bold;"
            )
        self._status_label.setText(status)


# =============================================================================
# BaseTab —— 所有 Tab 页的抽象基类
# =============================================================================

class BaseTab(QWidget):
    """所有 Tab 页的基类。

    子类必须实现：
      - tab_title(): 返回 Tab 标签文本。
      - refresh():   从 DataManager 读取最新数据并更新 UI。

    所有 Tab 共享同一个 DataManager 实例。
    """

    def __init__(
        self,
        data_manager: DataManager,
        title: str,
        parent: QWidget | None = None,
    ) -> None:
        """初始化 Tab。

        参数：
          data_manager: 共享的数据管理器。
          title:        Tab 标签文本。
          parent:       父控件。
        """
        super().__init__(parent)
        self._data_manager = data_manager
        self._title = title

    def tab_title(self) -> str:
        """返回此 Tab 的标签文本。"""
        return self._title

    def refresh(self) -> None:
        """从 DataManager 读取最新数据并刷新显示。

        子类必须重写此方法以实现具体的刷新逻辑。
        """
        pass


# =============================================================================
# OverviewTab —— 总览页
# =============================================================================

class OverviewTab(BaseTab):
    """总览 Tab：核心生命体征卡片 + 传感器状态 + 解析/Schema 状态。

    布局自上而下：
      1. 顶部状态栏：MQTT 连接状态 + 最新帧时间。
      2. 核心体征卡片网格（2×4）：
         BPM | SpO2 | RR | IBI
         SQ  | Motion | ECG HR | PTT
      3. 传感器状态行：Finger | Raw Signal | LeadOff | Rhythm。
      4. 解析状态行：Parse OK/ERROR | protocol | schema | field_count | WARNINGS。
    """

    def __init__(
        self, data_manager: DataManager, parent: QWidget | None = None
    ) -> None:
        """初始化 Overview Tab。"""
        super().__init__(data_manager, "Overview", parent)
        self._cards: dict[str, StatusCard] = {}       # 卡片字典 (key → StatusCard)
        self._state_label = QLabel("等待数据...")
        self._state_label.setStyleSheet("color: #888; font-size: 9pt;")
        self._build_ui()

    def _build_ui(self) -> None:
        """构建 Overview Tab 的完整 UI 布局。"""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        main_layout = QVBoxLayout(inner)

        # ---- 顶部状态栏 ----
        top_bar = QHBoxLayout()
        self._conn_label = QLabel("MQTT: --")
        self._conn_label.setStyleSheet("color: #888; font-size: 10pt;")
        self._time_label = QLabel("")
        self._time_label.setStyleSheet("color: #888; font-size: 10pt;")
        top_bar.addWidget(self._conn_label)
        top_bar.addStretch(1)
        top_bar.addWidget(self._time_label)
        main_layout.addLayout(top_bar)

        # ---- 核心生命体征卡片网格（2 行 × 4 列） ----
        vital_grid = QGridLayout()
        vital_grid.setSpacing(10)

        # 卡片定义: (标题, 单位, 键名, 行, 列)
        card_defs: list[tuple[str, str, str, int, int]] = [
            # 第一行：核心 PPG 衍生指标
            ("BPM", "bpm", "bpm", 0, 0),
            ("SpO2", "%", "spo2", 0, 1),
            ("RR", "次/分", "rr", 0, 2),
            ("IBI", "ms", "ibi", 0, 3),
            # 第二行：信号质量 / ECG / PTT
            ("SQ", "0-100", "signal_quality", 1, 0),
            ("Motion", "", "motion", 1, 1),
            ("ECG HR", "bpm", "ecg_hr", 1, 2),
            ("PTT", "ms", "ptt_ms", 1, 3),
        ]

        for title, unit, key, row, col in card_defs:
            card = StatusCard(title, unit)
            self._cards[key] = card
            vital_grid.addWidget(card, row, col)

        main_layout.addLayout(vital_grid)

        # ---- 传感器状态行 ----
        status_layout = QHBoxLayout()
        self._finger_label = QLabel("Finger: --")
        self._finger_label.setStyleSheet("color: #888; font-size: 10pt;")
        self._raw_signal_label = QLabel("Raw Signal: --")
        self._raw_signal_label.setStyleSheet("color: #888; font-size: 10pt;")
        self._lead_off_label = QLabel("LeadOff: --")
        self._lead_off_label.setStyleSheet("color: #888; font-size: 10pt;")
        self._rhythm_label = QLabel("Rhythm: --")
        self._rhythm_label.setStyleSheet("color: #888; font-size: 10pt;")
        status_layout.addWidget(self._finger_label)
        status_layout.addWidget(self._raw_signal_label)
        status_layout.addWidget(self._lead_off_label)
        status_layout.addWidget(self._rhythm_label)
        status_layout.addStretch(1)
        main_layout.addLayout(status_layout)

        # ---- 解析状态行 ----
        parse_layout = QHBoxLayout()
        self._parse_label = QLabel("Parse: --")
        self._parse_label.setStyleSheet("color: #888; font-size: 9pt;")
        self._warn_label = QLabel("")
        self._warn_label.setStyleSheet("color: #aa0; font-size: 9pt;")
        self._rx_label = QLabel("")
        self._rx_label.setStyleSheet("color: #888; font-size: 9pt;")
        parse_layout.addWidget(self._parse_label)
        parse_layout.addWidget(self._warn_label)
        parse_layout.addStretch(1)
        parse_layout.addWidget(self._rx_label)
        main_layout.addLayout(parse_layout)

        main_layout.addWidget(self._state_label)
        main_layout.addStretch(1)

        scroll.setWidget(inner)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll)

    def set_connection_text(self, text: str) -> None:
        """设置 MQTT 连接状态文本（由 TabPlotManager 调用）。

        参数：
          text: MQTT 连接状态描述文本。
        """
        self._conn_label.setText(f"MQTT: {text}")

    def _safe_card(self, key: str) -> StatusCard | None:
        """安全获取卡片，KeyError 不中断 refresh。"""
        return self._cards.get(key)

    def refresh(self) -> None:
        """从 DataManager 读取最新消息并刷新所有卡片和状态标签。

        处理逻辑：
          1. 若无数据，显示 "等待数据..."。
          2. 更新 8 张核心指标卡片。
          3. 更新传感器状态（手指在位、原始信号、导联、心律）。
          4. 更新解析状态（含 schema 版本检测和警告）。
        """
        latest = self._data_manager.latest()
        if latest is None:
            self._state_label.setText("等待数据...")
            return

        # ---- 状态栏：最新帧时间和 RTC 状态 ----
        self._state_label.setText(
            f"最新帧: {latest.plot_timestamp().strftime('%Y-%m-%d %H:%M:%S')}  |  "
            f"{'RTC有效' if latest.timestamp_valid() else 'RTC无效(PC时间)'}"
        )

        # ---- 更新 8 张核心指标卡片 ----
        # 使用 _safe_card 防护：单个卡片 key 配置错误不会中断整体刷新。

        if (card := self._safe_card("bpm")) is not None:
            bpm_v, bpm_s = _fmt_int(latest.bpm, latest.bpm_valid)
            card.update_value(bpm_v, latest.bpm_valid, bpm_s)

        if (card := self._safe_card("spo2")) is not None:
            s_v, s_s = _fmt_int(latest.spo2, latest.spo2_valid)
            card.update_value(s_v, latest.spo2_valid, s_s)

        if (card := self._safe_card("rr")) is not None:
            rr_v, rr_s = _fmt_int(latest.rr, latest.rr_valid)
            card.update_value(rr_v, latest.rr_valid, rr_s)

        if (card := self._safe_card("ibi")) is not None:
            ibi_v, ibi_s = _fmt_int(latest.ibi, latest.ibi_valid)
            card.update_value(ibi_v, latest.ibi_valid, ibi_s)

        if (card := self._safe_card("signal_quality")) is not None:
            sq = latest.signal_quality
            sq_v = str(sq) if sq is not None else "--"
            sq_valid = sq is not None and sq > 0
            card.update_value(sq_v, sq_valid)

        if (card := self._safe_card("motion")) is not None:
            mot = latest.motion_artifact
            ms = latest.motion_score
            mot_v = f"YES ({ms})" if mot else "NO"
            card.update_value(mot_v, True, "运动干扰" if mot else "")

        if (card := self._safe_card("ecg_hr")) is not None:
            ecg_v, ecg_s = _fmt_int(latest.ecg_hr, latest.ecg_valid)
            card.update_value(ecg_v, latest.ecg_valid, ecg_s)

        if (card := self._safe_card("ptt_ms")) is not None:
            ptt_v, ptt_s = _fmt_int(latest.ptt_ms, latest.ptt_valid)
            card.update_value(ptt_v, latest.ptt_valid, ptt_s)

        # ---- 传感器状态 ----

        # 手指在位
        fng = latest.finger
        self._finger_label.setText(f"Finger: {'在位' if fng else '离位'}")
        self._finger_label.setStyleSheet(
            f"color: {'#0f0' if fng else '#f00'}; font-size: 10pt;"
        )

        # 原始信号
        rs = latest.raw_signal_present
        if rs is None:
            self._raw_signal_label.setText("Raw Signal: --")
            self._raw_signal_label.setStyleSheet("color: #888; font-size: 10pt;")
        elif rs:
            self._raw_signal_label.setText("Raw Signal: YES")
            self._raw_signal_label.setStyleSheet("color: #0f0; font-size: 10pt;")
        else:
            self._raw_signal_label.setText("Raw Signal: NO")
            self._raw_signal_label.setStyleSheet("color: #f00; font-size: 10pt;")

        # 导联脱落
        lo = latest.ecg_lead_off_label
        if lo == "OK":
            self._lead_off_label.setText(f"LeadOff: {lo}")
            self._lead_off_label.setStyleSheet("color: #0f0; font-size: 10pt;")
        else:
            self._lead_off_label.setText(f"LeadOff: {lo}")
            self._lead_off_label.setStyleSheet("color: #f00; font-size: 10pt;")

        # 心律状态
        ri = latest.rhythm_irregular
        if ri:
            self._rhythm_label.setText("Rhythm: IRREGULAR")
            self._rhythm_label.setStyleSheet(
                "color: #f80; font-size: 10pt; font-weight: bold;"
            )
        else:
            self._rhythm_label.setText("Rhythm: Regular")
            self._rhythm_label.setStyleSheet("color: #0f0; font-size: 10pt;")

        # ---- 解析状态（含 Schema 检测） ----
        schema_issue = latest.detect_schema_issue()
        protocol = latest.protocol or "json"
        schema_ver = latest.schema_version or "?"
        self._parse_label.setText(
            f"Parse: {'OK' if latest.parse_ok else 'ERROR'}"
            f"  |  protocol={protocol}"
            f"  |  schema={schema_ver}"
            f"  |  fields={latest.field_count}"
        )

        # 解析状态颜色
        # 橙色加粗 = schema 问题（旧 ESP32 固件等）
        # 绿色 = 正常
        # 红色 = 解析错误
        if schema_issue:
            self._parse_label.setStyleSheet(
                "color: #f80; font-size: 9pt; font-weight: bold;"
            )
        elif latest.parse_ok:
            self._parse_label.setStyleSheet("color: #0f0; font-size: 9pt;")
        else:
            self._parse_label.setStyleSheet("color: #f00; font-size: 9pt;")

        # Schema 警告 / 解析警告
        if schema_issue:
            # Schema 不兼容：显示橙色加粗警告
            self._warn_label.setText(f"SCHEMA: {schema_issue}")
            self._warn_label.setStyleSheet(
                "color: #f80; font-size: 10pt; font-weight: bold;"
            )
        else:
            warns = latest.parse_warnings
            if warns:
                self._warn_label.setText(f"WARNINGS: {len(warns)}")
                self._warn_label.setStyleSheet("color: #aa0; font-size: 9pt;")
            else:
                self._warn_label.setText("")

        # 接收延迟
        self._rx_label.setText(f"rx_ms={latest.rx_ms}")

        # 帧时间
        self._time_label.setText(latest.plot_timestamp().strftime("%H:%M:%S"))


def _fmt_int(value: int | None, valid: bool) -> tuple[str, str]:
    """格式化整数值用于卡片显示。

    参数：
      value: 整数值或 None。
      valid: 是否有效。

    返回：
      (显示文本, 状态文本) 元组。
        - 值为 None → ("--", "")
        - 值存在但无效 → (str(value), "无效")
        - 值有效 → (str(value), "")
    """
    if value is None:
        return "--", ""
    if not valid:
        return str(value), "无效"
    return str(value), ""


# =============================================================================
# VitalsTrendsTab —— 生命体征趋势曲线
# =============================================================================

class VitalsTrendsTab(BaseTab):
    """生命体征趋势 Tab：BPM & RR、SpO2、IBI 三条子图。

    每个子图均叠加运动伪影（橙色）和手指离位（灰色）背景带。
    """

    def __init__(
        self, data_manager: DataManager, parent: QWidget | None = None
    ) -> None:
        """初始化生命体征趋势 Tab。"""
        super().__init__(data_manager, "Vitals Trends", parent)
        self._plot_group: PlotGroup | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        """构建子图配置和 PlotGroup。"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        subplots = [
            SubplotDef(
                title="BPM & RR",
                y_label="bpm / 次/分",
                curves=[
                    CurveDef(
                        "bpm", "BPM", "#0984e3", width=2,
                        valid_check="bpm_valid", unit="bpm",
                    ),
                    CurveDef(
                        "rr", "RR", "#00b894", width=2, dashed=True,
                        valid_check="rr_valid", unit="次/分",
                    ),
                ],
                motion_background=True,
                finger_background=True,
            ),
            SubplotDef(
                title="SpO2",
                y_label="%",
                curves=[
                    CurveDef(
                        "spo2", "SpO2", "#6c5ce7", width=2,
                        valid_check="spo2_valid", unit="%",
                    ),
                ],
                y_range=(70, 100),       # SpO2 限定在 70-100% 范围
                motion_background=True,
                finger_background=True,
            ),
            SubplotDef(
                title="IBI",
                y_label="ms",
                curves=[
                    CurveDef(
                        "ibi", "IBI", "#e17055", width=2,
                        valid_check="ibi_valid", unit="ms",
                    ),
                ],
                motion_background=True,
                finger_background=True,
            ),
        ]

        self._plot_group = PlotGroup(self._data_manager, subplots)
        layout.addWidget(self._plot_group.widget)

    def refresh(self) -> None:
        """刷新曲线数据。"""
        if self._plot_group:
            self._plot_group.refresh()


# =============================================================================
# SignalQualityTab —— 信号质量趋势曲线
# =============================================================================

class SignalQualityTab(BaseTab):
    """信号质量趋势 Tab：SQ、Motion Score、PI、R Ratio 四条子图。"""

    def __init__(
        self, data_manager: DataManager, parent: QWidget | None = None
    ) -> None:
        """初始化信号质量 Tab。"""
        super().__init__(data_manager, "Signal Quality", parent)
        self._plot_group: PlotGroup | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        """构建子图配置和 PlotGroup。"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        subplots = [
            SubplotDef(
                title="Signal Quality (0-100)",
                y_label="SQ",
                curves=[
                    CurveDef("signal_quality", "SQ", "#00b894", width=2),
                ],
                y_range=(0, 100),
            ),
            SubplotDef(
                title="Motion Score",
                y_label="score",
                curves=[
                    CurveDef("motion_score", "Motion", "#e17055", width=2),
                ],
            ),
            SubplotDef(
                title="Perfusion Index (PI / 10 %)",
                y_label="%",
                curves=[
                    # PI×1000 × 0.1 = PI 百分比
                    CurveDef(
                        "signal_ir_pi_x1000", "IR PI", "#0984e3", width=2,
                        scale=0.1, unit="%",
                    ),
                    CurveDef(
                        "signal_red_pi_x1000", "Red PI", "#d63031", width=2,
                        scale=0.1, unit="%",
                    ),
                ],
            ),
            SubplotDef(
                title="SpO2 Ratio (R / 1000)",
                y_label="ratio",
                curves=[
                    # R×1000 × 0.001 = R 比值
                    CurveDef(
                        "spo2_ratio_x1000", "R", "#6c5ce7", width=2,
                        scale=0.001, valid_check="spo2_ratio_valid",
                    ),
                ],
            ),
        ]

        self._plot_group = PlotGroup(self._data_manager, subplots)
        layout.addWidget(self._plot_group.widget)

    def refresh(self) -> None:
        """刷新曲线数据。"""
        if self._plot_group:
            self._plot_group.refresh()


# =============================================================================
# PPGRawTab —— 原始 PPG 信号趋势曲线
# =============================================================================

class PPGRawTab(BaseTab):
    """原始 PPG Tab：Red/IR/Baseline、AC RMS、Signal Span/Delta 三条子图。

    注意：这些是低频趋势（约 5 Hz），不是真实的 PPG 波形。
    """

    def __init__(
        self, data_manager: DataManager, parent: QWidget | None = None
    ) -> None:
        """初始化 PPG Raw Tab。"""
        super().__init__(data_manager, "PPG Raw", parent)
        self._plot_group: PlotGroup | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        """构建子图配置和 PlotGroup。"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # 提示：低频趋势，非真实波形
        note = QLabel("低频趋势，不是真实 PPG 波形 (约 5 Hz)")
        note.setStyleSheet("color: #aa0; font-size: 9pt; padding: 4px;")
        layout.addWidget(note)

        subplots = [
            SubplotDef(
                title="PPG Raw — Red / IR / Baseline",
                y_label="ADC",
                curves=[
                    CurveDef("red", "Red", "#d63031", width=1),
                    CurveDef("ir", "IR", "#00b894", width=1),
                    CurveDef(
                        "baseline_ir", "Baseline IR", "#636e72",
                        width=1, dashed=True,
                    ),
                ],
                finger_background=True,
            ),
            SubplotDef(
                title="AC RMS",
                y_label="ADC",
                curves=[
                    CurveDef(
                        "signal_ir_ac_rms", "IR AC RMS", "#00b894", width=2,
                    ),
                    CurveDef(
                        "signal_red_ac_rms", "Red AC RMS", "#d63031", width=2,
                    ),
                ],
            ),
            SubplotDef(
                title="Signal Span / Delta",
                y_label="ADC",
                curves=[
                    CurveDef(
                        "ir_signal_delta", "IR Delta", "#0984e3", width=2,
                    ),
                    CurveDef(
                        "ir_signal_span", "IR Span", "#6c5ce7", width=2,
                    ),
                    CurveDef(
                        "red_signal_span", "Red Span", "#e17055", width=2,
                    ),
                ],
            ),
        ]

        self._plot_group = PlotGroup(self._data_manager, subplots)
        layout.addWidget(self._plot_group.widget)

    def refresh(self) -> None:
        """刷新曲线数据。"""
        if self._plot_group:
            self._plot_group.refresh()


# =============================================================================
# HRVTab —— HRV 心率变异性趋势曲线
# =============================================================================

class HRVTab(BaseTab):
    """HRV Tab：时域、庞加莱图、频域、LF/HF 四条子图。

    注意：短窗口趋势，仅工程观察，不是临床诊断。
    """

    def __init__(
        self, data_manager: DataManager, parent: QWidget | None = None
    ) -> None:
        """初始化 HRV Tab。"""
        super().__init__(data_manager, "HRV", parent)
        self._plot_group: PlotGroup | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        """构建子图配置和 PlotGroup。"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # 提示：仅工程观察
        note = QLabel("短窗口趋势，仅工程观察，不是诊断")
        note.setStyleSheet("color: #aa0; font-size: 9pt; padding: 4px;")
        layout.addWidget(note)

        subplots = [
            SubplotDef(
                title="HRV Time Domain (ms)",
                y_label="ms",
                curves=[
                    CurveDef(
                        "mean_ibi", "Mean IBI", "#0984e3", width=2,
                        valid_check="hrv_valid", unit="ms",
                    ),
                    CurveDef(
                        "sdnn", "SDNN", "#00b894", width=2,
                        valid_check="hrv_valid", unit="ms",
                    ),
                    CurveDef(
                        "rmssd", "RMSSD", "#6c5ce7", width=2,
                        valid_check="hrv_valid", unit="ms",
                    ),
                ],
            ),
            SubplotDef(
                title="Poincare (ms)",
                y_label="ms",
                curves=[
                    CurveDef(
                        "sd1", "SD1", "#0984e3", width=2,
                        valid_check="hrv_valid", unit="ms",
                    ),
                    CurveDef(
                        "sd2", "SD2", "#d63031", width=2,
                        valid_check="hrv_valid", unit="ms",
                    ),
                    # SD1/SD2×100 ÷ 100 = SD1/SD2 比值
                    CurveDef(
                        "sd1_sd2_x100", "SD1/SD2×100", "#fdcb6e",
                        width=1, dashed=True, scale=0.01,
                    ),
                ],
            ),
            SubplotDef(
                title="HRV Freq Domain",
                y_label="ms² / ratio",
                curves=[
                    # LF×100 × 0.01 = LF
                    CurveDef(
                        "lf_power_x100", "LF (×0.01 ms²)", "#0984e3",
                        width=2, scale=0.01, valid_check="hrv_freq_valid",
                    ),
                    # HF×100 × 0.01 = HF
                    CurveDef(
                        "hf_power_x100", "HF (×0.01 ms²)", "#6c5ce7",
                        width=2, scale=0.01, valid_check="hrv_freq_valid",
                    ),
                ],
            ),
            SubplotDef(
                title="LF/HF Ratio",
                y_label="ratio",
                curves=[
                    # LF/HF×100 × 0.01 = LF/HF 比值
                    CurveDef(
                        "lf_hf_x100", "LF/HF (×0.01)", "#e17055",
                        width=2, scale=0.01, valid_check="hrv_freq_valid",
                    ),
                ],
            ),
        ]

        self._plot_group = PlotGroup(self._data_manager, subplots)
        layout.addWidget(self._plot_group.widget)

    def refresh(self) -> None:
        """刷新曲线数据。"""
        if self._plot_group:
            self._plot_group.refresh()


# =============================================================================
# ECGIPTTTab —— ECG / PTT 趋势曲线
# =============================================================================

class ECGIPTTTab(BaseTab):
    """ECG / PTT Tab：ECG 滤波/原始波形、ECG HR、ECG RR、PTT 四条子图。

    注意：ECG 是低频趋势（约 5 Hz），非真实 ECG 波形；PTT 不是血压估计。
    """

    def __init__(
        self, data_manager: DataManager, parent: QWidget | None = None
    ) -> None:
        """初始化 ECG / PTT Tab。"""
        super().__init__(data_manager, "ECG / PTT", parent)
        self._plot_group: PlotGroup | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        """构建子图配置和 PlotGroup。"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # 提示：低频趋势，非真实波形
        note = QLabel("ECG 低频趋势 (约 5 Hz)，不是真实 ECG 波形。PTT 不是血压估计。")
        note.setStyleSheet("color: #aa0; font-size: 9pt; padding: 4px;")
        layout.addWidget(note)

        subplots = [
            SubplotDef(
                title="ECG Filtered / Raw (low-freq trend)",
                y_label="ADC",
                curves=[
                    CurveDef(
                        "ecg_filtered", "ECG Filt", "#00b894", width=1,
                    ),
                    CurveDef(
                        "ecg_raw", "ECG Raw", "#636e72",
                        width=1, dashed=True,
                    ),
                ],
            ),
            SubplotDef(
                title="ECG HR",
                y_label="bpm",
                curves=[
                    CurveDef(
                        "ecg_hr", "ECG HR", "#0984e3", width=2,
                        valid_check="ecg_valid", unit="bpm",
                    ),
                ],
            ),
            SubplotDef(
                title="ECG RR Interval",
                y_label="ms",
                curves=[
                    CurveDef(
                        "ecg_rr_ms", "ECG RR", "#6c5ce7", width=2,
                        valid_check="ecg_valid", unit="ms",
                    ),
                ],
            ),
            SubplotDef(
                title="PTT",
                y_label="ms",
                curves=[
                    CurveDef(
                        "ptt_ms", "PTT", "#e17055", width=2,
                        valid_check="ptt_valid", unit="ms",
                    ),
                ],
            ),
        ]

        self._plot_group = PlotGroup(self._data_manager, subplots)
        layout.addWidget(self._plot_group.widget)

    def refresh(self) -> None:
        """刷新曲线数据。"""
        if self._plot_group:
            self._plot_group.refresh()


# =============================================================================
# DiagnosticsTab —— 诊断页
# =============================================================================

class DiagnosticsTab(BaseTab):
    """诊断 Tab：传感器表 + 系统表 + 额外/未知字段 + 解析警告 + 原始帧 + 原始 JSON。

    布局（自上而下，用 QSplitter 分隔）：
      1. 解析状态标签（message_type, parse_ok, protocol, schema, field_count, rx_ms）
      2. 传感器诊断表（Sensor Diagnostics）
      3. 系统诊断表（System Diagnostics —— 当前 102 字段 schema）
      4. 额外/未知字段表（Extra / Unknown Fields）
      5. 解析警告文本框（Parse Warnings）
      6. 原始帧文本框（Raw Frame —— STM32 CSV 行）
      7. 原始 JSON 文本框（Raw JSON）
    """

    def __init__(
        self, data_manager: DataManager, parent: QWidget | None = None
    ) -> None:
        """初始化诊断 Tab。"""
        super().__init__(data_manager, "Diagnostics", parent)
        # 子控件引用
        self._sensor_table: QTableWidget | None = None
        self._system_table: QTableWidget | None = None
        self._extra_table: QTableWidget | None = None
        self._raw_json_view: QPlainTextEdit | None = None
        self._raw_frame_view: QPlainTextEdit | None = None
        self._warnings_view: QPlainTextEdit | None = None
        self._parse_info_label: QLabel | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        """构建诊断 Tab 的完整 UI 布局。"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        # ---- 解析状态标签 ----
        self._parse_info_label = QLabel("")
        self._parse_info_label.setStyleSheet("color: #888; font-size: 9pt;")
        layout.addWidget(self._parse_info_label)

        splitter = QSplitter(Qt.Orientation.Vertical)

        # ---- 传感器诊断表 ----
        sensor_group = QGroupBox("Sensor Diagnostics")
        sensor_layout = QVBoxLayout(sensor_group)
        self._sensor_table = QTableWidget(0, 2)
        self._sensor_table.setHorizontalHeaderLabels(["Parameter", "Value"])
        self._sensor_table.horizontalHeader().setStretchLastSection(True)
        self._sensor_table.setAlternatingRowColors(True)
        self._sensor_table.setStyleSheet(
            "QTableWidget { background: #1e1e2e; gridline-color: #333; color: #ddd; }"
            "QHeaderView::section { background: #2d2d3d; color: #aaa; }"
        )
        sensor_layout.addWidget(self._sensor_table)
        splitter.addWidget(sensor_group)

        # ---- 系统诊断表（当前 102 字段 schema） ----
        sys_group = QGroupBox("System Diagnostics")
        sys_layout = QVBoxLayout(sys_group)
        self._system_table = QTableWidget(0, 2)
        self._system_table.setHorizontalHeaderLabels(["Parameter", "Value"])
        self._system_table.horizontalHeader().setStretchLastSection(True)
        self._system_table.setAlternatingRowColors(True)
        self._system_table.setStyleSheet(
            "QTableWidget { background: #1e1e2e; gridline-color: #333; color: #ddd; }"
            "QHeaderView::section { background: #2d2d3d; color: #aaa; }"
        )
        sys_layout.addWidget(self._system_table)
        splitter.addWidget(sys_group)

        # ---- 额外/未知字段表 ----
        extra_group = QGroupBox("Extra / Unknown Fields")
        extra_layout = QVBoxLayout(extra_group)
        self._extra_table = QTableWidget(0, 2)
        self._extra_table.setHorizontalHeaderLabels(["Field", "Value"])
        self._extra_table.horizontalHeader().setStretchLastSection(True)
        self._extra_table.setAlternatingRowColors(True)
        self._extra_table.setStyleSheet(
            "QTableWidget { background: #1e1e2e; gridline-color: #333; color: #ddd; }"
            "QHeaderView::section { background: #2d2d3d; color: #aaa; }"
        )
        extra_layout.addWidget(self._extra_table)
        splitter.addWidget(extra_group)

        # ---- 解析警告文本框 ----
        warnings_group = QGroupBox("Parse Warnings")
        warnings_layout = QVBoxLayout(warnings_group)
        self._warnings_view = QPlainTextEdit()
        self._warnings_view.setReadOnly(True)
        self._warnings_view.setMaximumBlockCount(50)
        self._warnings_view.setPlaceholderText("无解析警告")
        self._warnings_view.setStyleSheet(
            "QPlainTextEdit { background: #111; color: #f80;"
            " font-family: Consolas, monospace; font-size: 9pt; }"
        )
        warnings_layout.addWidget(self._warnings_view)
        splitter.addWidget(warnings_group)

        # ---- 原始帧文本框（STM32 CSV） ----
        raw_frame_group = QGroupBox("Raw Frame (STM32 CSV)")
        raw_frame_layout = QVBoxLayout(raw_frame_group)
        self._raw_frame_view = QPlainTextEdit()
        self._raw_frame_view.setReadOnly(True)
        self._raw_frame_view.setMaximumBlockCount(10)
        self._raw_frame_view.setPlaceholderText("无原始 CSV 帧")
        self._raw_frame_view.setStyleSheet(
            "QPlainTextEdit { background: #111; color: #0ff;"
            " font-family: Consolas, monospace; font-size: 9pt; }"
        )
        raw_frame_layout.addWidget(self._raw_frame_view)
        splitter.addWidget(raw_frame_group)

        # ---- 原始 JSON 文本框 ----
        raw_json_group = QGroupBox("Raw JSON (latest message)")
        raw_json_layout = QVBoxLayout(raw_json_group)
        self._raw_json_view = QPlainTextEdit()
        self._raw_json_view.setReadOnly(True)
        self._raw_json_view.setMaximumBlockCount(200)
        self._raw_json_view.setStyleSheet(
            "QPlainTextEdit { background: #111; color: #0f0;"
            " font-family: Consolas, monospace; font-size: 10pt; }"
        )
        raw_json_layout.addWidget(self._raw_json_view)
        splitter.addWidget(raw_json_group)

        layout.addWidget(splitter)

    def refresh(self) -> None:
        """从 DataManager 读取最新消息并刷新所有诊断视图。

        刷新内容：
          1. 解析状态标签（含 Schema 检测结果）。
          2. 传感器诊断表（14 个 I2C/FIFO/采样 相关字段）。
          3. 系统诊断表（当前 102 字段 schema：RTC/UART/SD/Display/System/Finger）。
          4. 额外/未知字段表（不在已知字段集合中的字段 + extra_fields 列表项）。
          5. 解析警告文本框（设备端报告的 parse_warnings 详情）。
          6. 原始帧文本框（STM32 CSV 原始行）。
          7. 原始 JSON 文本框（格式化的完整 JSON）。
        """
        latest = self._data_manager.latest()
        if latest is None:
            return

        # ---- 解析状态标签 ----
        schema_ver = latest.schema_version or "?"
        protocol = latest.protocol or "?"
        schema_issue = latest.detect_schema_issue()
        warnings = latest.parse_warnings

        warn_text = f" | warnings: {len(warnings)}" if warnings else ""
        issue_text = f" | SCHEMA ISSUE: {schema_issue}" if schema_issue else ""
        self._parse_info_label.setText(
            f"message={latest.message_type} | parse_ok={latest.parse_ok}"
            f" | protocol={protocol} | schema={schema_ver}"
            f" | fields={latest.field_count} | rx_ms={latest.rx_ms}"
            f"{warn_text}{issue_text}"
        )
        # 状态标签颜色：橙色=Schema 问题，红色=解析错误，灰色=正常
        if schema_issue:
            self._parse_info_label.setStyleSheet(
                "color: #f80; font-size: 9pt; font-weight: bold;"
            )
        elif not latest.parse_ok:
            self._parse_info_label.setStyleSheet("color: #f00; font-size: 9pt;")
        else:
            self._parse_info_label.setStyleSheet("color: #888; font-size: 9pt;")

        # ---- 传感器诊断表 ----
        # 包含 I2C 状态、FIFO 指针/溢出、读取统计、采样统计
        self._populate_table(self._sensor_table, [
            ("sensor_last_read_status", latest.get_int("sensor_last_read_status")),
            ("sensor_error_streak", latest.get_int("sensor_error_streak")),
            ("sensor_fifo_write_ptr", latest.get_int("sensor_fifo_write_ptr")),
            ("sensor_fifo_read_ptr", latest.get_int("sensor_fifo_read_ptr")),
            ("sensor_fifo_overflow_count", latest.get_int("sensor_fifo_overflow_count")),
            ("sensor_fifo_available_samples", latest.get_int("sensor_fifo_available_samples")),
            ("sensor_read_ok_count", latest.get_int("sensor_read_ok_count")),
            ("sensor_read_busy_count", latest.get_int("sensor_read_busy_count")),
            ("sensor_read_error_count", latest.get_int("sensor_read_error_count")),
            ("sensor_recover_count", latest.get_int("sensor_recover_count")),
            ("sensor_last_sample_tick", latest.get_int("sensor_last_sample_tick")),
            ("sensor_sample_change_count", latest.get_int("sensor_sample_change_count")),
            ("sensor_sample_same_count", latest.get_int("sensor_sample_same_count")),
            ("sensor_last_i2c_error", latest.get_int("sensor_last_i2c_error")),
        ])

        # ---- 系统诊断表（当前 102 字段 schema） ----
        self._populate_table(self._system_table, [
            # RTC / UART 状态
            ("rtc_read_ok", latest.get_bool("rtc_read_ok")),
            ("uart_rx_message_valid", latest.get_bool("uart_rx_message_valid")),
            ("uart_tx_message_valid", latest.get_bool("uart_tx_message_valid")),
            # SD 卡（当前字段）
            ("sd_log_active", latest.get_bool("sd_log_active")),
            ("sd_state", latest.get_int("sd_state")),
            ("sd_error", latest.get_int("sd_error")),
            ("sd_total_written", latest.get_int("sd_total_written")),
            # 显示屏
            ("display_refresh_count", latest.get_int("display_refresh_count")),
            ("display_last_refresh_tick", latest.get_int("display_last_refresh_tick")),
            # 系统状态
            ("debug_mode", latest.get_bool("debug_mode")),
            ("current_page", latest.get_int("current_page")),
            ("crash_flag", latest.get_bool("crash_flag")),
            ("crash_source", latest.get_int("crash_source")),
            ("reboot_count", latest.get_int("reboot_count")),
            # 手指检测统计
            ("finger_on_confirm_count", latest.get_int("finger_on_confirm_count")),
            ("finger_off_confirm_count", latest.get_int("finger_off_confirm_count")),
            ("adaptive_finger_on_delta", latest.get_int("adaptive_finger_on_delta")),
            ("adaptive_finger_off_delta", latest.get_int("adaptive_finger_off_delta")),
            ("spo2_balance_status", latest.get_int("spo2_balance_status")),
        ])

        # ---- 额外/未知字段表 ----
        extra_rows: list[tuple[str, Any]] = []

        # 已知字段名集合（来自 FlexibleMessage 的所有属性 + 模块内部字段）
        known_keys = {
            "message", "bridge", "source", "channel", "protocol", "frame",
            "rtc_valid", "date", "time", "red", "ir", "baseline_ir", "finger",
            "bpm_valid", "bpm", "spo2_valid", "spo2", "rr_valid", "rr",
            "ibi_valid", "ibi", "signal_quality", "motion_artifact", "motion_score",
            "raw_signal_present", "ecg_valid", "ecg_hr", "ecg_rr_ms", "ecg_lead_off",
            "ecg_r_peak_ms", "ecg_filtered", "ecg_raw", "ptt_valid", "ptt_ms",
            "hrv_valid", "mean_ibi", "sdnn", "rmssd", "sd1", "sd2", "sd1_sd2_x100",
            "rhythm_irregular", "hrv_freq_valid", "lf_power_x100", "hf_power_x100",
            "lf_hf_x100", "signal_ir_pi_x1000", "signal_red_pi_x1000",
            "signal_ir_ac_rms", "signal_red_ac_rms", "spo2_ratio_valid",
            "spo2_ratio_x1000", "spo2_balance_status",
            "ir_signal_delta", "ir_signal_span", "red_signal_span", "baseline_range_ir",
            "parse_ok", "rx_ms", "field_count", "schema_version", "parse_warnings",
            "extra_fields", "raw_line", "error", "set_ok", "reason",
            "sd_log_active", "sd_state", "sd_error", "sd_total_written",
            "display_refresh_count", "display_last_refresh_tick",
            "debug_mode", "current_page", "crash_flag", "crash_source", "reboot_count",
            "rtc_read_ok", "uart_rx_message_valid", "uart_tx_message_valid",
            "sensor_last_read_status", "sensor_error_streak", "sensor_fifo_write_ptr",
            "sensor_fifo_read_ptr", "sensor_fifo_overflow_count",
            "sensor_fifo_available_samples", "sensor_read_ok_count",
            "sensor_read_busy_count", "sensor_read_error_count",
            "sensor_recover_count", "sensor_last_sample_tick",
            "sensor_sample_change_count", "sensor_sample_same_count",
            "sensor_last_i2c_error", "finger_on_confirm_count",
            "finger_off_confirm_count", "adaptive_finger_on_delta",
            "adaptive_finger_off_delta", "modules", "data",
        }

        # 收集 raw 字典中不在已知集合中的键
        for key, value in latest.raw.items():
            if key not in known_keys and key != "raw_line":
                extra_rows.append((key, value))

        # 同时显示 extra_fields 列表项（CSV 中 colN=val 格式）
        ef = latest.extra_fields
        if ef:
            for item in ef:
                extra_rows.append(("extra_field", item))

        self._populate_table(self._extra_table, extra_rows)

        # ---- 原始帧（STM32 CSV 原始行） ----
        if self._raw_frame_view:
            raw_line = latest.raw_line
            if raw_line:
                self._raw_frame_view.setPlainText(raw_line)
            else:
                self._raw_frame_view.setPlainText("")

        # ---- 原始 JSON（格式化输出） ----
        if self._raw_json_view:
            try:
                text = json.dumps(latest.raw, indent=2, ensure_ascii=False)
            except (TypeError, ValueError):
                text = str(latest.raw)
            self._raw_json_view.setPlainText(text)

        # ---- 解析警告详情 ----
        if self._warnings_view:
            if warnings:
                self._warnings_view.setPlainText(
                    "\n".join(str(w) for w in warnings)
                )
            else:
                self._warnings_view.setPlainText("")

    def _populate_table(
        self, table: QTableWidget, rows: list[tuple[str, Any]]
    ) -> None:
        """填充诊断表格。

        将 (键名, 值) 列表写入表格控件。
        值为 None 的行被过滤掉（不显示缺失的字段）。

        参数：
          table: 目标表格控件。
          rows:  (键名, 值) 元组列表。
        """
        if table is None:
            return
        # 过滤掉值为 None 的行
        filtered = [(k, v) for k, v in rows if v is not None]
        table.setRowCount(len(filtered))
        for i, (key, value) in enumerate(filtered):
            table.setItem(i, 0, QTableWidgetItem(key))
            table.setItem(i, 1, QTableWidgetItem(str(value)))


# =============================================================================
# TabPlotManager —— 顶层 Tab 容器
# =============================================================================

class TabPlotManager(QWidget):
    """顶层 Tab 容器控件。

    职责：
      1. 创建并管理所有 Tab（Overview + 6 个图窗/诊断页）。
      2. 提供时间窗口选择（30s / 2min / 10min / All）。
      3. 提供暂停/恢复更新按钮。
      4. 提供清除数据按钮。
      5. 统一的 update_all() 入口：遍历所有 Tab 并调用 refresh()。

    数据流：
      DataManager.data_received 信号 → MainWindow._on_data_received()
                                           └── TabPlotManager.update_all()
                                                └── 遍历所有 Tab.refresh()
    """

    def __init__(
        self, data_manager: DataManager, parent: QWidget | None = None
    ) -> None:
        """初始化 TabPlotManager。

        参数：
          data_manager: 共享的数据管理器。
          parent:       父控件。
        """
        super().__init__(parent)
        self._data_manager = data_manager
        self._tabs: list[BaseTab] = []
        self._overview_tab: OverviewTab | None = None
        self._tab_widget = QTabWidget()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # ---- 工具栏：时间窗口 + 暂停 + 清除 + 点数显示 ----
        toolbar = QHBoxLayout()

        # 时间窗口选择下拉框
        toolbar.addWidget(QLabel("Time Window:"))
        self._time_combo = QComboBox()
        self._time_combo.addItems(["30 s", "2 min", "10 min", "All (buffer)"])
        self._time_combo.currentIndexChanged.connect(self._on_time_window_changed)
        self._time_combo.setCurrentIndex(1)  # 默认 2 分钟，触发 _on_time_window_changed

        # 暂停/恢复按钮
        self._pause_button = QPushButton("Pause")
        self._pause_button.setCheckable(True)
        self._pause_button.toggled.connect(self._on_pause_toggled)

        # 清除按钮
        self._clear_button = QPushButton("Clear")
        self._clear_button.clicked.connect(self._on_clear)

        toolbar.addWidget(self._time_combo)
        toolbar.addWidget(self._pause_button)
        toolbar.addWidget(self._clear_button)
        toolbar.addStretch(1)

        # 数据点数显示
        self._point_count_label = QLabel("Points: 0")
        toolbar.addWidget(self._point_count_label)

        layout.addLayout(toolbar)

        # ---- 创建所有 Tab ----
        # 第 0 个 Tab: Overview（总览卡片页）
        self._overview_tab = OverviewTab(data_manager)

        # 依次创建 6 个图窗/诊断 Tab
        tab_classes: list[type[BaseTab]] = [
            VitalsTrendsTab,        # Tab 1: 生命体征趋势
            SignalQualityTab,       # Tab 2: 信号质量
            PPGRawTab,              # Tab 3: 原始 PPG
            HRVTab,                 # Tab 4: HRV 心率变异性
            ECGIPTTTab,             # Tab 5: ECG / PTT
            DiagnosticsTab,         # Tab 6: 诊断页
        ]
        for tc in tab_classes:
            t = tc(data_manager)
            self._tabs.append(t)
            self._tab_widget.addTab(t, t.tab_title())

        # Overview 作为第 0 个 Tab 插入（保持在最开始）
        self._tab_widget.insertTab(0, self._overview_tab, self._overview_tab.tab_title())
        self._tabs.insert(0, self._overview_tab)

        layout.addWidget(self._tab_widget)

    def set_connection_text(self, text: str) -> None:
        """设置 Overview Tab 中的 MQTT 连接状态文本。

        由 MainWindow._set_connection_status() 调用。

        参数：
          text: MQTT 连接状态描述文本。
        """
        if self._overview_tab:
            self._overview_tab.set_connection_text(text)

    def update_all(self) -> None:
        """刷新所有 Tab 的显示。

        若暂停按钮被选中，跳过刷新（冻结显示）。
        刷新后更新右上角的点数统计。
        All 视图时同步刷新 X 轴右边界。
        """
        if self._pause_button.isChecked():
            return
        for tab in self._tabs:
            tab.refresh()
        self._point_count_label.setText(f"Points: {len(self._data_manager)}")
        # All 视图：右侧随新数据动态增长
        if self._data_manager.time_window() is None:
            now = datetime.now().timestamp()
            origin = self._data_manager.origin_time.timestamp()
            for tab in self._tabs:
                pg = getattr(tab, '_plot_group', None)
                if pg is not None:
                    pg.reset_views(x_min=origin, x_max=now)

    def as_widget(self) -> QWidget:
        """返回自身作为 QWidget（兼容旧接口）。"""
        return self

    # -------------------------------------------------------------------------
    # 工具栏按钮回调
    # -------------------------------------------------------------------------

    def _on_time_window_changed(self, index: int) -> None:
        """时间窗口下拉框选择变化时的回调。

        将选择映射为秒数，传入 DataManager.set_time_window()。
        窗口选项: [30s, 120s, 600s, None(All)]。

        参数：
          index: 下拉框选中的索引（0-3）。
        """
        windows = [30, 120, 600, None]
        window = windows[index]
        self._data_manager.set_time_window(window)
        now = datetime.now()
        if window is not None:
            # 固定窗口：左侧锚定切换时刻，右侧 = 切换时刻 + 窗口
            self._data_manager.set_switch_time(now)
            x_min = now.timestamp()
            x_max = x_min + window
        else:
            # All：从启动原点到现在，右侧可随新数据增长
            self._data_manager.set_switch_time(None)
            x_min = self._data_manager.origin_time.timestamp()
            x_max = now.timestamp()
        # 先刷新曲线数据（应用新时间窗口过滤），再重置视图
        if not self._pause_button.isChecked():
            for tab in self._tabs:
                tab.refresh()
        for tab in self._tabs:
            pg = getattr(tab, '_plot_group', None)
            if pg is not None:
                pg.reset_views(x_min=x_min, x_max=x_max)

    def _on_pause_toggled(self, checked: bool) -> None:
        """暂停/恢复按钮切换时的回调。

        - 选中（checked=True）：按钮文本变为 "Resume"，
          此时 update_all() 被阻止，图表冻结不动。
        - 取消（checked=False）：按钮文本恢复 "Pause"，
          恢复正常实时刷新。

        参数：
          checked: 按钮是否被选中。
        """
        self._pause_button.setText("Resume" if checked else "Pause")

    def _on_clear(self) -> None:
        """清除所有历史数据并重置图表。

        调用 DataManager.clear() 清空环形缓冲，
        然后清空所有 PlotGroup 的曲线数据，
        最后刷新所有 Tab 显示空状态。
        """
        self._data_manager.clear()
        for tab in self._tabs:
            if hasattr(tab, '_plot_group') and tab._plot_group:
                tab._plot_group.clear()
            tab.refresh()


# =============================================================================
# 模块导出
# =============================================================================

__all__ = [
    "TabPlotManager",
    "PlotGroup",
    "OverviewTab",
    "CurveDef",
    "SubplotDef",
]
