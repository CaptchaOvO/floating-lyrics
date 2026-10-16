"""歌词逐行同步单元测试。"""

from app.lrc_parser import LyricLine, parse_lrc
from app.lyrics_sync import build_display, current_line, find_current_index, visible_index


def _lines() -> list[LyricLine]:
    return [
        LyricLine(0, "line0"),
        LyricLine(10_000, "line1"),
        LyricLine(20_000, "line2"),
        LyricLine(30_000, "line3"),
    ]


def test_find_current_index_basic():
    """§8.5 根据播放进度计算当前歌词行。"""
    times = [ln.time_ms for ln in _lines()]
    assert find_current_index(times, 0) == 0
    assert find_current_index(times, 5_000) == 0
    assert find_current_index(times, 10_000) == 1  # 时间戳等于进度 → 该行
    assert find_current_index(times, 19_999) == 1
    assert find_current_index(times, 20_000) == 2
    assert find_current_index(times, 999_999) == 3  # 超出最后一行 → 最后一行


def test_before_first_line():
    times = [ln.time_ms for ln in _lines()]
    assert find_current_index(times, -1) == -1
    assert find_current_index([], 1234) == -1
    # 第一条不是 0 秒的情况
    assert find_current_index([5_000, 10_000], 4_999) == -1


def test_rewind_updates_index():
    """§8.6 播放进度回退后当前歌词正确回退。"""
    times = [ln.time_ms for ln in _lines()]
    assert find_current_index(times, 35_000) == 3
    # 拖动进度条回到开头
    assert find_current_index(times, 2_000) == 0
    # 再次前进
    assert find_current_index(times, 25_000) == 2


def test_current_line_none_before_start():
    lines = _lines()
    assert current_line(lines, -500) is None
    assert current_line(lines, 0) is lines[0]
    assert current_line([], 100) is None


def test_build_display_three_line():
    lines = _lines()
    idx, visible = build_display(lines, 15_000, "three_line")
    assert idx == 1
    assert [ln.text for ln in visible] == ["line0", "line1", "line2"]


def test_build_display_three_line_edges():
    lines = _lines()
    # 开头
    _, visible = build_display(lines, 0, "three_line")
    assert [ln.text for ln in visible] == ["line0", "line1"]
    # 结尾
    _, visible = build_display(lines, 30_000, "three_line")
    assert [ln.text for ln in visible] == ["line2", "line3"]
    # 尚未开始 → 预告第一行
    idx, visible = build_display(lines, -1, "three_line")
    assert idx == -1
    assert [ln.text for ln in visible] == ["line0"]


def test_build_display_single_line():
    lines = _lines()
    idx, visible = build_display(lines, 15_000, "single_line")
    assert idx == 1
    assert [ln.text for ln in visible] == ["line1"]
    idx, visible = build_display(lines, -1, "single_line")
    assert idx == -1
    assert visible == []


def test_build_display_empty():
    assert build_display([], 100, "three_line") == (-1, [])
    assert build_display([], 100, "single_line") == (-1, [])


def test_pause_behavior_via_position():
    """暂停时进度不变 → 当前行不变（位置驱动天然满足）。"""
    lines = parse_lrc("[00:01.00]a\n[00:05.00]b\n[00:09.00]c")
    pos = 6_000
    assert current_line(lines, pos).text == "b"
    # 模拟暂停：位置保持 6s，多次计算结果一致
    for _ in range(5):
        assert current_line(lines, pos).text == "b"


def test_seek_back_then_forward():
    """拖动进度条：先倒退再前进，行号都正确。"""
    lines = parse_lrc("[00:00.00]a\n[00:10.00]b\n[00:20.00]c\n[00:30.00]d")
    times = [ln.time_ms for ln in lines]
    assert find_current_index(times, 25_000) == 2
    assert find_current_index(times, 5_000) == 0
    assert find_current_index(times, 31_000) == 3


def _many_lines(n: int = 8) -> list[LyricLine]:
    return [LyricLine(i * 2_000, f"line {i}") for i in range(n)]


def test_visible_index_three_line_mapping():
    """回归：悬浮窗高亮必须落在可见窗口内的“当前行”，不能用绝对下标直接比对。

    历史 bug：build_display 返回绝对下标，overlay 按可见列表位置高亮，
    绝对下标 >=2 时高亮错位（高亮下一行），>=3 时完全不高亮。
    """
    lines = _many_lines()
    # 4.5s → 绝对行 2；窗口 [line1, line2, line3] → 相对位置 1
    idx, visible = build_display(lines, 4_500, "three_line")
    assert idx == 2
    assert [ln.text for ln in visible] == ["line 1", "line 2", "line 3"]
    assert visible_index(idx, lines, visible) == 1
    assert visible[visible_index(idx, lines, visible)].text == "line 2"

    # 歌曲后段：11.5s → 绝对行 5；窗口 [line4, line5] → 相对位置 1
    idx, visible = build_display(lines, 11_500, "three_line")
    assert idx == 5
    assert visible_index(idx, lines, visible) == 1
    assert visible[visible_index(idx, lines, visible)].text == "line 5"

    # 开头：0s → 绝对行 0；窗口 [line0, line1] → 相对位置 0
    idx, visible = build_display(lines, 0, "three_line")
    assert idx == 0
    assert visible_index(idx, lines, visible) == 0


def test_visible_index_single_line_mapping():
    """single_line 只有一行，相对下标恒为 0（绝对下标会错位）。"""
    lines = _many_lines()
    idx, visible = build_display(lines, 4_500, "single_line")
    assert idx == 2
    assert visible_index(idx, lines, visible) == 0


def test_visible_index_not_started_and_bounds():
    """尚未开始（-1）或越界时返回 -1，悬浮窗不高亮任何行。"""
    lines = _many_lines(4)
    idx, visible = build_display(lines, -1, "three_line")
    assert idx == -1
    assert visible_index(idx, lines, visible) == -1
    assert visible_index(-1, lines, visible) == -1
    assert visible_index(4, lines, visible) == -1  # 越界
    assert visible_index(1, lines, []) == -1  # 空窗口
    assert build_display([], 100, "single_line") == (-1, [])
