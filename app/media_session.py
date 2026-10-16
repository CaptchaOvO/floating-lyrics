"""Windows 媒体会话读取模块（GSMTC）。

通过 ``Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager``
读取系统全局媒体会话（Spotify 桌面版、Windows 媒体播放器等兼容 GSMTC 的应用）：

- 使用官方 ``winrt-Windows.Media.Control`` 包（纯 Python 投影，无需 Spotify API Key）；
- 独立后台线程 + asyncio 轮询，不阻塞 GUI 主线程；
- 以 :class:`MediaSnapshot` 数据对象对外发信号，与 GUI / 歌词逻辑完全解耦；
- winrt 依赖缺失、系统不支持等情况返回带 ``error`` 的快照，不抛裸异常。

本模块不导入任何 Qt 窗口类；仅用 ``QObject`` 的线程安全信号做数据转发。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)

# 轮询间隔（秒）
POLL_INTERVAL_ACTIVE = 0.2   # 有会话时（行切换感知延迟 <0.3s）
POLL_INTERVAL_IDLE = 1.5     # 无会话时
ERROR_RETRY_INTERVAL = 10.0  # 出错后的重试间隔


@dataclass(frozen=True)
class MediaSnapshot:
    """某一时刻的媒体状态快照（不可变）。"""

    available: bool = False
    app_id: str = ""
    title: str = ""
    artist: str = ""
    album: str = ""
    duration_ms: int = 0
    position_ms: int = 0
    status: str = "closed"  # playing / paused / stopped / buffering / completed / closed / error
    error: str = ""
    # 快照拉取时刻的单调时钟，用于播放中的位置插值
    fetched_at: float = field(default_factory=time.monotonic)

    @property
    def track_key(self) -> tuple[str, str, str]:
        """用于判断“是否切歌”的键。"""
        return (self.title.strip().lower(), self.artist.strip().lower(), self.album.strip().lower())

    @property
    def has_track(self) -> bool:
        return bool(self.title.strip())

    def position_at(self, now: float | None = None) -> int:
        """返回给定单调时刻的播放位置（毫秒）。

        播放中按墙钟外插，暂停/停止时保持不变；结果截断在 [0, duration]。
        """
        pos = self.position_ms
        if self.status == "playing":
            base = self.fetched_at if now is None else now
            pos += int(max(0.0, base - self.fetched_at) * 1000)
        if pos < 0:
            return 0
        if self.duration_ms > 0 and pos > self.duration_ms:
            return self.duration_ms
        return pos


_STATUS_MAP = {
    "OPEN": "open",
    "CHANGED": "changed",
    "PLAYING": "playing",
    "PAUSED": "paused",
    "STOPPED": "stopped",
    "FAST_FORWARD": "fast_forward",
    "REWIND": "rewind",
    "BUFFERING": "buffering",
    "COMPLETED": "completed",
    "CLOSED": "closed",
}


def _ms(delta) -> int:
    """datetime.timedelta -> 毫秒。"""
    try:
        return int(delta.total_seconds() * 1000)
    except AttributeError:
        return 0


class MediaSessionWorker(QObject):
    """后台轮询 GSMTC，通过 ``snapshot_changed`` 信号发布快照。"""

    snapshot_changed = Signal(object)  # MediaSnapshot

    def __init__(
        self,
        poll_interval_active: float = POLL_INTERVAL_ACTIVE,
        poll_interval_idle: float = POLL_INTERVAL_IDLE,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._poll_active = poll_interval_active
        self._poll_idle = poll_interval_idle
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------- 生命周期

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="media-session", daemon=True)
        self._thread.start()
        logger.info("媒体会话轮询线程已启动")

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)
            if thread.is_alive():
                logger.warning("媒体会话线程未能在 %.1fs 内退出", timeout)
            else:
                logger.info("媒体会话轮询线程已退出")
        self._thread = None

    # ------------------------------------------------------------- 内部实现

    def _run(self) -> None:
        try:
            asyncio.run(self._poll_loop())
        except Exception as exc:  # noqa: BLE001 - 线程顶层兜底
            logger.exception("媒体会话线程异常退出: %s", exc)
            self.snapshot_changed.emit(
                MediaSnapshot(status="error", error=f"媒体会话线程异常：{exc}")
            )

    async def _poll_loop(self) -> None:
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as SMTC,
        )

        last_emitted: MediaSnapshot | None = None
        consecutive_errors = 0

        while not self._stop.is_set():
            interval = self._poll_idle
            try:
                snapshot = await self._read_once(SMTC)
                consecutive_errors = 0
                if snapshot.available:
                    interval = self._poll_active
                else:
                    interval = self._poll_idle
            except Exception as exc:  # noqa: BLE001 - 单次轮询失败不致线程崩溃
                consecutive_errors += 1
                logger.warning("读取媒体会话失败(第%d次): %s", consecutive_errors, exc)
                snapshot = MediaSnapshot(status="error", error=f"读取媒体会话失败：{exc}")
                if consecutive_errors >= 5:
                    interval = ERROR_RETRY_INTERVAL

            if self._should_emit(snapshot, last_emitted):
                last_emitted = snapshot
                self.snapshot_changed.emit(snapshot)

            # 分段睡眠以便快速响应 stop
            await self._sleep_interruptible(interval)

    async def _sleep_interruptible(self, interval: float) -> None:
        deadline = time.monotonic() + interval
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(0.1, remaining))

    @staticmethod
    def _should_emit(new: MediaSnapshot, last: MediaSnapshot | None) -> bool:
        if last is None:
            return True
        # 结构性字段（曲目/状态/可用性/错误）变化必发
        if (
            new.available != last.available
            or new.track_key != last.track_key
            or new.status != last.status
            or new.error != last.error
        ):
            return True
        # 播放中位置会外插，固定节奏发出即可；暂停/空闲时位置没变就不发
        if new.status == "playing":
            return abs(new.position_ms - last.position_ms) >= 100
        return new.position_ms != last.position_ms

    async def _read_once(self, SMTC) -> MediaSnapshot:
        """读取一次当前媒体状态。"""
        manager = await SMTC.request_async()
        session = manager.get_current_session()
        if session is None:
            sessions = manager.get_sessions()
            session = sessions[0] if len(sessions) else None
        if session is None:
            return MediaSnapshot(available=False, status="closed")

        app_id = session.source_app_user_model_id or ""
        props = await session.try_get_media_properties_async()
        timeline = session.get_timeline_properties()
        playback_info = session.get_playback_info()

        status = playback_info.playback_status
        # pywinrt 枚举：str() 是数值，name 才是 "PLAYING"
        status_name = getattr(status, "name", None) or str(status)
        status_key = str(status_name).rsplit(".", 1)[-1].upper()
        status = _STATUS_MAP.get(status_key, status_key.lower())

        duration_ms = _ms(timeline.end_time)
        position_ms = _ms(timeline.position)

        return MediaSnapshot(
            available=True,
            app_id=app_id,
            title=(props.title or "").strip(),
            artist=(props.artist or "").strip(),
            album=(props.album_title or "").strip(),
            duration_ms=max(0, duration_ms),
            position_ms=max(0, position_ms),
            status=status,
            fetched_at=time.monotonic(),
        )


def winrt_available() -> bool:
    """检查 winrt 媒体控制依赖是否可用。"""
    try:
        import winrt.windows.foundation  # noqa: F401
        import winrt.windows.media.control  # noqa: F401
    except ImportError as exc:
        logger.warning("winrt 依赖不可用: %s", exc)
        return False
    return True
