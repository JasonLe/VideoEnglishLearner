"""视频词汇覆盖率：这个视频里你认识多少词。

为什么重要：可理解输入的门槛大约在 95%（能猜），98% 才能不靠字幕听懂。覆盖率是
「这条视频现在适不适合你」的量化信号，也决定生词本要先学哪些词。

统计口径（与 docs/vocab-dict-api.md 一致）：
- 字幕按词切分、小写，去掉纯数字和单字母（a / i 除外）；
- 已知 = 词频排名 <= baseline_rank（默认 2000）或 vocab.status = 'known'；
- 主指标是 token 覆盖率（每个词按出现次数算），unique 只是参考。
"""

from __future__ import annotations

import re
from typing import Any

from . import db, dict as dictionary

TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'\u2019\-]*")
BASELINE_RANK = 2000
TARGET = 0.98
MAX_UNKNOWN = 120


def _tokens(text: str) -> list[str]:
    out: list[str] = []
    for raw in TOKEN_RE.findall(text or ""):
        word = raw.lower().replace("\u2019", "'").strip("'-")
        if not word:
            continue
        if len(word) == 1 and word not in ("a", "i"):
            continue
        out.append(word)
    return out


def coverage(video_pk: int, *, baseline_rank: int = BASELINE_RANK) -> dict[str, Any]:
    segments = db.get_segments(video_pk)
    tokens: list[str] = []
    for segment in segments:
        tokens.extend(_tokens(segment.get("text") or ""))

    dict_ready = dictionary.is_installed()
    meta = dictionary.meta_batch(tokens) if (dict_ready and tokens) else {}

    marks = db.vocab_status_map()
    known_tokens = 0
    known_unique: set[str] = set()
    unique = {t for t in tokens}
    counts: dict[str, int] = {}
    for token in tokens:
        info = meta.get(token) or {}
        lemma = info.get("lemma") or token
        rank = int(info.get("rank") or 0)
        status = (marks.get(token) or marks.get(lemma) or {}).get("status")
        is_known = status == "known" or (bool(rank) and rank <= baseline_rank)
        if is_known:
            known_tokens += 1
            known_unique.add(token)
        else:
            counts[token] = counts.get(token, 0) + 1

    total = len(tokens)
    unknown = []
    for word, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_UNKNOWN]:
        info = meta.get(word) or {}
        lemma = info.get("lemma") or word
        status = (marks.get(word) or marks.get(lemma) or {}).get("status")
        unknown.append({
            "word": word,
            "count": count,
            "lemma": lemma,
            "rank": int(info.get("rank") or 0),
            "in_vocab": status is not None,
            "status": status,
        })

    return {
        "dict_ready": dict_ready,
        "tokens": total,
        "unique": len(unique),
        "known_tokens": known_tokens,
        "known_unique": len(known_unique),
        "coverage": round(known_tokens / total, 4) if total else 0.0,
        "baseline_rank": baseline_rank,
        "target": TARGET,
        "unknown": unknown,
        "unknown_tokens": total - known_tokens,
    }
