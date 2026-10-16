# Floating Lyrics

Windows 桌面悬浮歌词。通过系统媒体接口（GSMTC）读播放进度，歌词来自 [LRCLIB](https://lrclib.net)，显示在一个透明置顶的小窗口里。主要对着 Spotify Desktop 用，任何支持系统媒体会话的播放器都能驱动。

Python 3.12 + PySide6 + winrt，无 Spotify API Key，不采集任何数据。
<img width="1920" height="1030" alt="image" src="https://github.com/user-attachments/assets/bd70aeca-10df-4418-9c59-4488d99954bf" />


## 功能

**同步**

- 监听系统媒体会话，切歌、暂停、拖进度条自动跟随，无需手动干预
- 0.2s 轮询 + 150ms 界面刷新，行切换延迟在半秒以内
- 歌词时间偏移 ±5 秒，个别歌曲 LRC 时间轴不准时可手动校准

**歌词**

- LRCLIB 精确匹配优先、搜索打分兜底，挑最接近的版本（标题/歌手/专辑/时长综合打分）
- 找到的歌词写入本地缓存（`%APPDATA%\FloatingLyrics\lyrics_cache\`），同一首歌下次直接读缓存，不重复联网
- 网络抖动自动重试，服务端限流（429）按 Retry-After 退避
- 没有同步歌词的曲目显示提示，不伪造时间轴

**显示**

- 透明无边框置顶窗，不进任务栏、不抢焦点
- 鼠标穿透默认开启；需要拖动窗口时从托盘关掉穿透，拖完再打开
- 三行模式（上/当前/下）或单行模式，当前行放大高亮
- 居中/左对齐，字体、字号、颜色、描边、阴影、行距、整窗透明度均可调
- 窗口以歌词中心为锚点自适应宽窄，长短句切换时歌词的视觉中心钉在原地不漂移
- 窗口位置记忆，下次启动回到原地

**托盘**

- 显示/隐藏、鼠标穿透、窗口置顶、重新获取歌词、清除歌词缓存、设置、退出
- 歌词窗口本身关不掉，退出只能走托盘，防止误关
- 单实例保护，重复启动会提示并退出，不会开出第二个窗口

## 构建与运行

要求 Windows 10+、Python 3.12（winrt 官方 wheel 只发布到 3.12）。

```powershell
# 建环境、装依赖
py -3.12 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt

# 运行
.\.venv\Scripts\python main.py

# 打包，产物在 dist\FloatingLyrics\
.\.venv\Scripts\python -m PyInstaller --noconfirm FloatingLyrics.spec
```

打包出的 `FloatingLyrics` 整个文件夹是便携的，拷到另一台 Windows 机器上双击 `FloatingLyrics.exe` 即可运行，不需要装 Python。

开发时的检查：

```powershell
.\.venv\Scripts\python.exe -m pytest                        # 单元测试
.\.venv\Scripts\python.exe -m ruff check .                  # 静态检查
.\.venv\Scripts\python.exe -u tools\integration_smoke.py    # 离屏集成冒烟
```

## 配置与日志

托盘右键 → 设置，改动即时生效并写入 `%APPDATA%\FloatingLyrics\config.json`。配置损坏时自动回退默认值并在日志中告警，不会崩。

日志在 `logs\app.log`（打包版在 `_internal\logs\`），滚动保留 3 份。歌词加载失败、限流重试、行号跳变都有记录，出问题先看它。

## 已知限制

- 无逐字高亮：LRCLIB 不提供逐字时间轴，也没有这方面需求
- 歌词时间轴由社区人工校对，个别歌曲会整体偏一两秒，属数据本身的问题，用「歌词偏移」校准
- Windows 注销/关机时进程由系统直接终止，不走正常退出流程；配置是实时保存的，无数据损失
