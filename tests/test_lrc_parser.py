"""LRC 解析单元测试。"""

from app.lrc_parser import LyricLine, parse_lrc, parse_metadata


def test_standard_timestamp():
    """§8.1 标准时间戳解析。"""
    lrc = "[00:35.20]第一句歌词\n[01:02.5]第二句歌词"
    lines = parse_lrc(lrc)
    assert lines == [
        LyricLine(35_200, "第一句歌词"),
        LyricLine(62_500, "第二句歌词"),
    ]


def test_timestamp_precision_variants():
    """小数位数：.x = 十分之一秒，.xx = 百分之一秒，.xxx = 毫秒。"""
    lrc = "[00:01.3]a\n[00:01.34]b\n[00:01.345]c"
    lines = parse_lrc(lrc)
    # .3 -> 300ms；.34 -> 340ms；.345 -> 345ms，时间不同所以都保留
    assert [ln.time_ms for ln in lines] == [1300, 1340, 1345]
    assert [ln.text for ln in lines] == ["a", "b", "c"]


def test_multiple_timestamps_one_line():
    """§8.2 一行多个时间戳。"""
    lrc = "[00:10.00][00:50.00]重复的副歌"
    lines = parse_lrc(lrc)
    assert lines == [LyricLine(10_000, "重复的副歌"), LyricLine(50_000, "重复的副歌")]


def test_metadata_ignored():
    """§8.3 非歌词元数据处理。"""
    lrc = (
        "[ar:某歌手]\n"
        "[ti:某标题]\n"
        "[al:某专辑]\n"
        "[by:某字幕组]\n"
        "[length:03:45]\n"
        "[re:lrclib.net]\n"
        "[offset:+500]\n"
        "[00:01.00]真歌词"
    )
    lines = parse_lrc(lrc)
    # 元数据不产生歌词行；offset:+500 使时间戳后移 500ms
    assert lines == [LyricLine(1_500, "真歌词")]
    meta = parse_metadata(lrc)
    assert meta["ar"] == "某歌手"
    assert meta["ti"] == "某标题"


def test_offset_negative():
    lrc = "[offset:-200]\n[00:10.00]行"
    assert parse_lrc(lrc)[0].time_ms == 9_800


def test_sorted_by_time():
    """§8.4 按时间排序（输入乱序）。"""
    lrc = "[00:30.00]c\n[00:10.00]a\n[00:20.00]b"
    assert [ln.text for ln in parse_lrc(lrc)] == ["a", "b", "c"]


def test_crlf_and_empty_lines():
    lrc = "[00:01.00]a\r\n\r\n[00:02.00]b\r\n"
    lines = parse_lrc(lrc)
    assert [ln.text for ln in lines] == ["a", "b"]


def test_invalid_timestamp_skipped():
    """无效时间戳（秒 >= 60）被跳过，同一行其它时间戳仍解析。"""
    lrc = "[00:75.00]坏的\n[00:12.00]好的"
    lines = parse_lrc(lrc)
    assert lines == [LyricLine(12_000, "好的")]


def test_empty_and_garbage_input():
    """§8.7 空歌词、异常数据不抛异常。"""
    assert parse_lrc(None) == []
    assert parse_lrc("") == []
    assert parse_lrc("没有任何时间戳的纯文本") == []
    assert parse_lrc("[not a timestamp]xxx") == []
    assert parse_lrc("[]\n[][]") == []


def test_blank_timestamp_lines_dropped():
    """只有时间戳没有正文的行不产生显示行。"""
    lrc = "[00:01.00]\n[00:02.00]有词\n[00:03.00]   "
    lines = parse_lrc(lrc)
    assert [ln.text for ln in lines] == ["有词"]


def test_duplicate_timestamp_keeps_first():
    lrc = "[00:05.00]第一次\n[00:05.00]第二次"
    lines = parse_lrc(lrc)
    assert lines == [LyricLine(5_000, "第一次")]


def test_colon_fraction_separator():
    """部分歌词用冒号分隔毫秒：[00:12:345]。"""
    lrc = "[00:12:345]行"
    assert parse_lrc(lrc)[0].time_ms == 12_345


def test_text_after_timestamp_preserved():
    """正文中的方括号文本（非时间戳）被保留。"""
    lrc = "[00:01.00]hello [chorus] world"
    assert parse_lrc(lrc)[0].text == "hello [chorus] world"
