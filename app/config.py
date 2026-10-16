"""配置模块。

使用本地 JSON 文件保存用户配置：

- 位置：``%APPDATA%\\FloatingLyrics\\config.json``（可用环境变量 ``FLOATING_LYRICS_HOME`` 覆盖目录）；
- 配置文件损坏时自动备份为 ``config.json.bad`` 并回退默认值；
- 所有字段读取时做类型校验，避免脏数据导致程序崩溃。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_VERSION = 1


def config_dir() -> Path:
    """返回配置目录（可能尚未创建）。"""
    override = os.environ.get("FLOATING_LYRICS_HOME")
    if override:
        return Path(override)
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home()
    return base / "FloatingLyrics"


@dataclass
class AppConfig:
    """应用配置。字段含义见 README。"""

    # 歌词显示模式：three_line（三行） / single_line（仅当前行）
    display_mode: str = "three_line"
    # 文字对齐：center（居中） / left（左对齐）
    text_align: str = "center"
    # 字体
    font_family: str = "Microsoft YaHei UI"
    font_size: int = 26
    # 颜色（#AARRGGBB 或 #RRGGBB）
    current_color: str = "#FFFFFFFF"
    other_color: str = "#99FFFFFF"
    # 描边/阴影
    outline_enabled: bool = True
    outline_color: str = "#B0000000"
    outline_width: int = 2
    shadow_enabled: bool = False
    shadow_color: str = "#80000000"
    shadow_blur: int = 4
    # 行距倍数（1.0 ~ 2.5）
    line_spacing: float = 1.35
    # 歌词时间偏移（毫秒，-5000 ~ +5000）：
    # 正值=歌词延后显示（LRC 时间戳偏早/歌曲前奏更长时用），负值=歌词提前显示
    lyrics_offset_ms: int = 0
    # 窗口整体透明度（0.3 ~ 1.0）
    window_opacity: float = 1.0
    # 无歌词时自动隐藏窗口（否则显示“未找到歌词”）
    hide_when_no_lyrics: bool = False
    # 无媒体会话时是否隐藏窗口
    hide_when_idle: bool = False
    # 窗口行为
    always_on_top: bool = True
    click_through: bool = True  # 默认开启穿透（用户要求）
    # 窗口位置（逻辑坐标）；None 表示未保存过
    window_x: int | None = None
    window_y: int | None = None
    # 网络
    http_timeout: int = 10
    # 内部
    config_version: int = CONFIG_VERSION
    # 每个字段是否允许出现在 JSON 中（防止未知字段回写）在 load 时过滤
    extra: dict = field(default_factory=dict, repr=False, compare=False)

    def validate(self) -> AppConfig:
        """就地校验并修正非法值，返回自身。"""
        if self.display_mode not in ("three_line", "single_line"):
            self.display_mode = "three_line"
        if self.text_align not in ("center", "left"):
            self.text_align = "center"
        if not isinstance(self.font_size, int) or isinstance(self.font_size, bool) or not (8 <= self.font_size <= 160):
            self.font_size = 26
        if not isinstance(self.line_spacing, (int, float)) or isinstance(self.line_spacing, bool) or not (1.0 <= float(self.line_spacing) <= 2.5):
            self.line_spacing = 1.35
        self.line_spacing = float(self.line_spacing)
        if not isinstance(self.window_opacity, (int, float)) or isinstance(self.window_opacity, bool) or not (0.3 <= float(self.window_opacity) <= 1.0):
            self.window_opacity = 1.0
        self.window_opacity = float(self.window_opacity)
        if not isinstance(self.outline_width, int) or isinstance(self.outline_width, bool) or self.outline_width not in range(0, 9):
            self.outline_width = 2
        if not isinstance(self.lyrics_offset_ms, int) or isinstance(self.lyrics_offset_ms, bool):
            self.lyrics_offset_ms = 0
        else:
            self.lyrics_offset_ms = max(-5000, min(5000, self.lyrics_offset_ms))
        if not isinstance(self.shadow_blur, int) or isinstance(self.shadow_blur, bool) or self.shadow_blur not in range(0, 21):
            self.shadow_blur = 4
        defaults = AppConfig()
        for name in ("current_color", "other_color", "outline_color", "shadow_color"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _is_color(value):
                setattr(self, name, getattr(defaults, name))
        if not isinstance(self.font_family, str) or not self.font_family.strip():
            self.font_family = defaults.font_family
        for name in ("always_on_top", "click_through", "outline_enabled",
                     "shadow_enabled", "hide_when_no_lyrics", "hide_when_idle"):
            setattr(self, name, bool(getattr(self, name)))
        if not isinstance(self.http_timeout, int) or isinstance(self.http_timeout, bool) or not (3 <= self.http_timeout <= 60):
            self.http_timeout = defaults.http_timeout
        for name in ("window_x", "window_y"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
                setattr(self, name, None)
        return self


def _is_color(value: str) -> bool:
    if not isinstance(value, str) or not value.startswith("#"):
        return False
    body = value[1:]
    return len(body) in (6, 8) and all(c in "0123456789abcdefABCDEF" for c in body)


def _coerce(raw: dict) -> AppConfig:
    """按字段类型构造 AppConfig，未知字段忽略、缺失字段用默认值。"""
    known = {f.name for f in fields(AppConfig)} - {"extra"}
    kwargs: dict = {}
    for key, value in raw.items():
        if key not in known:
            continue
        kwargs[key] = value
    cfg = AppConfig(**kwargs)
    cfg.extra = {k: v for k, v in raw.items() if k not in known}
    return cfg


class ConfigStore:
    """配置的加载/保存。"""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or config_dir()
        self.path = self.directory / "config.json"

    def load(self) -> AppConfig:
        """加载配置；文件不存在返回默认值，损坏则备份后返回默认值。"""
        if not self.path.exists():
            return AppConfig()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("配置根节点必须是对象")
            return _coerce(raw).validate()
        except (json.JSONDecodeError, ValueError, TypeError, OSError) as exc:
            logger.warning("配置文件损坏，已回退默认值 (%s): %s", self.path, exc)
            try:
                bad = self.path.with_suffix(".json.bad")
                shutil.copyfile(self.path, bad)
                logger.warning("原配置已备份到 %s", bad)
            except OSError:
                pass
            return AppConfig()

    def save(self, config: AppConfig) -> None:
        """保存配置（原子写入：先写临时文件再替换）。"""
        config.validate()
        self.directory.mkdir(parents=True, exist_ok=True)
        data = asdict(config)
        data.pop("extra", None)
        data["config_version"] = CONFIG_VERSION
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
