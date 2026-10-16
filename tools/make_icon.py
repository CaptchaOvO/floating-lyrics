"""生成 assets/icon.png（托盘图标）。

依赖 PySide6；运行：``python tools/make_icon.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QApplication  # noqa: E402


def main() -> int:
    app = QApplication(sys.argv)  # noqa: F841 - QPixmap 需要实例

    out = BASE / "assets" / "icon.png"
    out.parent.mkdir(parents=True, exist_ok=True)

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

    ok = pixmap.save(str(out), "PNG")
    print(f"saved={ok} path={out}")
    if not ok:
        return 1

    # 同步生成 icon.ico（PyInstaller --icon 需要 .ico）
    from make_icon_ico import main as ico_main

    ico_main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
