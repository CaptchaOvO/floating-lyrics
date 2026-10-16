"""歌词搜索/匹配逻辑测试（网络请求全部 mock，不访问真实服务）。"""

from __future__ import annotations

import json

from app.lyrics_provider import (
    LRCLIBProvider,
    LyricsCache,
    LyricsMatch,
    TrackInfo,
    normalize_text,
    pick_best_candidate,
    score_candidate,
    similarity,
)

TRACK = TrackInfo(title="Never Gonna Give You Up", artist="Rick Astley",
                  album="Whenever You Need Somebody", duration_ms=213_000)

SYNCED_LRC = "[00:01.00]We're no strangers to love\n[00:05.00]You know the rules and so do I"


def make_candidate(**kw):
    base = {
        "id": 42,
        "trackName": "Never Gonna Give You Up",
        "artistName": "Rick Astley",
        "albumName": "Whenever You Need Somebody",
        "duration": 213,
        "syncedLyrics": SYNCED_LRC,
        "plainLyrics": "we're no strangers to love...",
        "instrumental": False,
    }
    base.update(kw)
    return base


# --------------------------------------------------------------- 打分/归一化


def test_normalize_text():
    assert normalize_text("  Hello,  World! ") == "helloworld"
    assert normalize_text("Ｐａｒｔｙ（Ｍｉｘ）") == "partymix"
    assert normalize_text("") == ""


def test_similarity_basic():
    assert similarity("abc", "abc") == 1.0
    assert similarity("", "abc") == 0.0
    assert similarity("Never Gonna Give You Up", "never gonna give you up") == 1.0


def test_score_prefers_matching_track():
    good = score_candidate(make_candidate(), TRACK)
    bad = score_candidate(make_candidate(trackName="Completely Different Song"), TRACK)
    assert good >= 0.9
    assert bad == 0.0  # 标题差太远直接淘汰


def test_score_requires_synced_lyrics():
    assert score_candidate(make_candidate(syncedLyrics=""), TRACK) == 0.0
    assert score_candidate(make_candidate(syncedLyrics=None), TRACK) == 0.0


def test_score_rejects_instrumental():
    assert score_candidate(make_candidate(instrumental=True), TRACK) == 0.0


def test_score_duration_mismatch_penalized():
    same = score_candidate(make_candidate(duration=213), TRACK)
    far = score_candidate(make_candidate(duration=300), TRACK)
    assert far < same


def test_pick_best_candidate_threshold():
    best, score = pick_best_candidate([make_candidate()], TRACK)
    assert best is not None
    assert score >= 0.55

    weak, score = pick_best_candidate([make_candidate(trackName="Totally Other")], TRACK)
    assert weak is None


def test_pick_best_ignores_non_dict():
    best, _ = pick_best_candidate(["junk", None, make_candidate()], TRACK)
    assert best is not None


# --------------------------------------------------------------- 网络层 mock


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = dict(headers or {})
        self.url = "https://lrclib.net/api/test"

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    """按 URL 返回预设响应的假 requests.Session。"""

    def __init__(self, routes):
        self.headers: dict = {}
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None, timeout=None):
        assert timeout is not None, "所有请求必须设置超时"
        self.calls.append((url, dict(params or {})))
        for key, resp in self.routes.items():
            if key in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"未预期的 URL: {url}")


def test_lookup_exact_match():
    session = FakeSession({"/api/get": FakeResponse(200, make_candidate())})
    provider = LRCLIBProvider(timeout=5, session=session)
    match = provider.lookup(TRACK)
    assert match.status == "ok"
    assert match.synced_lrc == SYNCED_LRC
    assert match.match_score == 1.0


def test_lookup_search_fallback():
    session = FakeSession({
        "/api/get": FakeResponse(404, None),
        "/api/search": FakeResponse(200, [make_candidate()]),
    })
    provider = LRCLIBProvider(timeout=5, session=session)
    match = provider.lookup(TRACK)
    assert match.status == "ok"
    assert "[00:01.00]" in match.synced_lrc


def test_lookup_no_results():
    session = FakeSession({
        "/api/get": FakeResponse(404, None),
        "/api/search": FakeResponse(200, []),
    })
    provider = LRCLIBProvider(timeout=5, session=session)
    match = provider.lookup(TRACK)
    assert match.status == "none"
    assert match.synced_lrc is None
    assert "未找到" in match.message


def test_lookup_candidates_without_synced_lyrics():
    """候选只有纯文本歌词时不返回伪造时间戳。"""
    session = FakeSession({
        "/api/get": FakeResponse(404, None),
        "/api/search": FakeResponse(200, [make_candidate(syncedLyrics="", plainLyrics="abc")]),
    })
    provider = LRCLIBProvider(timeout=5, session=session)
    match = provider.lookup(TRACK)
    assert match.status == "none"


def test_lookup_network_timeout():
    import requests

    session = FakeSession({
        "/api/get": requests.exceptions.ConnectTimeout("timeout"),
    })
    provider = LRCLIBProvider(timeout=5, session=session)
    match = provider.lookup(TRACK)
    assert match.status == "error"
    assert "网络" in match.message


def test_lookup_rate_limited_on_get():
    """429 限流：返回 error + Retry-After，且不再打 /search（省请求）。"""
    session = FakeSession({
        "/api/get": FakeResponse(429, None, headers={"Retry-After": "30"}),
    })
    provider = LRCLIBProvider(timeout=5, session=session)
    match = provider.lookup(TRACK)
    assert match.status == "error"
    assert match.retry_after_s == 30.0
    assert "限流" in match.message
    assert len(session.calls) == 1  # 只打了 /api/get


def test_lookup_rate_limited_on_search():
    session = FakeSession({
        "/api/get": FakeResponse(404, None),
        "/api/search": FakeResponse(429, None, headers={"Retry-After": "不是数字"}),
    })
    provider = LRCLIBProvider(timeout=5, session=session)
    match = provider.lookup(TRACK)
    assert match.status == "error"
    assert match.retry_after_s == 0.0  # 非法头不致崩溃


def test_strong_identity_duration_gap_still_matches():
    """标题+歌手完全一致但时长差很大（版本不同）→ 仍应匹配而不是报无歌词。"""
    cand = make_candidate(duration=300, albumName="")  # TRACK 是 213s
    best, score = pick_best_candidate([cand], TRACK)
    assert best is not None
    assert score >= 0.50


def test_weak_identity_duration_gap_rejected():
    """标题/歌手都只有一半相似且时长差大 → 拒绝（避免错配）。"""
    cand = make_candidate(
        trackName="Lonely (Some Random Cover Version)",
        artistName="Somebody Else",
        duration=300,
    )
    best, _ = pick_best_candidate([cand], TRACK)
    assert best is None


def test_lookup_bad_json():
    session = FakeSession({
        "/api/get": FakeResponse(200, json.decoder.JSONDecodeError("Expecting value", "", 0)),
        "/api/search": FakeResponse(200, {"not": "a list"}),
    })
    provider = LRCLIBProvider(timeout=5, session=session)
    match = provider.lookup(TRACK)
    # /api/get 解析失败后继续走 /api/search；search 返回类型不对 → 数据错误
    assert match.status == "error"


def test_cache_roundtrip(tmp_path):
    cache = LyricsCache(directory=tmp_path)
    assert cache.get(TRACK) is None
    match = LyricsMatch(status="ok", synced_lrc=SYNCED_LRC)
    cache.put(TRACK, match)
    assert cache.get(TRACK) is match
    # 缓存键对大小写/空白不敏感（时长需一致）
    other = TrackInfo(title="  never gonna give you up ", artist="RICK ASTLEY",
                      duration_ms=213_000)
    assert cache.get(other) is match
    # 时长不同的曲目视作不同键
    assert cache.get(TrackInfo(title="Never Gonna Give You Up", artist="Rick Astley",
                               duration_ms=999_000)) is None


def test_cache_eviction(tmp_path):
    cache = LyricsCache(directory=tmp_path, max_size=2)
    for i in range(4):
        t = TrackInfo(title=f"song{i}", artist="a")
        cache.put(t, LyricsMatch(status="ok", synced_lrc="x"))
    assert len(cache.memory) == 2
