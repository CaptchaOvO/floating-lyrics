"""Floating Lyrics 入口。

启动顺序：日志 → 单实例锁 → QApplication → 配置 → 控制器 → 事件循环。

退出：只能通过托盘“退出”（悬浮窗 close 事件被忽略；托盘退出前会放行关闭事件，
避免 Qt 的退出前 closeAll 事件被忽略而中止退出）；``aboutToQuit`` 里做清理。
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

logger = logging.getLogger("floating_lyrics")

BASE_DIR = Path(__file__).resolve().parent


def setup_logging() -> None:
    """控制台 + 滚动文件日志（UTF-8）。"""
    log_dir = BASE_DIR / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        probe = log_dir / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError:
        # 项目目录不可写（如打包到 Program Files）时回退到用户目录
        from app.config import config_dir
        log_dir = config_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(name)s] %(message)s", "%H:%M:%S"
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "app.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s [%(name)s] %(message)s")
    )
    root.addHandler(file_handler)
    logger.info("日志目录: %s", log_dir)


def acquire_single_instance_lock():
    """单实例保护：避免出现两个互相覆盖的悬浮窗。返回锁对象或 None。"""
    from PySide6.QtCore import QLockFile

    from app.config import config_dir

    lock_path = config_dir() / "floating-lyrics.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(lock_path))
    lock.setStaleLockTime(30_000)
    if lock.tryLock(100):
        return lock
    logger.warning("检测到另一个实例正在运行，本实例退出（锁: %s）", lock_path)
    return None


def create_icon():
    """加载托盘图标；文件缺失时用 Qt 现场生成一个。"""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QIcon, QLinearGradient, QPainter, QPen, QPixmap

    icon_path = BASE_DIR / "assets" / "icon.png"
    if icon_path.exists():
        return QIcon(str(icon_path))

    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    gradient = QLinearGradient(0, 0, 64, 64)
    gradient.setColorAt(0.0, QColor("#4F8EF7"))
    gradient.setColorAt(1.0, QColor("#2B5FD9"))
    painter.setBrush(gradient)
    painter.setPen(QPen(QColor("#1B3F91"), 1))
    painter.drawRoundedRect(QRectF(2, 2, 60, 60), 14, 14)
    font = QFont("Microsoft YaHei UI")
    font.setPixelSize(34)
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(QColor("#FFFFFF"))
    painter.drawText(QRectF(0, 0, 64, 64), Qt.AlignmentFlag.AlignCenter, "词")
    painter.end()
    return QIcon(pixmap)


def main() -> int:
    setup_logging()

    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    app.setApplicationName("FloatingLyrics")
    app.setOrganizationName("FloatingLyrics")
    app.setQuitOnLastWindowClosed(False)  # 悬浮窗不可关闭，靠托盘退出

    lock = acquire_single_instance_lock()
    if lock is None:
        print("Floating Lyrics 已在运行，本实例退出。")
        return 1

    from app.config import ConfigStore
    from app.controller import AppController

    store = ConfigStore()
    config = store.load()
    logger.info(
        "配置加载完成: 模式=%s 字号=%d 穿透=%s 置顶=%s 位置=(%s, %s)",
        config.display_mode, config.font_size, config.click_through,
        config.always_on_top, config.window_x, config.window_y,
    )

    controller = AppController(config, store, create_icon())
    app.aboutToQuit.connect(controller.shutdown)
    controller.start()

    try:
        code = app.exec()
    finally:
        lock.unlock()
    logger.info("程序退出 code=%s", code)
    return code


if __name__ == "__main__":
    sys.exit(main())
