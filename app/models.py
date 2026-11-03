"""核心数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Word:
    """词级时间戳。start/end 都是毫秒，相对视频开头。"""

    text: str
    start_ms: int
    end_ms: int


@dataclass(slots=True)
class Segment:
    """一行字幕。"""

    seq: int
    start_ms: int
    end_ms: int
    text: str
    words: list[Word] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "text": self.text,
            "words": [
                {"text": w.text, "start_ms": w.start_ms, "end_ms": w.end_ms}
                for w in self.words
            ],
        }


@dataclass(slots=True)
class MediaInfo:
    """yt-dlp 抽到的元信息。"""

    video_id: str
    title: str = ""
    channel: str = ""
    duration_ms: int = 0
    thumb: str = ""
    webpage_url: str = ""
