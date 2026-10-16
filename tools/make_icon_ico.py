"""把 assets/icon.png 打包为 assets/icon.ico（PNG-in-ICO，无需 Pillow）。

ICO 容器格式：
  ICONDIR   (6B): reserved=0, type=1, count
  ICONDIRENTRY (16B/个): w, h, ... , size, offset
  数据区: 原样嵌入 PNG 字节（Vista+ 支持）
"""

import struct
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "assets"
PNG = ASSETS / "icon.png"
ICO = ASSETS / "icon.ico"


def png_size(data: bytes) -> tuple[int, int]:
    # IHDR 位于 PNG 头之后: 8B 签名 + 4B 长度 + "IHDR" + w(4) + h(4)
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    w, h = struct.unpack(">II", data[16:24])
    return w, h


def main() -> None:
    png = PNG.read_bytes()
    w, h = png_size(png)
    # ICO 目录里 0 表示 256，其余用实际尺寸（>255 的也写 0）
    entry_w = w if w < 256 else 0
    entry_h = h if h < 256 else 0
    header = struct.pack("<HHH", 0, 1, 1)  # reserved, type=icon, count=1
    entry = struct.pack(
        "<BBBBHHII",
        entry_w,
        entry_h,
        0,  # palette colors
        0,  # reserved
        1,  # color planes
        32,  # bits per pixel
        len(png),
        6 + 16,  # offset: header + 1 entry
    )
    ICO.write_bytes(header + entry + png)
    print(f"wrote {ICO} ({w}x{h} png, {ICO.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
