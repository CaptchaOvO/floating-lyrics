"""悬浮窗纯函数测试（无需创建真实窗口）。"""

from __future__ import annotations

from app.overlay_window import PADDING_X, text_x


def test_text_x_left_align():
    assert text_x("left", 500, 200) == PADDING_X


def test_text_x_center_align():
    assert text_x("center", 500, 200) == 150.0


def test_text_x_center_wider_than_window_clamped():
    assert text_x("center", 100, 300) == 0.0
