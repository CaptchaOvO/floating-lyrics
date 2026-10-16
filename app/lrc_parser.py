"""LRC 歌词解析模块。

支持常见 LRC 时间戳格式，例如 ``[00:35.20]歌词内容``：

- 一行包含多个时间戳：``[00:01.00][00:05.00]同一行歌词``；
- 时间戳分隔符可以是 ``.`` 或 ``:``（如 ``[00:12:345]``）；
- 忽略 ``[ar:]``/``[ti:]``/``[al:]``/``[by:]`` 等非歌词元数据标签；
- 支持 ``[offset:±ms]`` 整体时间偏移（``new_time = time + offset``）；
- 无效时间戳（如秒数 >= 60）与无法解析的行会被跳过，不抛异常。

本模块不依赖 GUI，可独立单元测试。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# [分:秒] 或 [分:秒.毫秒]，分可以到 3 位，秒必须 < 60，毫秒 1~3 位
_TIMESTAMP_RE = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")

# [offset:500] / [offset:-120]
_OFFSET_RE = re.compile(r"\[offset:\s*([+-]?\d+)\s*\]", re.IGNORECASE)

# 元数据标签，仅用于识别，不产生歌词行
_META_KEYS = {"ar", "ti", "al", "by", "re", "ve", "length", "au", "song", "album", "artist"}


@dataclass(frozen=True)
class LyricLine:
    """一条带时间戳的歌词。"""

    time_ms: int
    text: str


def _fraction_to_ms(fraction: str) -> int:
    """把小数部分转换为毫秒：``3`` -> 300ms，``34`` -> 340ms，``345`` -> 345ms。"""
    padded = (fraction + "000")[:3]
    return int(padded)


def parse_lrc(text: str | None) -> list[LyricLine]:
    """解析 LRC 文本，返回按时间升序排列的歌词行。

    - 空输入/无有效时间戳时返回空列表（不抛异常）；
    - 同一时间戳出现多次时保留第一个；
    - 解析结果按 ``time_ms`` 稳定排序。
    """
    if not text:
        return []

    offset_ms = 0
    offset_match = _OFFSET_RE.search(text)
    if offset_match:
        try:
            offset_ms = int(offset_match.group(1))
        except ValueError:
            offset_ms = 0

    lines: list[LyricLine] = []
    seen_times: set[int] = set()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # 完全没有时间戳的行：元数据或普通文本，跳过
        matches = list(_TIMESTAMP_RE.finditer(line))
        if not matches:
            continue

        # 正文 = 最后一个时间戳之后的内容
        body = line[matches[-1].end():].strip()

        for m in matches:
            minutes = int(m.group(1))
            seconds = int(m.group(2))
            if seconds >= 60:
                # 无效时间戳，跳过该标签（同一行其他时间戳仍可解析）
                logger.debug("跳过无效时间戳: %s", m.group(0))
                continue
            fraction = m.group(3)
            ms = _fraction_to_ms(fraction) if fraction else 0
            time_ms = ((minutes * 60) + seconds) * 1000 + ms + offset_ms
            if time_ms < 0:
                continue
            if time_ms in seen_times:
                continue
            # 无正文的时间戳（空白行/纯间奏标记）不产生显示行
            if not body:
                continue
            seen_times.add(time_ms)
            lines.append(LyricLine(time_ms=time_ms, text=body))

    lines.sort(key=lambda line: line.time_ms)
    return lines


def parse_metadata(text: str | None) -> dict[str, str]:
    """提取 LRC 元数据标签（ar/ti/al 等），便于调试与展示。"""
    if not text:
        return {}
    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        m = re.match(r"\[([A-Za-z_]+):(.*)\]\s*$", line)
        if not m:
            continue
        key = m.group(1).lower()
        if key in _META_KEYS:
            result[key] = m.group(2).strip()
    return result
