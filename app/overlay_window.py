"""悬浮歌词窗口。

特性：

- 无边框 + 半透明背景（``WA_TranslucentBackground``），只绘制歌词文字；
- 始终置顶、不出现在任务栏（``Qt.Tool``）、不抢焦点（``WA_ShowWithoutActivating``
  + Win32 ``WS_EX_NOACTIVATE``）；
- 鼠标穿透通过 Win32 ``WS_EX_TRANSPARENT`` 动态开关；
- 非穿透模式下可拖动，拖动结束发出 :meth:`position_changed` 供保存位置；
- 文字支持：描边（QPainterPath + Pen）、伪阴影（加宽半透明描边）、当前行放大变色、
  行距、整体窗口透明度。

窗口尺寸随文字自动调整；窗口没有关闭按钮（关闭事件被忽略），只能经托盘退出。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from . import win32_utils
from .config import AppConfig
from .lrc_parser import LyricLine

logger = logging.getLogger(__name__)

PADDING_X = 28
PADDING_Y = 14
OTHER_SCALE = 0.68  # 非当前行相对当前行的字号比例


def text_x(align: str, width: float, advance: float) -> float:
    """一行文字的起始 x 坐标（居中/左对齐；绘制与测试共用的纯函数）。"""
    if align == "center":
        return max(0.0, (width - advance) / 2.0)
    return float(PADDING_X)


def _parse_color(text: str, fallback: str = "#FFFFFFFF") -> QColor:
    color = QColor(text)
    return color if color.isValid() else QColor(fallback)


class OverlayWindow(QWidget):
    """歌词悬浮窗。"""

    position_changed = Signal(int, int)  # x, y（逻辑坐标）

    def __init__(self, config: AppConfig) -> None:
        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool                 # 不在任务栏显示
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setWindowTitle("Floating Lyrics")

        self._config = config
        self._click_through = config.click_through
        self._visible_lines: list[LyricLine] = []
        self._current_index = -1
        self._status_text: str | None = None
        self._dragging = False
        self._drag_offset = QPoint()
        self._drag_moved = False
        self._allow_close = False

        self.setWindowOpacity(config.window_opacity)
        self.setCursor(
            Qt.CursorShape.ArrowCursor if config.click_through else Qt.CursorShape.OpenHandCursor
        )
        self._resize_to_content()
        self._apply_native_styles()

    # ---------------------------------------------------------- 数据输入

    def set_lyrics(self, lines: list[LyricLine], current_index: int) -> None:
        """设置当前可见歌词行与当前行下标（-1 表示尚未开始）。"""
        self._status_text = None
        if self._visible_lines != lines or self._current_index != current_index:
            self._visible_lines = list(lines)
            self._current_index = current_index
            self._resize_to_content()
            self.update()

    def set_status(self, text: str | None) -> None:
        """显示状态提示（如 “未找到歌词”、“等待媒体会话…”）。"""
        if self._status_text != text:
            self._status_text = text
            self._visible_lines = []
            self._current_index = -1
            self._resize_to_content()
            self.update()

    def clear(self) -> None:
        self.set_status(None)

    # ---------------------------------------------------------- 配置应用

    def apply_config(self, config: AppConfig) -> None:
        """应用新配置（设置对话框保存后调用）。"""
        old_topmost = self._config.always_on_top
        self._config = config
        self.setWindowOpacity(config.window_opacity)
        self._click_through = config.click_through
        self.setCursor(
            Qt.CursorShape.ArrowCursor if config.click_through else Qt.CursorShape.OpenHandCursor
        )
        if config.always_on_top != old_topmost:
            self.set_topmost(config.always_on_top)
        self._resize_to_content()
        self._apply_native_styles()
        self.update()

    # ---------------------------------------------------------- 窗口行为

    @property
    def click_through(self) -> bool:
        return self._click_through

    def set_click_through(self, enabled: bool) -> None:
        """开启/关闭鼠标穿透（WS_EX_TRANSPARENT）。"""
        if enabled == self._click_through:
            return
        self._click_through = enabled
        self.setCursor(
            Qt.CursorShape.ArrowCursor if enabled else Qt.CursorShape.OpenHandCursor
        )
        self._apply_native_styles()
        logger.info("鼠标穿透已%s", "开启" if enabled else "关闭")

    def set_topmost(self, enabled: bool) -> None:
        """开启/关闭窗口置顶。"""
        self._config.always_on_top = enabled
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, enabled)
        # setWindowFlag 会隐藏窗口，恢复原可见性
        if self.isVisible():
            self.show()
        self._apply_native_styles()
        win32_utils.set_topmost(int(self.winId()), enabled)
        logger.info("窗口置顶已%s", "开启" if enabled else "关闭")

    def show_overlay(self) -> None:
        if not self.isVisible():
            self.show()
        self._apply_native_styles()
        win32_utils.show_noactivate(int(self.winId()))

    def hide_overlay(self) -> None:
        if self.isVisible():
            self.hide()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """窗口不可关闭：忽略关闭事件（退出只能走托盘）。

        悬浮窗无边框且 WS_EX_NOACTIVATE，正常操作不会触发关闭；
        外部强行投递 WM_CLOSE 时也不销毁、不隐藏。

        注意：``QApplication.quit()`` 退出前会给所有可见顶层窗口发送 close 事件，
        若全部被忽略，退出流程会被中止（aboutToQuit 不触发、事件循环不退出）。
        因此托盘/程序内部退出前必须调用 :meth:`allow_close_on_quit` 放行。
        """
        if self._allow_close:
            event.accept()
            return
        event.ignore()

    def allow_close_on_quit(self) -> None:
        """退出流程中调用：放行随后到达的关闭事件。"""
        self._allow_close = True

    # ---------------------------------------------------------- 拖动

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if self._click_through or event.button() != Qt.MouseButton.LeftButton:
            event.ignore()
            return
        self._dragging = True
        self._drag_moved = False
        self._drag_offset = event.globalPosition().toPoint() - self.pos()
        self.setCursor(Qt.CursorShape.ClosedHandCursor)
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if not self._dragging:
            event.ignore()
            return
        new_pos = event.globalPosition().toPoint() - self._drag_offset
        if new_pos != self.pos():
            self._drag_moved = True
            self.move(new_pos)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if not self._dragging:
            event.ignore()
            return
        self._dragging = False
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        event.accept()
        if self._drag_moved:
            self.position_changed.emit(self.pos().x(), self.pos().y())

    # ---------------------------------------------------------- 绘制

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        y = float(PADDING_Y)
        for text, font, color in self._draw_items():
            metrics = QFontMetricsF(font)
            line_height = metrics.height() * self._config.line_spacing
            baseline = y + metrics.ascent() + (line_height - metrics.height()) / 2
            self._draw_text(painter, text, font, baseline, color)
            y += line_height

    def _draw_items(self) -> list[tuple[str, QFont, QColor]]:
        """排版结果（文本、字体、颜色），绘制与尺寸测量共用。"""
        cfg = self._config
        if self._status_text is not None:
            font = QFont(cfg.font_family)
            font.setPixelSize(max(9, int(cfg.font_size * 0.75)))
            return [(self._status_text, font, _parse_color(cfg.other_color))]

        items: list[tuple[str, QFont, QColor]] = []
        for i, line in enumerate(self._visible_lines):
            is_current = i == self._current_index
            size = cfg.font_size if is_current else max(9, int(cfg.font_size * OTHER_SCALE))
            font = QFont(cfg.font_family)
            font.setPixelSize(size)
            font.setWeight(QFont.Weight.DemiBold if is_current else QFont.Weight.Normal)
            color = _parse_color(cfg.current_color if is_current else cfg.other_color)
            items.append((line.text, font, color))
        return items

    def _draw_text(
        self,
        painter: QPainter,
        text: str,
        font: QFont,
        baseline: float,
        color: QColor,
    ) -> None:
        """用路径绘制：阴影 -> 描边 -> 填充。"""
        cfg = self._config
        advance = QFontMetricsF(font).horizontalAdvance(text)
        x = text_x(cfg.text_align, float(self.width()), advance)
        path = QPainterPath()
        path.addText(x, baseline, font, text)

        if cfg.shadow_enabled and cfg.shadow_blur > 0:
            shadow_pen = QPen(
                _parse_color(cfg.shadow_color, "#80000000"),
                float(cfg.shadow_blur),
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
            painter.setPen(shadow_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path.translated(1.5, 1.5))

        if cfg.outline_enabled and cfg.outline_width > 0:
            outline_pen = QPen(
                _parse_color(cfg.outline_color, "#B0000000"),
                float(cfg.outline_width),
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
            painter.setPen(outline_pen)
        else:
            painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawPath(path)

    # ---------------------------------------------------------- 尺寸/原生样式

    def _resize_to_content(self) -> None:
        items = self._draw_items()
        if not items:
            # 最小占位尺寸，避免 0 尺寸窗口问题
            self.resize(PADDING_X * 2, PADDING_Y * 2 + 24)
            return
        width = 0.0
        height = float(PADDING_Y)
        for text, font, _color in items:
            metrics = QFontMetricsF(font)
            width = max(width, metrics.horizontalAdvance(text))
            height += metrics.height() * self._config.line_spacing
        height += PADDING_Y
        self.resize(int(width + PADDING_X * 2 + self._config.outline_width + 4),
                    int(height + self._config.shadow_blur))

    def _apply_native_styles(self) -> None:
        try:
            hwnd = int(self.winId())
        except Exception:  # noqa: BLE001
            logger.debug("winId 获取失败（窗口尚未创建原生句柄）")
            return
        if not win32_utils.is_window(hwnd):
            return
        win32_utils.apply_overlay_styles(hwnd, self._click_through)
        win32_utils.set_topmost(hwnd, self._config.always_on_top)
