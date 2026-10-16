"""应用控制器：媒体会话 → 歌词获取 → 同步 → 悬浮窗显示。

职责：

- 监听 :class:`MediaSessionWorker` 的快照，检测切歌/暂停/进度变化；
- 切歌时立即清空旧歌词并异步获取新歌词（``ThreadPoolExecutor``，不阻塞 GUI）；
- 用“代数（generation）”丢弃过期的歌词请求结果；
- 同步当前歌词行到 :class:`OverlayWindow`；
- 托盘菜单/设置对话框的响应与状态同步；
- 配置落盘（窗口位置、设置项）。
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QObject, QTimer, Signal, Slot
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtWidgets import QApplication

from .config import AppConfig, ConfigStore
from .lrc_parser import parse_lrc
from .lyrics_provider import LRCLIBProvider, LyricsCache, LyricsMatch, TrackInfo
from .lyrics_sync import build_display, visible_index
from .media_session import MediaSessionWorker, MediaSnapshot
from .overlay_window import OverlayWindow
from .settings_dialog import SettingsDialog
from .tray import TrayIcon

logger = logging.getLogger(__name__)

FETCH_RETRY_DELAY_MS = 5000
MAX_FETCH_ATTEMPTS = 3


class AppController(QObject):
    """总控制器。"""

    # 工作线程 → 主线程的歌词结果信号（generation, LyricsMatch）
    _fetch_done = Signal(object, object)

    def __init__(
        self,
        config: AppConfig,
        store: ConfigStore,
        icon: QIcon,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self.store = store

        self.overlay = OverlayWindow(config)
        self.tray = TrayIcon(icon)
        self.media = MediaSessionWorker()
        self.provider = LRCLIBProvider(timeout=config.http_timeout)
        self.cache = LyricsCache()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lyrics-fetch")
        self._settings_dialog: SettingsDialog | None = None

        # 状态
        self._snapshot: MediaSnapshot | None = None
        self._track_key: tuple[str, str, str] | None = None
        self._lyrics_key: tuple[str, str, str] | None = None   # 已加载歌词对应的曲目
        self._lines = []
        self._generation = 0
        self._fetch_attempts = 0
        self._fetching = False
        self._no_lyrics = False
        self._pending_track: TrackInfo | None = None
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._retry_fetch)
        # 界面刷新定时器：两次媒体快照之间也让行号跟着播放进度走（含墙钟外插与歌词偏移）
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(150)
        self._tick_timer.timeout.connect(self._on_tick)
        self._positioned = False  # 首次定位

        self._wire()

    # ------------------------------------------------------------- 装配

    def _wire(self) -> None:
        self.media.snapshot_changed.connect(self._on_snapshot)
        self._fetch_done.connect(self._on_fetch_done)

        self.overlay.position_changed.connect(self._on_position_changed)

        self.tray.show_hide_requested.connect(self.toggle_window_visible)
        self.tray.toggle_click_through.connect(self.toggle_click_through)
        self.tray.toggle_topmost.connect(self.toggle_topmost)
        self.tray.reload_lyrics.connect(self.reload_lyrics)
        self.tray.clear_cache_requested.connect(self.clear_lyrics_cache)
        self.tray.open_settings.connect(self.open_settings)
        self.tray.quit_requested.connect(self._quit_app)

    @Slot()
    def _quit_app(self) -> None:
        """托盘退出：先放行悬浮窗的关闭事件（否则 Qt 退出会被 closeEvent 忽略中止）。"""
        logger.info("收到退出请求（托盘）")
        self.overlay.allow_close_on_quit()
        QApplication.instance().quit()

    def start(self) -> None:
        """启动媒体轮询并显示悬浮窗/托盘。"""
        self._sync_tray_state()
        self.overlay.show_overlay()
        self.tray.show()
        self._restore_position()
        self.overlay.set_status("等待媒体会话…")
        self._tick_timer.start()
        self.media.start()
        logger.info("控制器已启动 (穿透=%s, 置顶=%s)", self.config.click_through, self.config.always_on_top)

    def shutdown(self) -> None:
        """退出清理：停线程、关线程池、保存配置。"""
        logger.info("开始退出清理")
        self._retry_timer.stop()
        self._tick_timer.stop()
        try:
            self.media.stop()
        except Exception:  # noqa: BLE001
            logger.exception("停止媒体线程失败")
        self.executor.shutdown(wait=False, cancel_futures=True)
        try:
            self.store.save(self.config)
        except Exception:  # noqa: BLE001
            logger.exception("保存配置失败")
        self.tray.hide()
        self.overlay.hide_overlay()
        logger.info("退出清理完成")

    # ------------------------------------------------------------- 媒体快照

    @Slot(object)
    def _on_snapshot(self, snapshot: MediaSnapshot) -> None:
        self._snapshot = snapshot

        if not snapshot.available:
            self._track_key = None
            if snapshot.error:
                self.overlay.set_status("媒体会话不可用")
                logger.warning("媒体会话不可用: %s", snapshot.error)
            elif self.config.hide_when_idle:
                self.overlay.hide_overlay()
                self._sync_tray_state()
            else:
                self.overlay.set_status("等待媒体播放…")
            return

        if snapshot.error:
            self.overlay.set_status("读取媒体状态失败")
            return

        if not snapshot.has_track:
            # 会话存在但没有曲目信息
            self.overlay.set_status("媒体会话已连接（无曲目信息）")
            return

        # 播放状态变化记录（暂停/继续的可观测证据）
        prev_status = getattr(self, "_last_status", None)
        if prev_status != snapshot.status:
            self._last_status = snapshot.status
            logger.info("播放状态变化: %s -> %s", prev_status or "(初始)", snapshot.status)

        key = snapshot.track_key
        if self.config.hide_when_idle and not self.overlay.isVisible():
            self.overlay.show_overlay()

        if key != self._track_key:
            self._on_track_changed(snapshot, key)

        self._update_position(snapshot)

    def _on_track_changed(self, snapshot: MediaSnapshot, key: tuple[str, str, str]) -> None:
        """切歌：清旧歌词、查缓存或发起新请求。"""
        logger.info("曲目切换: %s - %s (%s)", snapshot.artist, snapshot.title, snapshot.app_id)
        self._track_key = key
        self._generation += 1
        self._fetch_attempts = 0
        self._lines = []
        self._no_lyrics = False
        self._retry_timer.stop()
        self.overlay.show_overlay()

        # 内存缓存命中
        track = self._track_to_info(snapshot)
        cached = self.cache.get(track)
        if cached is not None and cached.status == "ok" and cached.synced_lrc:
            self._apply_lyrics(self._generation, key, cached, from_cache=True)
            return

        self._fetching = True
        self.overlay.set_status("正在获取歌词…")
        self._submit_fetch(self._generation, track)

    def _submit_fetch(self, generation: int, track: TrackInfo) -> None:
        def work() -> None:
            match = self.provider.lookup(track)
            # emit 跨线程安全（接收方在主线程，自动队列连接）
            self._fetch_done.emit(generation, match)

        try:
            self.executor.submit(work)
        except RuntimeError:
            logger.warning("线程池已关闭，放弃歌词请求")
            self._fetching = False

    @Slot(object, object)
    def _on_fetch_done(self, generation: int, match: LyricsMatch) -> None:
        """歌词请求返回（可能已过期）。"""
        if generation != self._generation:
            logger.debug("丢弃过期歌词结果 (gen=%s != %s)", generation, self._generation)
            return
        self._fetching = False
        key = self._track_key
        if key is None:
            return
        self._apply_lyrics(generation, key, match)

    def _apply_lyrics(
        self,
        generation: int,
        key: tuple[str, str, str],
        match: LyricsMatch,
        from_cache: bool = False,
    ) -> None:
        if match.status == "error":
            self._fetch_attempts += 1
            if self._fetch_attempts < MAX_FETCH_ATTEMPTS:
                delay_ms = FETCH_RETRY_DELAY_MS
                if match.retry_after_s > 0:  # 服务端限流：按 Retry-After 退避
                    delay_ms = int(max(3.0, min(match.retry_after_s, 120.0)) * 1000)
                logger.info("歌词获取失败，%.1fs 后重试 (%d/%d)：%s",
                            delay_ms / 1000, self._fetch_attempts, MAX_FETCH_ATTEMPTS, match.message)
                if self._snapshot and self._snapshot.has_track:
                    self._pending_track = self._track_to_info(self._snapshot)
                    self._retry_timer.start(delay_ms)
                self.overlay.set_status(match.message or "歌词服务出错，稍后重试…")
            else:
                logger.warning("歌词获取多次失败，放弃: %s", match.message)
                self._show_no_lyrics()
            return

        if match.status != "ok" or not match.synced_lrc:
            logger.info("未找到歌词: %s", match.message)
            self._show_no_lyrics()
            return

        lines = parse_lrc(match.synced_lrc)
        if not lines:
            logger.warning("歌词解析结果为空 (id=%s)", match.candidate_id)
            self._show_no_lyrics()
            return

        if not from_cache:
            self.cache.put(self._current_track(), match)
        self._lines = lines
        self._lyrics_key = key
        self._no_lyrics = False
        logger.info("歌词已加载: %d 行 (来源=%s, 分数=%.2f, 缓存=%s)",
                    len(lines), match.source, match.match_score, from_cache)
        if self._snapshot:
            self._update_position(self._snapshot)

    def _retry_fetch(self) -> None:
        track = self._pending_track
        if track is None or self._track_key is None:
            return
        self._fetching = True
        self._submit_fetch(self._generation, track)

    def _show_no_lyrics(self) -> None:
        self._no_lyrics = True
        self._lines = []
        if self.config.hide_when_no_lyrics:
            self.overlay.hide_overlay()
            self._sync_tray_state()
        else:
            self.overlay.set_status("未找到歌词")

    # ------------------------------------------------------------- 位置同步

    def _on_tick(self) -> None:
        """界面刷新定时器（150ms）：媒体快照间隔内持续按墙钟外插刷新行号。"""
        snap = self._snapshot
        if self._lines and snap is not None and snap.has_track:
            self._update_position(snap)

    def _update_position(self, snapshot: MediaSnapshot) -> None:
        """把当前播放位置同步到悬浮窗（无歌词时保持状态提示）。"""
        if not self._lines:
            return
        # 应用用户设置的歌词时间偏移：正值=歌词延后显示
        position = snapshot.position_at(time.monotonic()) - self.config.lyrics_offset_ms
        current_idx, visible = build_display(self._lines, position, self.config.display_mode)
        if current_idx != getattr(self, "_last_logged_idx", None):
            self._last_logged_idx = current_idx
            logger.info("当前歌词行: %d/%d (位置=%.1fs)", current_idx + 1, len(self._lines), position / 1000)
        # 悬浮窗按可见列表内的相对位置高亮，绝对下标需换算
        self.overlay.set_lyrics(visible, visible_index(current_idx, self._lines, visible))

    # ------------------------------------------------------------- 交互入口

    @Slot()
    def toggle_window_visible(self) -> None:
        if self.overlay.isVisible():
            self.overlay.hide_overlay()
        else:
            self.overlay.show_overlay()
            if self._snapshot is None:
                self.overlay.set_status("等待媒体会话…")
        self._sync_tray_state()

    @Slot()
    def toggle_click_through(self) -> None:
        self.config.click_through = not self.config.click_through
        self.overlay.set_click_through(self.config.click_through)
        self.store.save(self.config)
        self._sync_tray_state()

    @Slot()
    def toggle_topmost(self) -> None:
        self.config.always_on_top = not self.config.always_on_top
        self.overlay.set_topmost(self.config.always_on_top)
        self.store.save(self.config)
        self._sync_tray_state()

    @Slot()
    def reload_lyrics(self) -> None:
        """手动重新获取当前曲目歌词（托盘菜单）。"""
        if self._snapshot is None or not self._snapshot.has_track:
            self.overlay.set_status("当前没有正在播放的曲目")
            return
        logger.info("手动重新获取歌词")
        self._generation += 1
        self._fetch_attempts = 0
        self._lines = []
        self._no_lyrics = False
        self._retry_timer.stop()
        track = self._track_to_info(self._snapshot)
        # 清掉缓存里的旧结果（内存 + 磁盘），强制走网络重新拉取
        self.cache.invalidate(track)
        self._fetching = True
        self.overlay.set_status("正在获取歌词…")
        self._submit_fetch(self._generation, track)

    @Slot()
    def clear_lyrics_cache(self) -> None:
        """清空歌词缓存（托盘菜单）。"""
        removed = self.cache.clear()
        logger.info("歌词缓存已清除: %d 条", removed)
        self.tray.show_message("Floating Lyrics", f"歌词缓存已清除（{removed} 条）")

    @Slot()
    def open_settings(self) -> None:
        dialog = SettingsDialog(self.config)
        if dialog.exec():
            dialog.apply_to(self.config)
            self.store.save(self.config)
            self.provider.timeout = float(self.config.http_timeout)
            self.overlay.apply_config(self.config)
            self._sync_tray_state()
            # 重新按新布局刷新
            if self._snapshot:
                if self._lines:
                    self._update_position(self._snapshot)
                elif self._no_lyrics:
                    self._show_no_lyrics()
            logger.info("设置已保存")
        self._settings_dialog = None

    # ------------------------------------------------------------- 杂项

    @Slot(int, int)
    def _on_position_changed(self, x: int, y: int) -> None:
        self.config.window_x = x
        self.config.window_y = y
        self.store.save(self.config)
        logger.info("窗口位置已保存: (%d, %d)", x, y)

    def _sync_tray_state(self) -> None:
        self.tray.set_states(
            window_visible=self.overlay.isVisible(),
            click_through=self.config.click_through,
            topmost=self.config.always_on_top,
        )

    def _track_to_info(self, snapshot: MediaSnapshot) -> TrackInfo:
        return TrackInfo(
            title=snapshot.title,
            artist=snapshot.artist,
            album=snapshot.album,
            duration_ms=snapshot.duration_ms,
        )

    def _current_track(self) -> TrackInfo:
        snap = self._snapshot
        if snap is None:
            return TrackInfo(title="", artist="")
        return self._track_to_info(snap)

    def _restore_position(self) -> None:
        """恢复保存的窗口位置；无记录或超出屏幕时放到默认位置。"""
        screens = QGuiApplication.screens()
        x, y = self.config.window_x, self.config.window_y
        target = None
        if x is not None and y is not None and screens:
            from PySide6.QtCore import QPoint
            point = QPoint(x, y)
            for screen in screens:
                if screen.geometry().contains(point):
                    target = screen
                    break
        if target is None:
            target = QGuiApplication.primaryScreen()
        if target is None:
            return
        geo = target.availableGeometry()
        w, h = self.overlay.width(), self.overlay.height()
        if x is None or y is None:
            x = geo.x() + (geo.width() - w) // 2
            y = geo.y() + int(geo.height() * 0.72)
        else:
            # 部分越界时拉回屏幕内
            x = max(geo.x(), min(x, geo.x() + geo.width() - w))
            y = max(geo.y(), min(y, geo.y() + geo.height() - h))
        self.overlay.move(x, y)
        self._positioned = True
