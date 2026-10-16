"""离屏集成冒烟：托盘动作、设置往返、无歌词路径、歌词同步高亮、歌词偏移、歌词缓存、托盘退出。

用途：在没有交互桌面的环境下验证需求 §5.2 / §8 的关键链路（18 项断言）。
不启动媒体轮询、不发真实网络请求（provider 被 stub）。
配置写入独立的临时 FLOATING_LYRICS_HOME，不污染真实配置。

运行：``.venv\\Scripts\\python.exe -u tools\\integration_smoke.py``
全部通过时退出码为 0。
"""

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
_TMP = tempfile.mkdtemp(prefix="fl_smoke_")
os.environ["FLOATING_LYRICS_HOME"] = _TMP

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'} | {name} | {detail}", flush=True)


def pump(app, seconds: float, until=None) -> bool:
    """处理事件若干秒，until 为真可提前结束，返回是否达到了 until。"""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.02)
    return until is not None and until()


def main() -> int:
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)

    import main as main_mod
    from app.config import ConfigStore
    from app.controller import AppController
    from app.lyrics_provider import LyricsMatch
    from app.media_session import MediaSnapshot
    from app.settings_dialog import SettingsDialog

    store = ConfigStore()
    config = store.load()
    controller = AppController(config, store, main_mod.create_icon())
    app.aboutToQuit.connect(controller.shutdown)
    overlay = controller.overlay
    tray = controller.tray

    # ---- 1) 托盘显示/隐藏 ----
    overlay.show_overlay()
    pump(app, 0.2)
    check("初始窗口可见", overlay.isVisible())

    tray._act_show.trigger()  # 隐藏
    pump(app, 0.2)
    check("托盘隐藏窗口", not overlay.isVisible())

    tray._act_show.trigger()  # 显示
    pump(app, 0.2)
    check("托盘显示窗口", overlay.isVisible())

    # ---- 2) 托盘鼠标穿透 / 置顶（toggle 信号，检查 config 翻转+落盘）----
    before = config.click_through
    tray.toggle_click_through.emit()
    pump(app, 0.1)
    check("穿透切换生效", config.click_through == (not before), f"{before} -> {config.click_through}")
    saved = store.load()
    check("穿透设置已落盘", saved.click_through == config.click_through)
    tray.toggle_click_through.emit()  # 还原默认
    pump(app, 0.1)
    check("穿透还原默认(True)", config.click_through is True)

    before_top = config.always_on_top
    tray.toggle_topmost.emit()
    pump(app, 0.1)
    check("置顶切换生效", config.always_on_top == (not before_top))
    tray.toggle_topmost.emit()
    pump(app, 0.1)
    check("置顶还原默认(True)", config.always_on_top is True)

    # ---- 3) 设置对话框数据往返（不弹模态框，直接驱动控件）----
    dlg = SettingsDialog(config)
    dlg.mode.setCurrentIndex(1)  # single_line
    dlg.font_size.setValue(42)
    dlg.opacity.setValue(55)
    dlg.line_spacing.setValue(1.50)
    dlg.outline_enabled.setChecked(False)
    dlg.hide_when_no_lyrics.setChecked(True)
    dlg.apply_to(config)
    store.save(config)
    c2 = ConfigStore().load()
    ok = (
        c2.display_mode == "single_line"
        and c2.font_size == 42
        and abs(c2.window_opacity - 0.55) < 1e-9
        and abs(c2.line_spacing - 1.50) < 1e-9
        and c2.outline_enabled is False
        and c2.hide_when_no_lyrics is True
    )
    check("设置往返持久化", ok,
          f"mode={c2.display_mode} size={c2.font_size} opacity={c2.window_opacity} "
          f"spacing={c2.line_spacing} outline={c2.outline_enabled}")
    # 还原默认
    c2.display_mode = "three_line"
    c2.font_size = 26
    c2.window_opacity = 1.0
    c2.line_spacing = 1.35
    c2.outline_enabled = True
    c2.hide_when_no_lyrics = False
    store.save(c2)
    controller.config = c2
    config = c2

    # ---- 4) 无歌词路径（stub provider 返回 none）----
    class NoneProvider:
        def lookup(self, track):
            return LyricsMatch(status="none", message="未找到匹配的同步歌词")

    controller.provider = NoneProvider()
    snap1 = MediaSnapshot(
        available=True, app_id="Spotify.exe", title="NoLyrics Song", artist="X", album="Y",
        duration_ms=180_000, position_ms=1_000, status="playing",
    )
    controller._on_snapshot(snap1)
    got = pump(app, 5.0, until=lambda: controller._no_lyrics)
    check("无歌词标记置位", got, f"_no_lyrics={controller._no_lyrics}")
    check("无歌词提示可见(未隐藏)", overlay.isVisible() and controller._no_lyrics)

    # ---- 5) 有歌词路径：同步高亮（悬浮窗按可见窗口内的相对位置高亮）----
    lrc = "\n".join(f"[{i * 2 // 60:02d}:{i * 2 % 60:02d}.00]line {i} 行{i}" for i in range(10))

    class OkProvider:
        def lookup(self, track):
            return LyricsMatch(status="ok", synced_lrc=lrc, source="lrclib", match_score=1.0,
                               message="精确匹配", candidate_id=1)

    controller.provider = OkProvider()
    snap2 = MediaSnapshot(
        available=True, app_id="Spotify.exe", title="OkSong", artist="A", album="B",
        duration_ms=120_000, position_ms=4_500, status="playing",
    )
    controller._on_snapshot(snap2)
    got = pump(app, 5.0, until=lambda: bool(controller._lines))
    check("歌词加载", got, f"lines={len(controller._lines)}")
    # 4.5s → 绝对行 2；three_line 窗口 [行1,行2,行3] → 相对高亮位置 1（= 行2）
    check("当前行高亮(4.5s -> 行2, rel=1)", overlay._current_index == 1,
          f"rel_idx={overlay._current_index} visible={[ln.text for ln in overlay._visible_lines]}")

    # 进度回退
    snap3 = MediaSnapshot(
        available=True, app_id="Spotify.exe", title="OkSong", artist="A", album="B",
        duration_ms=120_000, position_ms=1_000, status="playing",
    )
    controller._on_snapshot(snap3)
    pump(app, 0.2)
    check("回退后高亮(1s -> 行0, rel=0)", overlay._current_index == 0,
          f"rel_idx={overlay._current_index}")

    # 歌词时间偏移：偏移 -4.5s（歌词提前显示）→ 1s 位置按 5.5s 匹配 → 行2, rel=1
    config.lyrics_offset_ms = -4_500
    snap4 = MediaSnapshot(
        available=True, app_id="Spotify.exe", title="OkSong", artist="A", album="B",
        duration_ms=120_000, position_ms=1_000, status="paused",
    )
    controller._on_snapshot(snap4)
    pump(app, 0.2)
    check("歌词偏移生效(-4.5s: 1s->按5.5s匹配行2, rel=1)", overlay._current_index == 1,
          f"rel_idx={overlay._current_index} visible={[ln.text for ln in overlay._visible_lines]}")
    config.lyrics_offset_ms = 0

    # ---- 5b) 歌词缓存：切走再切回 → 命中磁盘缓存，无需网络 ----
    calls: list[str] = []

    class CountingProvider:
        def lookup(self, track):
            calls.append(track.title)
            return LyricsMatch(status="none", message="no")

    controller.provider = CountingProvider()
    other = MediaSnapshot(
        available=True, app_id="Spotify.exe", title="OtherSong", artist="X", album="Y",
        duration_ms=60_000, position_ms=0, status="playing",
    )
    controller._on_snapshot(other)
    got = pump(app, 5.0, until=lambda: controller._no_lyrics)
    check("无缓存时访问网络", got and calls == ["OtherSong"], f"calls={calls}")

    controller.cache.memory.clear()  # 清空内存，强制走磁盘
    ok2 = MediaSnapshot(
        available=True, app_id="Spotify.exe", title="OkSong", artist="A", album="B",
        duration_ms=120_000, position_ms=4_500, status="paused",
    )
    controller._on_snapshot(ok2)
    got = pump(app, 5.0, until=lambda: bool(controller._lines))
    check("磁盘缓存命中免网络(OkSong)", got and "OkSong" not in calls,
          f"calls={calls} lines={len(controller._lines)}")

    # ---- 5c) 居中锚定：歌词变短后窗口宽度收缩，但中心点不漂移 ----
    from dataclasses import replace as dc_replace

    center_before = overlay.geometry().center()
    overlay.set_lyrics([dc_replace(controller._lines[0], text="短")], 0)
    center_after = overlay.geometry().center()
    check("居中锚定(歌词变短中心不漂移)", center_before == center_after,
          f"{center_before} -> {center_after}")

    # ---- 6) 托盘退出（窗口可见状态下触发 QAction，事件循环应立即自行退出）----
    from PySide6.QtCore import QTimer

    t0 = time.monotonic()
    QTimer.singleShot(100, tray._act_quit.trigger)
    QTimer.singleShot(5000, app.quit)  # 兜底
    app.exec()
    elapsed = time.monotonic() - t0
    check("托盘退出(可见窗口)结束事件循环", elapsed < 3.0, f"{elapsed:.2f}s")

    shutil.rmtree(_TMP, ignore_errors=True)
    failed = [r for r in results if not r[1]]
    print(f"\nTOTAL={len(results)} FAILED={len(failed)}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
