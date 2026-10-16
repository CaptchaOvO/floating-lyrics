"""配置加载/保存测试。"""

from __future__ import annotations

import json

from app.config import AppConfig, ConfigStore


def test_defaults(tmp_path):
    store = ConfigStore(tmp_path)
    cfg = store.load()
    assert cfg.display_mode == "three_line"
    assert cfg.click_through is True      # 默认开启穿透（用户要求）
    assert cfg.always_on_top is True
    assert cfg.window_x is None


def test_save_load_roundtrip(tmp_path):
    store = ConfigStore(tmp_path)
    cfg = AppConfig()
    cfg.font_size = 40
    cfg.display_mode = "single_line"
    cfg.current_color = "#80FF0000"
    cfg.window_x = 123
    cfg.window_y = 456
    cfg.line_spacing = 1.5
    cfg.window_opacity = 0.8
    store.save(cfg)

    loaded = ConfigStore(tmp_path).load()
    assert loaded.font_size == 40
    assert loaded.display_mode == "single_line"
    assert loaded.current_color == "#80FF0000"
    assert (loaded.window_x, loaded.window_y) == (123, 456)
    assert loaded.line_spacing == 1.5
    assert loaded.window_opacity == 0.8


def test_corrupted_config_recovers(tmp_path):
    store = ConfigStore(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{ 这不是合法 JSON !!!", encoding="utf-8")
    cfg = store.load()
    assert cfg == AppConfig()  # 回退默认值
    assert store.path.with_suffix(".json.bad").exists()  # 原文件已备份


def test_partial_config_uses_defaults(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.json").write_text(json.dumps({"font_size": 30}), encoding="utf-8")
    cfg = ConfigStore(tmp_path).load()
    assert cfg.font_size == 30
    assert cfg.display_mode == "three_line"  # 缺失字段用默认


def test_invalid_values_fixed(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    bad = {
        "display_mode": "not_a_mode",
        "font_size": 9999,
        "line_spacing": 99,
        "window_opacity": 0.01,
        "current_color": "red",
        "window_x": "left",
        "unknown_key": {"nested": 1},
    }
    (tmp_path / "config.json").write_text(json.dumps(bad), encoding="utf-8")
    cfg = ConfigStore(tmp_path).load()
    assert cfg.display_mode == "three_line"
    assert cfg.font_size == 26
    assert cfg.line_spacing == 1.35
    assert cfg.window_opacity == 1.0
    assert cfg.current_color == "#FFFFFFFF"
    assert cfg.window_x is None
    # 未知字段保留在 extra 中且不会影响加载
    assert "unknown_key" in cfg.extra


def test_unknown_fields_not_written_back(tmp_path):
    store = ConfigStore(tmp_path)
    cfg = AppConfig()
    cfg.extra = {"stale_key": 1}
    store.save(cfg)
    data = json.loads(store.path.read_text(encoding="utf-8"))
    assert "stale_key" not in data
    assert "config_version" in data


def test_lyrics_offset_validation(tmp_path):
    """歌词时间偏移：合法值保留、越界钳制、非法类型回默认 0。"""
    tmp_path.mkdir(parents=True, exist_ok=True)

    (tmp_path / "config.json").write_text(
        json.dumps({"lyrics_offset_ms": 2500}), encoding="utf-8"
    )
    assert ConfigStore(tmp_path).load().lyrics_offset_ms == 2500

    (tmp_path / "config.json").write_text(
        json.dumps({"lyrics_offset_ms": -99999}), encoding="utf-8"
    )
    assert ConfigStore(tmp_path).load().lyrics_offset_ms == -5000

    (tmp_path / "config.json").write_text(
        json.dumps({"lyrics_offset_ms": "abc"}), encoding="utf-8"
    )
    assert ConfigStore(tmp_path).load().lyrics_offset_ms == 0


def test_text_align_validation(tmp_path):
    """对齐方式：非法值回默认（居中），合法值保留。"""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.json").write_text(json.dumps({"text_align": "justify"}), encoding="utf-8")
    assert ConfigStore(tmp_path).load().text_align == "center"
    (tmp_path / "config.json").write_text(json.dumps({"text_align": "left"}), encoding="utf-8")
    assert ConfigStore(tmp_path).load().text_align == "left"
