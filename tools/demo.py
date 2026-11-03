"""造一条本地演示数据：假视频文件 + 假字幕，用来离线联调播放器。

    python tools/demo.py

它不联网、不下载，只往 data/vel.db 里插一条 status=ready 的记录，
并生成 data/media/<pk>.mp4（2MB 占位字节）。播放器能打开、能点字幕跳转、
能看高亮 —— 只是画面是黑的。真实验证走 `python -m app.cli ingest <url>`。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import db, ensure_utf8_stdio  # noqa: E402
from app.config import MEDIA_DIR, cfg  # noqa: E402
from app.models import Word  # noqa: E402
from app.segment import build_segments  # noqa: E402

LINES = [
    "Hey everyone, welcome back to the channel.",
    "Today I want to talk about something that took me years to figure out.",
    "When I first started learning English, I tried to memorize every single word.",
    "And honestly, that approach did not work for me at all.",
    "What actually worked was watching the same short video over and over again.",
    "So in this video, I will show you exactly how I do it now.",
    "It is simple, it is free, and it does not take much time.",
    "Let me know in the comments if you want a part two.",
]

WORD_MS = 300
GAP_MS = 220


def main() -> int:
    ensure_utf8_stdio()
    db.init_db()
    pk = db.create_video(
        "https://www.youtube.com/watch?v=demo0000000",
        "demo0000000",
        "Demo · 我是怎么练英语听力的（本地假数据）",
        "Demo Channel",
        180_000,
        "",
    )

    words: list[Word] = []
    cursor = 900
    for line in LINES:
        for token in line.split():
            words.append(Word(token, cursor, cursor + WORD_MS))
            cursor += WORD_MS
        cursor += GAP_MS

    segments = build_segments(words, cfg.get)
    count = db.replace_segments(pk, segments)

    media = MEDIA_DIR / f"{pk}.mp4"
    media.write_bytes(b"\x00" * (2 * 1024 * 1024))

    db.update_video(
        pk,
        media_path=str(media),
        source="youtube-auto",
        status="ready",
        detail=f"{count} 行字幕（演示数据）",
        duration_ms=cursor,
    )
    db.set_playback(pk, 0, 1.0)

    print(f"✓ 演示数据已写入 video_pk={pk}，{count} 行字幕，{len(words)} 个词")
    print(f"  视频占位文件: {media}")
    print("  打开 http://127.0.0.1:8787 就能点进去看播放器")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
