"""歌词逐行同步模块。

给定排序后的歌词时间戳与当前播放进度，计算“当前歌词行”：

- 当前行 = 时间戳不晚于播放位置的最后一条有效歌词；
- 播放进度回退时当前行随之回退（纯二分查找，天然支持）；
- 播放位置早于第一条歌词时返回 ``-1``（尚未开始）。

本模块不依赖 GUI，可独立单元测试。
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence

from .lrc_parser import LyricLine


def find_current_index(times: Sequence[int], position_ms: int) -> int:
    """返回 ``times`` 中最后一个 ``<= position_ms`` 的下标，否则 ``-1``。

    ``times`` 必须已升序排列（``parse_lrc`` 的输出满足该条件）。
    """
    if not times or position_ms < 0:
        return -1
    # bisect_right 返回第一个 > position 的下标，减一即为最后一个 <= position 的下标
    return bisect_right(times, position_ms) - 1


def current_line(lines: Sequence[LyricLine], position_ms: int) -> LyricLine | None:
    """返回当前位置对应的歌词行；尚未到达第一行时返回 ``None``。"""
    idx = find_current_index([line.time_ms for line in lines], position_ms)
    if idx < 0:
        return None
    return lines[idx]


def build_display(
    lines: Sequence[LyricLine],
    position_ms: int,
    mode: str = "three_line",
) -> tuple[int, list[LyricLine]]:
    """构造待显示的歌词行。

    返回 ``(current_index, visible_lines)``：

    - ``three_line`` 模式显示 上一行/当前行/下一行（靠近开头或结尾时自动减少）；
    - ``single_line`` 模式只显示当前行；
    - 尚未到达第一行歌词时 ``current_index == -1``，``three_line`` 显示第一行做预告，
      ``single_line`` 不显示。
    """
    if not lines:
        return -1, []

    idx = find_current_index([line.time_ms for line in lines], position_ms)

    if mode == "single_line":
        if idx < 0:
            return -1, []
        return idx, [lines[idx]]

    # three_line
    if idx < 0:
        return -1, [lines[0]]
    start = max(0, idx - 1)
    end = min(len(lines), idx + 2)
    return idx, list(lines[start:end])


def visible_index(absolute_index: int, lines: Sequence[LyricLine], visible: Sequence[LyricLine]) -> int:
    """把全量歌词中的**绝对**下标换算成可见窗口内的**相对**下标。

    ``build_display`` 返回的 ``current_index`` 是全量 ``lines`` 中的绝对下标，
    而悬浮窗按 ``visible`` 列表内的位置（``0..len(visible)-1``）高亮当前行，
    两者只在窗口开头对齐（绝对下标 0/1），之后会错位。本函数按对象同一性
    在 ``visible`` 中定位，找不到（如 -1 尚未开始）返回 -1。

    依赖：``visible`` 必须由 ``build_display`` 从同一 ``lines`` 切片生成
    （元素是同一批 ``LyricLine`` 对象）。
    """
    if absolute_index < 0 or absolute_index >= len(lines) or not visible:
        return -1
    target = lines[absolute_index]
    for i, line in enumerate(visible):
        if line is target:
            return i
    return -1
