"""歌词来源模块。

抽象 :class:`LyricsProvider` 便于未来扩展其他歌词服务；默认实现为 LRCLIB
（https://lrclib.net，公开逐行歌词 API）。

要点：

- 所有网络请求都有超时，且不感知 GUI（在工作线程调用）；
- 匹配逻辑是纯函数（:func:`score_candidate` / :func:`pick_best_candidate`），可单元测试；
- 只返回服务端真实提供的同步歌词，绝不伪造时间戳；
- 网络异常、无匹配、数据格式异常分别用不同状态返回，不抛裸异常。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Protocol

import requests

from .config import config_dir

logger = logging.getLogger(__name__)

LRCLIB_BASE = "https://lrclib.net/api"
USER_AGENT = "FloatingLyrics/0.1 (desktop lyric overlay; local project)"


class LyricsProviderError(Exception):
    """歌词源基础异常。"""


class LyricsNetworkError(LyricsProviderError):
    """网络超时/连接失败/服务不可用。"""


class LyricsDataError(LyricsProviderError):
    """返回数据格式异常。"""


@dataclass(frozen=True)
class LyricsMatch:
    """一次匹配结果。"""

    status: str  # ok / none / error
    synced_lrc: str | None = None
    message: str = ""
    match_score: float = 0.0
    candidate_id: int | None = None
    source: str = "lrclib"
    # 429 限流时服务端 Retry-After 秒数（0 表示未限流/未提供）
    retry_after_s: float = 0.0


@dataclass
class TrackInfo:
    """用于查询的歌曲信息。"""

    title: str
    artist: str
    album: str = ""
    duration_ms: int = 0

    @property
    def duration_s(self) -> int:
        return int(round(self.duration_ms / 1000)) if self.duration_ms > 0 else 0


class LyricsProvider(Protocol):
    """歌词来源接口。"""

    def lookup(self, track: TrackInfo) -> LyricsMatch:  # pragma: no cover - 协议
        ...


# ---------------------------------------------------------------- 匹配打分（纯函数）

_PUNCT_RE = re.compile(r"[\s\(\)\[\]【】「」·,，.。!！?？:：;；'\"“”‘’\-_—~〜/\\]+")
_FEAT_RE = re.compile(r"\b(feat|ft|featuring|with|prod)\b.*$", re.IGNORECASE)


def normalize_text(text: str) -> str:
    """归一化文本用于比较：NFKC 全半角统一、去标点、小写、去 feat 尾巴。"""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = _FEAT_RE.sub("", text)
    text = _PUNCT_RE.sub("", text)
    return text.lower()


def similarity(a: str, b: str) -> float:
    """归一化后的序列相似度（0~1）。"""
    na, nb = normalize_text(a), normalize_text(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def score_candidate(
    candidate: dict[str, Any],
    track: TrackInfo,
) -> float:
    """给一个 LRCLIB 候选结果打分（0~1 越高越好），无同步歌词返回 0。

    打分维度：标题相似度（权重最高）、歌手、专辑、时长差、是否含同步歌词。
    """
    synced = candidate.get("syncedLyrics")
    if not isinstance(synced, str) or not synced.strip():
        return 0.0

    title = str(candidate.get("trackName") or candidate.get("title") or "")
    artist = str(candidate.get("artistName") or "")
    album = str(candidate.get("albumName") or "")
    if not title or not normalize_text(title):
        return 0.0

    title_sim = similarity(title, track.title)
    if title_sim < 0.45:
        return 0.0  # 标题差太远，直接淘汰（避免同名误配）

    artist_sim = similarity(artist, track.artist) if track.artist else 0.5
    album_sim = similarity(album, track.album) if track.album else 0.5

    score = 0.55 * title_sim + 0.35 * artist_sim
    if track.album:
        score += 0.10 * album_sim

    # 时长匹配加分（LRCLIB duration 单位为秒）；差太多说明版本不同（Live/剪辑），轻度扣分
    duration = candidate.get("duration")
    if track.duration_s > 0 and isinstance(duration, (int, float)) and duration:
        diff = abs(int(duration) - track.duration_s)
        if diff <= 3:
            score += 0.10
        elif diff <= 8:
            score += 0.05
        else:
            score -= 0.10

    # 纯器乐/无歌词标记的候选直接淘汰
    if candidate.get("instrumental"):
        return 0.0

    return max(0.0, min(1.0, score))


def pick_best_candidate(
    candidates: list[dict[str, Any]],
    track: TrackInfo,
    threshold: float = 0.55,
) -> tuple[dict[str, Any] | None, float]:
    """从候选列表挑最佳结果。返回 ``(候选, 分数)``；无合格者返回 ``(None, 0)``。

    标题+歌手都近乎完全一致时放宽阈值（时长差异不应导致“明明有歌词却报没有”）。
    """
    best: dict[str, Any] | None = None
    best_score = 0.0
    best_strong = False
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        s = score_candidate(cand, track)
        if s > best_score:
            best, best_score = cand, s
    if best is None:
        return None, 0.0

    # “强标识”判断：标题、歌手都 >= 0.92 视为同一首歌的可靠匹配
    title_sim = similarity(
        str(best.get("trackName") or best.get("title") or ""), track.title
    )
    artist_sim = similarity(str(best.get("artistName") or ""), track.artist) if track.artist else 0.0
    best_strong = title_sim >= 0.92 and artist_sim >= 0.92
    effective_threshold = 0.50 if best_strong else threshold

    if best_score < effective_threshold:
        return None, best_score
    return best, best_score


# ---------------------------------------------------------------- LRCLIB 实现


class LRCLIBProvider:
    """LRCLIB 歌词源。

    查询策略：

    1. 先用 ``/api/get`` 精确查询（服务端按 标题/歌手/专辑/时长 匹配）；
    2. 失败则用 ``/api/search`` 搜索，再本地打分挑选；
    3. 全部失败返回 ``status=none``；网络失败返回 ``status=error``。
    """

    name = "lrclib"

    def __init__(self, timeout: float = 10.0, session: requests.Session | None = None) -> None:
        self.timeout = timeout
        self._session = session or requests.Session()
        self._session.headers.setdefault("User-Agent", USER_AGENT)

    def lookup(self, track: TrackInfo) -> LyricsMatch:
        try:
            return self._lookup(track)
        except requests.RequestException as exc:
            logger.warning("歌词服务网络异常: %s", exc)
            return LyricsMatch(status="error", message=f"网络错误：{exc.__class__.__name__}")
        except LyricsDataError as exc:
            logger.warning("歌词服务数据异常: %s", exc)
            return LyricsMatch(status="error", message=f"数据异常：{exc}")

    # -- 内部 --

    @staticmethod
    def _rate_limited(resp: requests.Response) -> LyricsMatch | None:
        """429 限流 → 返回带 Retry-After 的错误匹配；非限流返回 None。"""
        if resp.status_code != 429:
            return None
        retry_after = 0.0
        try:
            retry_after = float(resp.headers.get("Retry-After", "0") or 0)
        except (TypeError, ValueError):
            retry_after = 0.0
        retry_after = max(0.0, min(retry_after, 600.0))
        wait = f"，{int(retry_after)}s 后自动重试" if retry_after > 0 else ""
        logger.warning("歌词服务限流 (HTTP 429, %s), Retry-After=%s", resp.url, retry_after)
        return LyricsMatch(
            status="error",
            message=f"歌词服务限流（HTTP 429）{wait}",
            retry_after_s=retry_after,
        )

    def _lookup(self, track: TrackInfo) -> LyricsMatch:
        params: dict[str, Any] = {
            "artist_name": track.artist,
            "track_name": track.title,
        }
        if track.album:
            params["album_name"] = track.album
        if track.duration_s > 0:
            params["duration"] = track.duration_s

        # 1) 精确查询（数据异常/服务端错误只记录，继续降级到搜索；429 限流直接报错）
        try:
            resp = self._session.get(f"{LRCLIB_BASE}/get", params=params, timeout=self.timeout)
            limited = self._rate_limited(resp)
            if limited is not None:
                return limited
            if resp.status_code == 200:
                try:
                    data = self._require_dict(resp)
                except LyricsDataError as exc:
                    logger.warning("/api/get 数据异常，降级为搜索: %s", exc)
                else:
                    synced = data.get("syncedLyrics")
                    if isinstance(synced, str) and synced.strip():
                        return LyricsMatch(
                            status="ok",
                            synced_lrc=synced,
                            message="精确匹配",
                            match_score=1.0,
                            candidate_id=data.get("id") if isinstance(data.get("id"), int) else None,
                        )
            elif resp.status_code not in (400, 404):
                logger.warning("/api/get 返回 HTTP %d，降级为搜索", resp.status_code)
        except requests.RequestException:
            raise  # 网络层失败：没有继续搜索的意义，直接按网络错误处理
        except ValueError as exc:
            logger.warning("/api/get JSON 异常，降级为搜索: %s", exc)

        # 2) 模糊搜索
        search_params: dict[str, Any] = {"track_name": track.title, "artist_name": track.artist}
        logger.info("歌词查询: artist=%r title=%r album=%r dur=%ds",
                    track.artist, track.title, track.album, track.duration_s)
        resp = self._session.get(f"{LRCLIB_BASE}/search", params=search_params, timeout=self.timeout)
        limited = self._rate_limited(resp)
        if limited is not None:
            return limited
        if resp.status_code != 200:
            raise LyricsDataError(f"/search HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise LyricsDataError(f"/search JSON 解析失败: {exc}") from exc
        if not isinstance(data, list):
            raise LyricsDataError("/search 返回不是列表")
        logger.info("搜索到 %d 个候选", len(data))

        # 3) 本地打分
        best, score = pick_best_candidate(data, track)
        if best is None:
            logger.info("未找到匹配歌词: %s - %s (best=%.2f)", track.artist, track.title, score)
            return LyricsMatch(status="none", message="未找到匹配的同步歌词", match_score=score)
        synced = best.get("syncedLyrics")
        if not isinstance(synced, str) or not synced.strip():
            return LyricsMatch(status="none", message="候选结果没有同步歌词", match_score=score)
        cid = best.get("id")
        return LyricsMatch(
            status="ok",
            synced_lrc=synced,
            message="搜索匹配",
            match_score=score,
            candidate_id=cid if isinstance(cid, int) else None,
        )

    @staticmethod
    def _require_dict(resp: requests.Response) -> dict[str, Any]:
        try:
            data = resp.json()
        except ValueError as exc:
            raise LyricsDataError(f"JSON 解析失败: {exc}") from exc
        if not isinstance(data, dict):
            raise LyricsDataError("返回数据不是对象")
        return data


# ---------------------------------------------------------------- 本地缓存

CACHE_MAX_ENTRIES = 500  # 磁盘缓存上限（超出按修改时间淘汰最旧）


@dataclass
class LyricsCache:
    """歌词缓存：进程内内存 + 磁盘持久化。

    - 键：归一化 ``歌手|标题|时长``（跨会话稳定，大小写/标点/空格差异不影响命中）；
    - 只缓存成功的同步歌词；重启后从磁盘恢复，同一首歌不再重复访问网络；
    - 磁盘读写失败静默降级（不影响主流程）；``directory`` 缺省为
      ``config_dir()/lyrics_cache``，测试传临时目录即可隔离。
    """

    memory: dict[str, LyricsMatch] = field(default_factory=dict)
    max_size: int = 128
    directory: Path | None = None
    _disk_ready: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if self.directory is None:
            self.directory = config_dir() / "lyrics_cache"
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._disk_ready = True
        except OSError:
            logger.warning("歌词缓存目录不可用，退化为纯内存缓存: %s", self.directory)
            self._disk_ready = False

    @staticmethod
    def key(track: TrackInfo) -> str:
        return f"{normalize_text(track.artist)}|{normalize_text(track.title)}|{track.duration_s}"

    def get(self, track: TrackInfo) -> LyricsMatch | None:
        key = self.key(track)
        hit = self.memory.get(key)
        if hit is not None:
            return hit
        hit = self._load_disk(key)
        if hit is not None:
            self.memory[key] = hit
        return hit

    def put(self, track: TrackInfo, match: LyricsMatch) -> None:
        if match.status != "ok" or not match.synced_lrc:
            return  # 只缓存成功的同步歌词
        key = self.key(track)
        self.memory[key] = match
        while len(self.memory) > self.max_size:
            self.memory.pop(next(iter(self.memory)))
        self._save_disk(key, track, match)

    def invalidate(self, track: TrackInfo) -> None:
        """删除指定曲目的内存与磁盘缓存（托盘“重新获取歌词”时调用）。"""
        key = self.key(track)
        self.memory.pop(key, None)
        if self._disk_ready:
            try:
                self._path(key).unlink(missing_ok=True)
            except OSError:
                logger.debug("缓存文件删除失败，忽略", exc_info=True)

    def clear(self) -> int:
        """清空全部缓存，返回清除的条目数（磁盘优先，内存兜底）。"""
        self.memory.clear()
        if not self._disk_ready:
            return 0
        removed = 0
        try:
            for f in self.directory.glob("*.json"):
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
        except OSError:
            logger.debug("清空缓存目录失败，忽略", exc_info=True)
        return removed

    # -- 磁盘读写（失败均静默降级） --

    def _path(self, key: str) -> Path:
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
        assert self.directory is not None
        return self.directory / f"{digest}.json"

    def _load_disk(self, key: str) -> LyricsMatch | None:
        if not self._disk_ready:
            return None
        try:
            raw = self._path(key).read_text(encoding="utf-8")
            data = json.loads(raw)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            logger.debug("缓存文件读取失败，忽略 (key=%s)", key)
            return None
        if not isinstance(data, dict) or not isinstance(data.get("synced_lrc"), str):
            return None
        score = data.get("match_score")
        cid = data.get("candidate_id")
        return LyricsMatch(
            status="ok",
            synced_lrc=data["synced_lrc"],
            message="本地缓存",
            match_score=float(score) if isinstance(score, (int, float)) else 0.0,
            candidate_id=cid if isinstance(cid, int) else None,
            source=str(data.get("source") or "lrclib"),
        )

    def _save_disk(self, key: str, track: TrackInfo, match: LyricsMatch) -> None:
        if not self._disk_ready:
            return
        payload = {
            "key": key,
            "artist": track.artist,
            "title": track.title,
            "album": track.album,
            "duration_s": track.duration_s,
            "synced_lrc": match.synced_lrc,
            "match_score": match.match_score,
            "candidate_id": match.candidate_id,
            "source": match.source,
            "saved_at": int(time.time()),
        }
        try:
            self._path(key).write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            logger.debug("缓存文件写入失败，忽略 (key=%s)", key)
            return
        self._prune()

    def _prune(self) -> None:
        """磁盘条目超过上限时按修改时间删除最旧的。"""
        if not self._disk_ready:
            return
        try:
            files = sorted(self.directory.glob("*.json"), key=lambda p: p.stat().st_mtime)
        except OSError:
            return
        for f in files[: max(0, len(files) - CACHE_MAX_ENTRIES)]:
            try:
                f.unlink()
            except OSError:
                pass
