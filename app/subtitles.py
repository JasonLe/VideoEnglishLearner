"""把字幕文件解析成词级时间戳。

优先级：json3（YouTube 官方/自动字幕的原生格式，自带逐词 tOffsetMs）> vtt > srt。
vtt/srt 没有词级时间戳，用 `segment.distribute_words()` 按字符比例摊。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .models import Word
from .segment import distribute_words

_TAG_RE = re.compile(r"<[^>]*>")
_TS_RE = re.compile(r"(\d+):(\d{2}):(\d{2})[.,](\d{1,3})")


def _to_ms(h: str, m: str, s: str, frac: str) -> int:
    return (
        int(h) * 3600_000
        + int(m) * 60_000
        + int(s) * 1000
        + int(frac.ljust(3, "0")[:3])
    )


# ------------------------------------------------------------------ json3

def parse_json3(path: str | Path) -> list[Word]:
    """YouTube json3：events[] -> segs[] -> {utf8, tOffsetMs}。

    有两种截然不同的数据：
    - 自动字幕：segs 里每个词带 tOffsetMs（词级时间戳），但下一行会滚动重复上一行。
    - 人工字幕：每个 event 就是整行/两行文本，**没有** tOffsetMs，只有 cue 级时间。
    分开处理：前者走字符流重组词，后者走 distribute_words 摊时间。
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    events = [ev for ev in (data.get("events") or []) if ev.get("tStartMs") is not None]
    if not events:
        return []

    word_level = any(
        seg.get("tOffsetMs") is not None
        for ev in events
        for seg in (ev.get("segs") or [])
    )
    if word_level:
        return _words_from_word_segs(events)
    return _words_from_cues(events)


def _norm_body(text: str) -> str:
    body = _TAG_RE.sub("", text)
    body = body.replace("\\n", " ").replace("\u00a0", " ")
    return re.sub(r"\\s+", " ", body).strip()


def _words_from_cues(events: list[dict]) -> list[Word]:
    """人工字幕：只有 cue 级时间，按字符比例摊到词上。"""
    words: list[Word] = []
    prev = None
    for ev in events:
        body = _norm_body(" ".join(seg.get("utf8") or "" for seg in ev.get("segs") or []))
        if not body or body == prev:
            continue
        prev = body
        start = int(ev["tStartMs"])
        dur = int(ev.get("dDurationMs") or 0) or max(2000, len(body) * 60)
        words.extend(distribute_words(start, start + dur, body))
    return words


def _words_from_word_segs(events: list[dict]) -> list[Word]:
    """自动字幕：每个 seg 是一个词（或词的碎片），带相对 tOffsetMs。"""
    chars: list[tuple[str, int]] = []
    seen: set[tuple[int, str]] = set()
    for ev in events:
        base = int(ev["tStartMs"])
        for seg in ev.get("segs") or []:
            text = seg.get("utf8") or ""
            start = base + int(seg.get("tOffsetMs") or 0)
            if not text.strip():
                # 纯空白 seg 也是分隔符，丢了会把两个词粘在一起
                if chars:
                    chars.append((" ", start))
                continue
            key = (start, text)
            if key in seen:  # 滚动字幕重复播的同一行
                continue
            seen.add(key)
            if chars and not chars[-1][0].isspace() and not text[0].isspace():
                chars.append((" ", start))
            chars.extend((ch, start) for ch in text)

    if not chars:
        return []

    words: list[Word] = []
    buf: list[str] = []
    buf_start = 0
    for ch, t in chars:
        if ch.isspace():
            if buf:
                words.append(Word("".join(buf), buf_start, buf_start))
                buf = []
            continue
        if not buf:
            buf_start = t
        buf.append(ch)
    if buf:
        words.append(Word("".join(buf), buf_start, buf_start))

    # 时间戳偶尔会倒退（滚动窗口的 base 有抖动），强制单调
    for i in range(1, len(words)):
        if words[i].start_ms < words[i - 1].start_ms:
            words[i].start_ms = words[i - 1].start_ms
    return _fill_ends(words)


def _fill_ends(words: list[Word]) -> list[Word]:
    """json3 只有起点，终点用下一个词的起点，最后一个词按平均语速估。"""
    if not words:
        return []
    if len(words) > 1:
        total_span = max(words[-1].start_ms - words[0].start_ms, 1)
        avg_per_word = total_span / (len(words) - 1)
    else:
        avg_per_word = 350.0
    for i, w in enumerate(words):
        if i + 1 < len(words):
            nxt = words[i + 1].start_ms
            w.end_ms = nxt if nxt > w.start_ms else w.start_ms + 200
        else:
            w.end_ms = w.start_ms + int(avg_per_word)
    return words


# --------------------------------------------------------------- vtt / srt

def _parse_cue_blocks(text: str) -> list[tuple[int, int, str]]:
    """粗粒度解析 vtt/srt 的 cue 块。"""
    cues: list[tuple[int, int, str]] = []
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    i = 0
    while i < len(lines):
        match = _TS_RE.search(lines[i])
        if not match or "-->" not in lines[i]:
            i += 1
            continue

        start_ms = _to_ms(*match.groups())
        tail = lines[i].split("-->", 1)[1]
        end_match = _TS_RE.search(tail)
        end_ms = _to_ms(*end_match.groups()) if end_match else start_ms + 2000

        i += 1
        payload: list[str] = []
        while i < len(lines) and lines[i].strip() and "-->" not in lines[i]:
            payload.append(lines[i])
            i += 1

        body = " ".join(payload)
        body = _TAG_RE.sub("", body)
        body = re.sub(r"\s+", " ", body).strip()
        if body:
            cues.append((start_ms, max(end_ms, start_ms + 1), body))
    return cues


def parse_vtt(path: str | Path) -> list[Word]:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    words: list[Word] = []
    prev_body = None
    for start_ms, end_ms, body in _parse_cue_blocks(text):
        if body == prev_body:  # 滚动字幕的重复行
            continue
        prev_body = body
        words.extend(distribute_words(start_ms, end_ms, body))
    return words


def parse_srt(path: str | Path) -> list[Word]:
    return parse_vtt(path)  # 时间轴格式一致（毫秒分隔符兼容）


# ------------------------------------------------------------------ 入口

def load_caption_file(path: str | Path) -> list[Word]:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".json3":
        return parse_json3(path)
    if suffix in (".vtt", ".srt", ".srv1", ".srv2", ".srv3"):
        return parse_vtt(path)
    # 未知后缀：看内容决定
    head = path.read_text(encoding="utf-8", errors="replace").lstrip()
    if head.startswith("{"):
        return parse_json3(path)
    if head.startswith("WEBVTT"):
        return parse_vtt(path)
    return parse_srt(path)


def find_subtitle_files(directory: str | Path, stem: str) -> list[Path]:
    """找出 yt-dlp 落盘的 `<stem>.<lang>.<ext>` 字幕文件，英文优先。"""
    directory = Path(directory)
    candidates = [p for p in directory.glob(f"{stem}.*") if p.suffix.lower() in (
        ".json3", ".vtt", ".srt", ".srv1", ".srv2", ".srv3"
    )]
    if not candidates:
        return []

    def rank(p: Path) -> tuple[int, int]:
        parts = p.name.lower().split(".")
        lang = parts[-2] if len(parts) >= 2 else ""
        lang_score = 0 if lang.startswith("en") else 1
        # json3 优先（有词级时间戳），其次 vtt
        ext_score = 0 if p.suffix.lower() == ".json3" else 1
        return (lang_score, ext_score)

    return sorted(candidates, key=rank)
