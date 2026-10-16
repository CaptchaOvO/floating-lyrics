# 技术文档

面向后续维护者。README 讲怎么用，这里讲代码怎么组织、为什么这么写、改东西从哪下手、踩过什么坑。

## 1. 总体架构

```text
media_session.py   [media-session 线程]  GSMTC 轮询 → MediaSnapshot
        │                                    （Qt Signal 跨线程，自动队列连接）
        ▼
controller.py      [GUI 主线程]  切歌检测 → 歌词获取 → 同步 → 更新悬浮窗
        │  ├─ lyrics_provider.py  LRCLIB 查询 + 内存/磁盘两级缓存
        │  ├─ lrc_parser.py       LRC → list[LyricLine]（纯函数）
        │  └─ lyrics_sync.py      位置 → 当前行（纯函数，二分查找）
        ▼
overlay_window.py  透明置顶窗（绘制、拖动、Win32 样式）
tray.py            托盘菜单（信号转发，无业务逻辑）
settings_dialog.py 设置对话框（读写 AppConfig）
```

### 线程模型

| 线程 | 职责 | 通信方式 |
| --- | --- | --- |
| GUI 主线程 | 全部 Qt 对象、controller、缓存读写、配置落盘 | — |
| `media-session`（daemon Thread + `asyncio.run`） | 轮询 GSMTC，0.2s 有会话 / 1.5s 空闲 / 5 次连续错误后 10s | `snapshot_changed` Signal |
| `lyrics-fetch`（ThreadPoolExecutor，1 worker） | LRCLIB 网络请求 | `_fetch_done` Signal `(generation, match)` |

跨线程只走 Qt Signal；缓存只在主线程读写，无锁。

### 生命周期

启动（`main.py:main`）：`setup_logging` → `QApplication`（`setQuitOnLastWindowClosed(False)`）→ 单实例锁（`QLockFile`，残留锁 30s 判定失效）→ `ConfigStore.load` → 构造 `AppController` → `aboutToQuit` 接 `shutdown` → `start()`（托盘/悬浮窗显示、恢复位置、启动 150ms tick 与媒体轮询）。

退出（唯一入口：托盘）：`tray.quit_requested` → `AppController._quit_app` → `overlay.allow_close_on_quit()` → `QApplication.quit()` → `shutdown`（停定时器、`media.stop` join 3s、线程池 cancel、`store.save`、隐藏托盘与窗口）→ `finally` 解锁。

## 2. 模块职责

| 文件 | 职责 | 关键点 |
| --- | --- | --- |
| `main.py` | 入口：日志、单实例、装配 | 日志目录优先项目内 `logs/`，不可写回退 `%APPDATA%` |
| `app/config.py` | `AppConfig` + `ConfigStore` | `validate()` 就地修正；损坏备份 `config.json.bad`；原子写（tmp + `os.replace`） |
| `app/media_session.py` | GSMTC 读取（winrt） | 快照不可变；`_should_emit` 去重；异常全部转 error 快照 |
| `app/lyrics_provider.py` | LRCLIB + 匹配打分 + `LyricsCache` | 打分是纯函数；429 带 `retry_after_s`；缓存两级 |
| `app/lrc_parser.py` | LRC 解析 | 多时间戳、`[offset:]`、去重排序；坏行跳过不抛 |
| `app/lyrics_sync.py` | 位置 → 当前行 | `build_display`、`visible_index`（见 §4.4） |
| `app/controller.py` | 总协调 | 切歌检测、generation、重试、托盘/设置响应 |
| `app/overlay_window.py` | 悬浮窗 | 绘制（阴影→描边→填充）、拖动、中心锚定、closeEvent 拒绝 |
| `app/win32_utils.py` | Win32 扩展样式 | WS_EX_TRANSPARENT/TOOLWINDOW/NOACTIVATE；64 位用 Ptr 版 API |
| `app/tray.py` | 托盘 | 仅信号转发；`set_states` 同步勾选（防信号回环） |
| `app/settings_dialog.py` | 设置对话框 | `_load` 读初值，`apply_to` 写回同一 config 对象 |

## 3. 关键数据结构

**`MediaSnapshot`**（frozen dataclass）：媒体状态快照。`track_key`（title/artist/album 归一化三元组）判断切歌；`position_at(now)` 在播放中按墙钟外插（`position_ms + (now - fetched_at) * 1000`，截断到 `[0, duration]`），暂停时保持不变。

**`LyricLine`**（frozen）：`time_ms` + `text`。

**`TrackInfo` / `LyricsMatch`**：查询入参 / 出参。`LyricsMatch.status` ∈ `ok`（有 `synced_lrc`）/ `none` / `error`（429 时 `retry_after_s` 有效）。

**`LyricsCache`**：键 = `normalize_text(artist)|normalize_text(title)|duration_s`（大小写、标点、空格不敏感；时长区分版本）。内存 dict（上限 128，插入序淘汰）+ 磁盘 `config_dir()/lyrics_cache/<sha1(键)>.json`（上限 `CACHE_MAX_ENTRIES=500`，按 mtime 淘汰）。只缓存 `ok` 且非空的结果；`invalidate(track)` 单曲删除（重取时用），`clear()` 全清。磁盘读写失败静默降级。

**`AppConfig`**（`config.py` 全字段有注释）：注意三点——

- `validate()` 对每个字段做类型/范围修正，非法值回默认，不抛异常；
- 未知 JSON 字段进 `extra`，**不会回写**（防止旧版本字段被新版本抹掉，反之亦然）；
- `lyrics_offset_ms` 语义：正值 = 歌词延后显示（匹配时 `position - offset`）。

## 4. 核心机制（写代码前先读懂）

### 4.1 同步链路

```text
MediaSnapshot ──position_at(monotonic)──▶ 减 lyrics_offset_ms ──▶ build_display(lines, pos, mode)
  ──▶ (绝对下标, 可见行) ──▶ visible_index 换算 ──▶ overlay.set_lyrics(可见行, 相对下标)
```

- 轮询 0.2s + 外插保证位置精度；另有 150ms `_on_tick` 在两次快照之间刷新行号，行切换感知延迟 < 0.3s。
- 重试：`_apply_lyrics` 中 error 状态最多 `MAX_FETCH_ATTEMPTS=3` 次，间隔 5s（429 用服务端 Retry-After，钳制 3–120s）。

### 4.2 切歌竞态（generation）

每次切歌 `_generation += 1`，抓取结果经 Signal 带回时丢弃 `generation != self._generation` 的过期结果。新增异步取词路径必须沿用这个令牌，否则快速切歌会闪错歌词。

### 4.3 窗口中心锚定

`_resize_to_content` → `_resize_keep_center`：先取 `geometry().center()`，resize 后 `moveCenter` 移回去。窗口宽窄随最长一行变化，但歌词视觉中心钉死。曾因左上角锚定导致长短句切换时歌词左右跳（commit `6307a8f`），改动 resize 路径必须保持这个不变量，冒烟有断言看护。

### 4.4 高亮下标：绝对 vs 相对

`build_display` 返回的 `current_index` 是**全量歌词**的绝对下标；悬浮窗按**可见列表**位置高亮。两者在窗口开头之外会错位（三行模式窗口滑动后），必须经 `visible_index()` 换算（按对象身份匹配）。给 `set_lyrics` 传下标前先问：这是哪种下标。

### 4.5 退出流程（曾经的坑，勿动）

悬浮窗 `closeEvent` 一律忽略（窗口不可被关闭）；但 `QApplication.quit()` 退出前会给可见窗口发 close 事件，全被忽略则**退出被中止**。所以托盘退出必须先 `allow_close_on_quit()` 放行。改退出逻辑时保持这个顺序。

### 4.6 Win32 窗口行为

| 能力 | 实现 |
| --- | --- |
| 鼠标穿透 | `WS_EX_TRANSPARENT` 动态开关（`apply_overlay_styles`） |
| 不进任务栏 / 不抢焦点 | `WS_EX_TOOLWINDOW` / `WS_EX_NOACTIVATE` |
| 置顶 | `SetWindowPos(HWND_TOPMOST/NOTOPMOST, SWP_NOACTIVATE)` |
| 显示不激活 | `ShowWindow(SW_SHOWNOACTIVATE)` |

64 位下必须用 `GetWindowLongPtrW/SetWindowLongPtrW`（`win32_utils` 已按指针宽度选择）。

### 4.7 绘制

`paintEvent` 按 `_draw_items()`（绘制与尺寸测量共用）逐行：阴影（pen 宽 = `shadow_blur`，路径平移 1.5px）→ 描边（pen 宽 = `outline_width`）→ 填充，全部走 `QPainterPath.addText`。起始 x 由纯函数 `text_x(align, width, advance)` 决定（居中/左对齐）。非当前行字号 = `font_size * 0.68`，当前行 DemiBold。

## 5. 常见维护任务

### 加一个设置项

1. `config.py`：`AppConfig` 加字段 + 默认值；
2. `config.py`：`validate()` 加校验分支（类型/范围，非法回默认）；
3. `settings_dialog.py`：`__init__` 加控件与表单行，`_load` 读初值，`apply_to` 写回；
4. 需要即时生效的（如超时）在 `controller.open_settings` 里同步到运行对象；
5. `tests/test_config.py` 加往返/校验测试；README 设置表加一行。

### 加一个托盘菜单项

1. `tray.py`：加 `Signal` + `QAction`（可勾选的记得 `setCheckable` 与 `_syncing` 防回环）+ `menu.addAction`；
2. `controller.py`：写 slot 并在 `_wire` 里 connect；
3. 勾选状态类的在 `_sync_tray_state` 里同步。

### 换/加歌词源

`lyrics_provider.py` 顶部定义了 `LyricsProvider` 协议：`lookup(track: TrackInfo) -> LyricsMatch`。新源实现该协议后替换 `controller.provider` 即可；匹配打分是纯函数（`score_candidate`/`pick_best_candidate`），可离线单测。缓存对源透明（存 `source` 字段）。

### 调同步手感

- 行切换延迟 → `POLL_INTERVAL_ACTIVE`（media_session.py）与 `_tick_timer` 间隔；
- 位置外插逻辑 → `MediaSnapshot.position_at`；
- 时间轴整体偏移 → `lyrics_offset_ms`（已产品化为设置项）。

## 6. 测试

| 层 | 命令 | 覆盖 |
| --- | --- | --- |
| 单元测试（64） | `.\.venv\Scripts\python.exe -m pytest` | 解析、同步、打分、缓存、配置校验、绘制纯函数 |
| 静态检查 | `.\.venv\Scripts\python.exe -m ruff check .` | E/F/W/I/UP/B，配置在 `pyproject.toml` |
| 离屏集成冒烟（19 断言） | `.\.venv\Scripts\python.exe -u tools\integration_smoke.py` | 托盘、设置往返、同步高亮、偏移、缓存、托盘退出 |

冒烟要点：脚本自带 `QT_QPA_PLATFORM=offscreen` 与独立 `FLOATING_LYRICS_HOME` 临时目录；provider 被 stub，不发真网络；`pump(app, 秒, until=...)` 驱动事件循环；退出走真实托盘动作并计时。

**写测试的硬性规矩**：涉及 `LyricsCache`/`ConfigStore`/controller 的测试必须传 `tmp_path` 或设置 `FLOATING_LYRICS_HOME`——曾因裸 `LyricsCache()` 把测试数据写进真实 `%APPDATA%\FloatingLyrics\lyrics_cache`。离屏渲染需要 `QT_QPA_FONTDIR=C:\Windows\Fonts`（否则无字体，宽度全 0）。

改了 `overlay_window`/`controller`/`lyrics_sync` 的行为，先想冒烟里该加/改哪条断言再动手。

## 7. 构建与发布

### PyInstaller 要点（`FloatingLyrics.spec`）

- `collect_all('winrt')` **必须保留**——winrt 是运行时动态加载的原生扩展，漏掉则打包版 SMTC 直接导入失败；
- onedir 布局：`dist\FloatingLyrics\FloatingLyrics.exe` + `_internal\`（整个文件夹是产物，exe 不能单飞）；
- `console=False`；图标 `assets/icon.ico`（由 `tools/make_icon_ico.py` 从 PNG 生成，无 Pillow 依赖）；
- 打包版日志在 `_internal\logs\`（冻结后 `BASE_DIR` 指向 `_internal`）；源码版在项目 `logs\`。目录不存在会自建。

### 发布流程

```powershell
# 1. 停掉正在运行的实例（否则 _internal 文件被锁，打包/压缩失败或缺文件）
taskkill /IM FloatingLyrics.exe /F
# 2. 重新打包
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm FloatingLyrics.spec
# 3. 打 zip（Windows 自带 tar，比 Compress-Archive 快；应用必须已停，日志文件会被锁）
tar -a -cf $env:TEMP\floating-lyrics-vX.Y.Z-windows-x64.zip -C .\dist FloatingLyrics
# 4. 上传（覆盖已有资产用 --clobber）
gh release create vX.Y.Z <zip> --title "vX.Y.Z" --notes-file <说明文件>
# 5. 启动验证：进程存在、窗口标题 Floating Lyrics、日志有 GSMTC 轮询与歌词行
Start-Process .\dist\FloatingLyrics\FloatingLyrics.exe
```

版本号同步改 `pyproject.toml` 的 `version`。

## 8. 踩坑记录

按踩过的顺序记，都是真实发生过的：

1. **BOM 毒化配置**：用 PowerShell `Set-Content`/`ConvertTo-Json` 改 `config.json` 会写入 UTF-8 BOM，`json.loads` 直接失败 → 程序判定"配置损坏"回退默认值（用户配色丢失）。手工改配置一律用 Python 写 `encoding="utf-8"`。
2. **`QApplication.quit()` 被 closeEvent 中止**：见 §4.5。排查"点退出没反应"先看 `allow_close_on_quit` 调用顺序。
3. **高亮错位**：绝对下标 vs 可见窗口相对下标，见 §4.4。
4. **歌词左右跳**：resize 左上角锚定 vs 中心锚定，见 §4.3。
5. **测试污染用户数据**：见 §6 硬性规矩。
6. **控制台中文乱码**：`chcp 936` 下 stdout 显示乱码，但日志文件是正确 UTF-8——是显示层问题，别去"修"日志编码。
7. **winrt 只有 cp312 wheel**：Python 版本必须 3.12，装错版本会找不到 wheel。
8. **pywinrt 枚举**：`playback_status` 的 `str()` 是数值，要取 `.name` 再解析（`media_session._read_once`）。
9. **运行中的 exe 锁文件**：PyInstaller 重建、tar 打包前必须先停进程，否则 `_internal` 下 DLL/日志被锁。
10. **单实例锁残留**：进程崩溃后 `QLockFile` 靠 30s stale 时间自愈，期间二次启动会提示"已在运行"。
11. **git 行尾警告**（LF will be replaced by CRLF）：无害，Windows 下 autocrlf 的正常提示。

## 9. 日志速查

格式：`时间 级别 [模块] 消息`，滚动 1MB × 3，UTF-8。

| 日志行 | 含义 / 排障方向 |
| --- | --- |
| `曲目切换: <artist> - <title>` | 切歌检测正常（`track_key` 变化） |
| `歌词已加载: N 行 (来源=…, 分数=…, 缓存=True/False)` | 缓存=True 即未走网络；分数 <0.6 的加载要人工确认是否配对 |
| `当前歌词行: i/N (位置=…s)` | 只在行号变化时打，用于核对同步是否跟手 |
| `播放状态变化: x -> y` | 暂停/继续可观测证据 |
| `歌词服务限流 (HTTP 429)` | LRCLIB 限流，按 Retry-After 自动退避，无需干预 |
| `歌词获取失败，Xs 后重试 (i/3)` | 网络抖动正常恢复路径；连续 3 次失败才放弃 |
| `未找到歌词: …` | LRCLIB 无该曲目同步歌词（纯音乐/冷门 remix 常见） |
| `配置文件损坏，已回退默认值` | 按 §8.1 查是谁写了 BOM/坏 JSON |
| `鼠标穿透已开启/关闭`、`窗口位置已保存` | 用户交互轨迹 |
| `收到退出请求（托盘）` → `退出清理完成` | 正常退出链路，两行都在才算干净退出 |
