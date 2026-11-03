"""VideoEnglishLearner (vel) —— 本地英语视频学习工具（YouTube → 字幕 → 播放器 → 精听）."""

from __future__ import annotations

import sys

APP_NAME = "VideoEnglishLearner"
APP_SLUG = "vel"

__version__ = "0.2.0"


def ensure_utf8_stdio() -> None:
    """Windows 控制台默认 GBK，打印 ✓/✗ 会 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
