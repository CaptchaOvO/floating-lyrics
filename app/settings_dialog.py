"""设置对话框。

从 :class:`AppConfig` 读取初值，确定时写回同一个 config 对象（由控制器负责
落盘与应用）。颜色按钮弹出支持 Alpha 的颜色选择器。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFontComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
)

from .config import AppConfig

logger = logging.getLogger(__name__)


class ColorButton(QPushButton):
    """显示当前颜色、点击弹出取色器的按钮。"""

    def __init__(self, color_text: str, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(72, 26)
        self.set_color(color_text)
        self.clicked.connect(self._pick)

    def color_text(self) -> str:
        return self._color_text

    def set_color(self, color_text: str) -> None:
        self._color_text = color_text
        color = QColor(color_text)
        if not color.isValid():
            color = QColor("#FFFFFFFF")
            self._color_text = "#FFFFFFFF"
        # 文字对比色
        luminance = 0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue()
        text_color = "#000000" if luminance > 140 else "#FFFFFF"
        alpha = color.alpha()
        self.setStyleSheet(
            f"QPushButton {{ background-color: {color.name(QColor.NameFormat.HexArgb)};"
            f"color: {text_color}; border: 1px solid #666; border-radius: 4px; }}"
        )
        self.setText(f"A{alpha}" if alpha < 255 else "")

    def _pick(self) -> None:
        initial = QColor(self._color_text)
        chosen = QColorDialog.getColor(
            initial,
            self,
            "选择颜色",
            QColorDialog.ColorDialogOption.ShowAlphaChannel,
        )
        if chosen.isValid():
            self.set_color(chosen.name(QColor.NameFormat.HexArgb))


class SettingsDialog(QDialog):
    """设置窗口。"""

    def __init__(self, config: AppConfig, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置 - Floating Lyrics")
        self.setModal(True)
        self.setMinimumWidth(430)
        self._config = config

        root = QVBoxLayout(self)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        # 显示模式
        self.mode = QComboBox()
        self.mode.addItem("三行（上/当前/下）", "three_line")
        self.mode.addItem("仅当前行", "single_line")
        form.addRow("显示模式:", self.mode)

        # 对齐方式
        self.align = QComboBox()
        self.align.addItem("居中", "center")
        self.align.addItem("左对齐", "left")
        form.addRow("对齐方式:", self.align)

        # 字体
        font_row = QHBoxLayout()
        self.font_family = QFontComboBox()
        self.font_family.setCurrentFont(QFont(config.font_family))
        self.font_size = QSpinBox()
        self.font_size.setRange(9, 160)
        font_row.addWidget(self.font_family, 1)
        font_row.addWidget(self.font_size)
        form.addRow("字体/字号:", font_row)

        # 颜色
        self.color_current = ColorButton(config.current_color)
        self.color_other = ColorButton(config.other_color)
        form.addRow("当前行颜色:", self.color_current)
        form.addRow("其他行颜色:", self.color_other)

        # 行距
        self.line_spacing = QDoubleSpinBox()
        self.line_spacing.setRange(1.0, 2.5)
        self.line_spacing.setSingleStep(0.05)
        self.line_spacing.setDecimals(2)
        form.addRow("行距倍数:", self.line_spacing)

        # 歌词时间偏移（校准个别 LRC 时间轴偏差；正值=歌词延后显示）
        self.lyrics_offset = QDoubleSpinBox()
        self.lyrics_offset.setRange(-5.0, 5.0)
        self.lyrics_offset.setSingleStep(0.1)
        self.lyrics_offset.setDecimals(1)
        self.lyrics_offset.setSuffix(" s")
        form.addRow("歌词偏移:", self.lyrics_offset)

        # 整体透明度（滑条 + 百分比标签）
        opacity_row = QHBoxLayout()
        self.opacity = QSlider(Qt.Orientation.Horizontal)
        self.opacity.setRange(30, 100)
        self.opacity_label = QSpinBox()
        self.opacity_label.setRange(30, 100)
        self.opacity_label.setSuffix(" %")
        self.opacity_label.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        opacity_row.addWidget(self.opacity, 1)
        opacity_row.addWidget(self.opacity_label)
        self.opacity.valueChanged.connect(self.opacity_label.setValue)
        self.opacity_label.valueChanged.connect(self.opacity.setValue)
        form.addRow("窗口透明度:", opacity_row)

        root.addLayout(form)

        # 文字效果
        effect_box = QGroupBox("文字效果")
        effect_form = QFormLayout(effect_box)
        self.outline_enabled = QCheckBox("启用描边（提高透明背景下可读性）")
        self.outline_color = ColorButton(config.outline_color)
        self.outline_width = QSpinBox()
        self.outline_width.setRange(1, 8)
        outline_row = QHBoxLayout()
        outline_row.addWidget(self.outline_color, 1)
        outline_row.addWidget(self.outline_width)
        effect_form.addRow(self.outline_enabled)
        effect_form.addRow("描边颜色/宽度:", outline_row)

        self.shadow_enabled = QCheckBox("启用阴影")
        self.shadow_color = ColorButton(config.shadow_color)
        self.shadow_blur = QSpinBox()
        self.shadow_blur.setRange(1, 20)
        shadow_row = QHBoxLayout()
        shadow_row.addWidget(self.shadow_color, 1)
        shadow_row.addWidget(self.shadow_blur)
        effect_form.addRow(self.shadow_enabled)
        effect_form.addRow("阴影颜色/模糊:", shadow_row)
        root.addWidget(effect_box)

        # 行为
        behavior_box = QGroupBox("窗口与行为")
        behavior_form = QFormLayout(behavior_box)
        self.always_on_top = QCheckBox("窗口始终置顶")
        self.click_through = QCheckBox("鼠标穿透（点击穿到后方窗口）")
        self.hide_when_no_lyrics = QCheckBox("无歌词时自动隐藏窗口")
        self.hide_when_idle = QCheckBox("无媒体会话时隐藏窗口")
        for cb in (self.always_on_top, self.click_through,
                   self.hide_when_no_lyrics, self.hide_when_idle):
            behavior_form.addRow(cb)
        self.http_timeout = QSpinBox()
        self.http_timeout.setRange(3, 60)
        self.http_timeout.setSuffix(" s")
        behavior_form.addRow("网络超时:", self.http_timeout)
        root.addWidget(behavior_box)

        # 按钮
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._load(config)

    # ------------------------------------------------------------- 数据

    def _load(self, cfg: AppConfig) -> None:
        idx = self.mode.findData(cfg.display_mode)
        self.mode.setCurrentIndex(max(0, idx))
        idx = self.align.findData(cfg.text_align)
        self.align.setCurrentIndex(max(0, idx))
        self.font_size.setValue(cfg.font_size)
        self.line_spacing.setValue(cfg.line_spacing)
        self.lyrics_offset.setValue(round(cfg.lyrics_offset_ms / 1000.0, 3))
        self.opacity.setValue(int(round(cfg.window_opacity * 100)))
        self.outline_enabled.setChecked(cfg.outline_enabled)
        self.outline_width.setValue(cfg.outline_width)
        self.shadow_enabled.setChecked(cfg.shadow_enabled)
        self.shadow_blur.setValue(max(1, cfg.shadow_blur))
        self.always_on_top.setChecked(cfg.always_on_top)
        self.click_through.setChecked(cfg.click_through)
        self.hide_when_no_lyrics.setChecked(cfg.hide_when_no_lyrics)
        self.hide_when_idle.setChecked(cfg.hide_when_idle)
        self.http_timeout.setValue(cfg.http_timeout)

    def apply_to(self, cfg: AppConfig) -> None:
        """把对话框当前值写入 ``cfg``。"""
        cfg.display_mode = self.mode.currentData()
        cfg.text_align = self.align.currentData()
        cfg.font_family = self.font_family.currentFont().family()
        cfg.font_size = self.font_size.value()
        cfg.current_color = self.color_current.color_text()
        cfg.other_color = self.color_other.color_text()
        cfg.line_spacing = round(self.line_spacing.value(), 2)
        cfg.lyrics_offset_ms = int(round(self.lyrics_offset.value() * 1000))
        cfg.window_opacity = round(self.opacity.value() / 100.0, 2)
        cfg.outline_enabled = self.outline_enabled.isChecked()
        cfg.outline_color = self.outline_color.color_text()
        cfg.outline_width = self.outline_width.value()
        cfg.shadow_enabled = self.shadow_enabled.isChecked()
        cfg.shadow_color = self.shadow_color.color_text()
        cfg.shadow_blur = self.shadow_blur.value()
        cfg.always_on_top = self.always_on_top.isChecked()
        cfg.click_through = self.click_through.isChecked()
        cfg.hide_when_no_lyrics = self.hide_when_no_lyrics.isChecked()
        cfg.hide_when_idle = self.hide_when_idle.isChecked()
        cfg.http_timeout = self.http_timeout.value()
        cfg.validate()
