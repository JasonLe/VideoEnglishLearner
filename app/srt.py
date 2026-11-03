"""导出 SRT —— 主要给 M0 阶段人工校验切分效果用。"""

from __future__ import annotations

from pathlib import Path

from .models import Segment


def format_ts(ms: int) -> str:
    ms = max(int(ms), 0)
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def to_srt(segments: list[Segment]) -> str:
    blocks = []
    for i, seg in enumerate(segments, start=1):
        blocks.append(
            f"{i}\n{format_ts(seg.start_ms)} --> {format_ts(seg.end_ms)}\n{seg.text}\n"
        )
    return "\n".join(blocks)


def save_srt(segments: list[Segment], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_srt(segments), encoding="utf-8")
    return path


def to_vtt(segments: list[Segment]) -> str:
    """顺手给浏览器 <track> 用（可选）。"""

    def ts(ms: int) -> str:
        return format_ts(ms).replace(",", ".")

    lines = ["WEBVTT", ""]
    for seg in segments:
        lines.append(f"{ts(seg.start_ms)} --> {ts(seg.end_ms)}")
        lines.append(seg.text)
        lines.append("")
    return "\n".join(lines)
