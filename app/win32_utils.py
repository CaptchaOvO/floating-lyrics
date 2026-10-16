"""Win32 窗口样式辅助（ctypes）。

实现悬浮歌词窗口所需的原生能力：

- 扩展样式：``WS_EX_TRANSPARENT``（鼠标穿透）、``WS_EX_TOOLWINDOW``（不在任务栏显示）、
  ``WS_EX_NOACTIVATE``（不抢焦点）、``WS_EX_LAYERED``（Qt 半透明窗口自带）；
- 置顶/取消置顶：``SetWindowPos(HWND_TOPMOST / HWND_NOTOPMOST)``；
- 不激活显示：``ShowWindow(SW_SHOWNOACTIVATE)``。

仅在 Windows 上可用；其他平台导入本模块会抛出 ``OSError``（本项目仅支持 Windows）。
"""

from __future__ import annotations

import ctypes
import logging
import sys

logger = logging.getLogger(__name__)

if not sys.platform.startswith("win"):
    raise OSError("win32_utils 仅支持 Windows 平台")

user32 = ctypes.windll.user32  # type: ignore[attr-defined]

GWL_EXSTYLE = -20

WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOPMOST = 0x00000008

HWND_TOPMOST = -1
HWND_NOTOPMOST = -2

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010

SW_HIDE = 0
SW_SHOWNOACTIVATE = 4

# GetWindowLongW / SetWindowLongW 在 64 位下要用 Ptr 版本
_get_long = user32.GetWindowLongW
_set_long = user32.SetWindowLongW
if ctypes.sizeof(ctypes.c_void_p) == 8:
    _get_long = user32.GetWindowLongPtrW
    _set_long = user32.SetWindowLongPtrW

_get_long.restype = ctypes.c_longlong
_set_long.restype = ctypes.c_longlong
_set_long.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_longlong]


def get_exstyle(hwnd: int) -> int:
    """读取窗口扩展样式；失败返回 0 并记录日志。"""
    value = _get_long(ctypes.c_void_p(hwnd), GWL_EXSTYLE)
    if value == 0:
        logger.debug("GetWindowLongPtrW 返回 0 (hwnd=%s, err=%s)", hwnd, ctypes.get_last_error())
    return int(value)


def set_exstyle(hwnd: int, style: int) -> None:
    """写入窗口扩展样式。"""
    _set_long(ctypes.c_void_p(hwnd), GWL_EXSTYLE, style)


def apply_overlay_styles(hwnd: int, click_through: bool) -> None:
    """应用悬浮窗公共样式并按需开启鼠标穿透。

    保留 WS_EX_LAYERED / WS_EX_TOOLWINDOW / WS_EX_NOACTIVATE，
    仅切换 WS_EX_TRANSPARENT。
    """
    style = get_exstyle(hwnd)
    style |= WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
    if click_through:
        style |= WS_EX_TRANSPARENT
    else:
        style &= ~WS_EX_TRANSPARENT
    set_exstyle(hwnd, style)


def set_topmost(hwnd: int, topmost: bool) -> None:
    """设置（取消）置顶，不激活窗口。"""
    user32.SetWindowPos(
        ctypes.c_void_p(hwnd),
        ctypes.c_void_p(HWND_TOPMOST if topmost else HWND_NOTOPMOST),
        0, 0, 0, 0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
    )


def show_noactivate(hwnd: int) -> None:
    """以不激活的方式显示窗口。"""
    user32.ShowWindow(ctypes.c_void_p(hwnd), SW_SHOWNOACTIVATE)


def hide_window(hwnd: int) -> None:
    user32.ShowWindow(ctypes.c_void_p(hwnd), SW_HIDE)


def is_window(hwnd: int) -> bool:
    return bool(user32.IsWindow(ctypes.c_void_p(hwnd)))
