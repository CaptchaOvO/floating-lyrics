"""系统托盘图标与菜单。

菜单项（第一版）：

- 显示/隐藏歌词窗口（可勾选）
- 鼠标穿透（可勾选，默认开启）
- 窗口置顶（可勾选）
- 重新获取歌词
- 清除歌词缓存
- 设置…
- 退出

所有交互都通过信号转发给控制器，托盘本身不持有业务逻辑。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon, QWidget

logger = logging.getLogger(__name__)


class TrayIcon(QObject):
    """托盘图标封装。"""

    show_hide_requested = Signal()
    toggle_click_through = Signal()
    toggle_topmost = Signal()
    reload_lyrics = Signal()
    clear_cache_requested = Signal()
    open_settings = Signal()
    quit_requested = Signal()

    def __init__(self, icon: QIcon, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._tray = QSystemTrayIcon(icon, self)
        self._tray.setToolTip("Floating Lyrics 悬浮歌词")

        menu = QMenu()
        self._act_show = QAction("隐藏歌词窗口", self)
        self._act_show.triggered.connect(self.show_hide_requested.emit)

        self._act_through = QAction("鼠标穿透", self)
        self._act_through.setCheckable(True)
        self._act_through.toggled.connect(self._on_through_toggled)

        self._act_topmost = QAction("窗口置顶", self)
        self._act_topmost.setCheckable(True)
        self._act_topmost.toggled.connect(self._on_topmost_toggled)

        self._act_reload = QAction("重新获取歌词", self)
        self._act_reload.triggered.connect(self.reload_lyrics.emit)

        self._act_clear_cache = QAction("清除歌词缓存", self)
        self._act_clear_cache.triggered.connect(self.clear_cache_requested.emit)

        self._act_settings = QAction("设置…", self)
        self._act_settings.triggered.connect(self.open_settings.emit)

        self._act_quit = QAction("退出", self)
        self._act_quit.triggered.connect(self.quit_requested.emit)

        menu.addAction(self._act_show)
        menu.addSeparator()
        menu.addAction(self._act_through)
        menu.addAction(self._act_topmost)
        menu.addSeparator()
        menu.addAction(self._act_reload)
        menu.addAction(self._act_clear_cache)
        menu.addAction(self._act_settings)
        menu.addSeparator()
        menu.addAction(self._act_quit)
        menu.aboutToShow.connect(self._on_menu_about_to_show)
        self._menu = menu

        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_activated)

        # 防止程序化更新勾选状态时触发信号
        self._syncing = False

    # ------------------------------------------------------------- 展示

    def show(self) -> None:
        self._tray.show()

    def hide(self) -> None:
        self._tray.hide()

    def is_visible(self) -> bool:
        return self._tray.isVisible()

    def show_message(self, title: str, message: str, ms: int = 4000) -> None:
        if self._tray.isVisible():
            self._tray.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, ms)

    # ------------------------------------------------------------- 状态同步

    def set_states(self, *, window_visible: bool, click_through: bool, topmost: bool) -> None:
        """由控制器同步勾选状态（程序化更新，不触发信号）。"""
        self._syncing = True
        try:
            self._act_show.setText("隐藏歌词窗口" if window_visible else "显示歌词窗口")
            self._act_through.setChecked(click_through)
            self._act_topmost.setChecked(topmost)
        finally:
            self._syncing = False

    # ------------------------------------------------------------- 内部

    def _on_menu_about_to_show(self) -> None:
        pass  # 状态在变化时已同步，这里留作扩展

    def _on_through_toggled(self, checked: bool) -> None:
        if self._syncing:
            return
        self.toggle_click_through.emit()

    def _on_topmost_toggled(self, checked: bool) -> None:
        if self._syncing:
            return
        self.toggle_topmost.emit()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # 在 Windows 上单击/双击托盘图标 → 显示/隐藏窗口
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_hide_requested.emit()
