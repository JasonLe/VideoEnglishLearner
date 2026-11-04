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


# python.org 的 macOS Python 没带 CA 证书；在导入 yt-dlp 之前修好环境变量，
# 否则「下载视频」会报 CERTIFICATE_VERIFY_FAILED（_ssl.c:1077）。
try:
    from .net import ensure_ca_env as _ensure_ca_env

    _ensure_ca_env()
except Exception:  # noqa: BLE001 - 证书环境修不好也不能影响启动
    pass
