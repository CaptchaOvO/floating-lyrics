"""LyricsCache 磁盘持久化测试。"""

from __future__ import annotations

from app import lyrics_provider
from app.lyrics_provider import LyricsCache, LyricsMatch, TrackInfo


def _track(title: str = "Song", artist: str = "Artist", duration_ms: int = 200_000) -> TrackInfo:
    return TrackInfo(title=title, artist=artist, album="Album", duration_ms=duration_ms)


def _match(lrc: str = "[00:01.00]hello") -> LyricsMatch:
    return LyricsMatch(status="ok", synced_lrc=lrc, match_score=0.9, candidate_id=7)


def test_put_get_memory_and_disk(tmp_path):
    cache = LyricsCache(directory=tmp_path)
    track = _track()
    cache.put(track, _match())
    # 内存命中
    hit = cache.get(track)
    assert hit is not None and hit.synced_lrc == "[00:01.00]hello"
    # “重启”后内存为空 → 磁盘命中
    fresh = LyricsCache(directory=tmp_path)
    assert fresh.memory == {}
    hit = fresh.get(track)
    assert hit is not None
    assert hit.synced_lrc == "[00:01.00]hello"
    assert hit.message == "本地缓存"
    assert hit.candidate_id == 7
    assert hit.match_score == 0.9
    # 磁盘命中会回填内存
    assert fresh.memory


def test_key_normalized_stable(tmp_path):
    a = TrackInfo(title="Hello  World", artist="A.", duration_ms=100_000)
    b = TrackInfo(title="hello-world", artist="a", duration_ms=100_000)
    assert LyricsCache.key(a) == LyricsCache.key(b)


def test_only_ok_matches_cached(tmp_path):
    cache = LyricsCache(directory=tmp_path)
    cache.put(_track(), LyricsMatch(status="none", message="未找到"))
    assert cache.get(_track()) is None
    assert list(tmp_path.glob("*.json")) == []


def test_invalidate_removes_memory_and_disk(tmp_path):
    cache = LyricsCache(directory=tmp_path)
    track = _track()
    cache.put(track, _match())
    cache.invalidate(track)
    assert cache.get(track) is None
    fresh = LyricsCache(directory=tmp_path)
    assert fresh.get(track) is None


def test_clear_removes_everything(tmp_path):
    cache = LyricsCache(directory=tmp_path)
    cache.put(_track("One"), _match())
    cache.put(_track("Two"), _match())
    removed = cache.clear()
    assert removed == 2
    assert list(tmp_path.glob("*.json")) == []
    assert LyricsCache(directory=tmp_path).get(_track("One")) is None


def test_prune_keeps_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(lyrics_provider, "CACHE_MAX_ENTRIES", 3)
    cache = LyricsCache(directory=tmp_path)
    for i in range(5):
        cache.put(_track(f"Song{i}"), _match())
    assert len(list(tmp_path.glob("*.json"))) == 3


def test_corrupt_file_treated_as_miss(tmp_path):
    cache = LyricsCache(directory=tmp_path)
    track = _track()
    cache.put(track, _match())
    for f in tmp_path.glob("*.json"):
        f.write_text("not json {{{", encoding="utf-8")
    fresh = LyricsCache(directory=tmp_path)
    assert fresh.get(track) is None
