"""字幕切分：把词级时间戳切成「一行一行」的字幕。

这里就是 VideoLingo `_3_2_split_meaning.py` 的替代品：它用「spacy 找切点 + LLM 选切点」，
本阶段没有 LLM，改用 **DP 最优切分** —— 把「切在哪」变成一个带代价的最短路问题：

    代价 = 在非自然边界处切(贵) + 行长偏离理想长度(平方惩罚) + 行太短会闪(小惩罚)

好处是接上 LLM 之后结构完全不用动：只需要把 `_break_cost()` 换成模型打分即可。
"""

from __future__ import annotations

from .models import Segment, Word
from .text import SOFT_BREAK, join_words, visual_len

# 在这些词前面断开，比随便断要自然
CONJ_START = {
    "and", "or", "but", "so", "because", "although", "though", "while", "when",
    "whereas", "if", "which", "that", "who", "whose", "where", "unless", "until",
    "since", "after", "before", "as", "than", "yet", "nor", "for",
}

# 行首出现这些词会让字幕读起来别扭（"the / beautiful day"）
BAD_LINE_START = {
    "a", "an", "the", "to", "of", "in", "on", "at", "for", "with", "from", "by",
    "as", "is", "are", "was", "were", "be", "been", "being", "that", "which",
}

# 行尾停在这些词上同样别扭（"I want to / go"）
BAD_LINE_END = {
    "a", "an", "the", "to", "of", "in", "on", "at", "for", "with", "from", "by",
    "and", "or", "but", "is", "are", "was", "were", "that", "which", "who",
    "my", "your", "his", "her", "its", "their", "our", "this", "these", "those",
}

SINGLE_WORD_PENALTY = 60.0
MAX_CHARS_HARD_PENALTY = 400.0


def group_sentences(words: list[Word]) -> list[list[Word]]:
    """先按句末标点分句：句子边界是硬边界，绝不跨句连成一行。"""
    from .text import ends_sentence

    groups: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        if not w.text.strip():
            continue
        cur.append(w)
        if ends_sentence(w.text):
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    return groups


def _break_cost(prev_word: Word, next_word: Word) -> float:
    """在 prev|next 之间断开的代价，0 最自然。"""
    prev_text = prev_word.text.strip()
    next_text = next_word.text.strip().lower()

    if prev_text and prev_text[-1] in SOFT_BREAK:
        cost = 0.0
    elif next_text in CONJ_START:
        cost = 0.8
    else:
        cost = 3.0

    if next_text in BAD_LINE_START:
        cost += 2.5
    if prev_text.rstrip(",;:—-").lower() in BAD_LINE_END:
        cost += 2.5
    return cost


def _line_cost(line: list[Word], target_chars: int, min_ms: int) -> float:
    text = join_words([w.text for w in line])
    dev = (visual_len(text) - target_chars) / max(target_chars, 1)
    cost = 1.6 * dev * dev
    dur = line[-1].end_ms - line[0].start_ms
    if dur < min_ms:
        cost += 0.8 * (min_ms - dur) / max(min_ms, 1)
    return cost


def dp_split(
    words: list[Word],
    *,
    max_chars: int = 42,
    target_chars: int = 32,
    max_ms: int = 7000,
    min_ms: int = 900,
) -> list[list[Word]]:
    """单句内的最优切分。返回若干行，每行是原始 Word 的子列表。"""
    n = len(words)
    if n == 0:
        return []
    if n == 1:
        return [words]

    INF = float("inf")
    best = [INF] * (n + 1)
    backtrack = [-1] * (n + 1)
    best[0] = 0.0

    for j in range(1, n + 1):
        # i 从 j-1 往回走：先试「最后一行只有一个词」，再逐步变长
        for i in range(j - 1, -1, -1):
            line = words[i:j]
            single = (j - i) == 1
            length = visual_len(join_words([w.text for w in line]))
            duration = line[-1].end_ms - line[0].start_ms
            over_chars = length > max_chars
            over_time = duration > max_ms

            if (over_chars or over_time) and not single:
                # 行已经超限，再往前扩只会更长更慢，直接收工
                break

            if single and (over_chars or over_time):
                # 单个词就超限（数字、专有名词），只能硬切，代价拉高
                step = best[i] + (MAX_CHARS_HARD_PENALTY if over_chars else 0.0)
            else:
                step = best[i] + (0.0 if i == 0 else _break_cost(words[i - 1], words[i]))
                step += _line_cost(line, target_chars, min_ms)

            if step < best[j]:
                best[j] = step
                backtrack[j] = i

    lines: list[list[Word]] = []
    j = n
    while j > 0:
        i = backtrack[j]
        if i < 0:  # 理论上不会发生；兜底逐词一行
            i = j - 1
        lines.append(words[i:j])
        j = i
    lines.reverse()
    return lines


def build_segments(words: list[Word], get) -> list[Segment]:
    """词序列 → 行级字幕。get 是 `config.get`，便于单测注入。"""
    max_chars = int(get("segment.max_chars", 42))
    target_chars = int(get("segment.target_chars", 32))
    max_ms = int(get("segment.max_ms", 7000))
    min_ms = int(get("segment.min_ms", 900))

    segments: list[Segment] = []
    for sentence in group_sentences(words):
        for line in dp_split(
            sentence,
            max_chars=max_chars,
            target_chars=target_chars,
            max_ms=max_ms,
            min_ms=min_ms,
        ):
            start = line[0].start_ms
            end = max(line[-1].end_ms, start + 1)
            segments.append(
                Segment(
                    seq=len(segments),
                    start_ms=start,
                    end_ms=end,
                    text=join_words([w.text for w in line]),
                    words=list(line),
                )
            )
    return segments


def distribute_words(start_ms: int, end_ms: int, text: str) -> list[Word]:
    """没有词级时间戳时（VTT 回退路径），按字符数比例把时间摊到每个词上。

    精度不如真时间戳，但足够支撑「读到这里高亮到这里」。
    """
    tokens = [t for t in str(text).split() if t]
    if not tokens:
        return []
    weights = [max(visual_len(t), 0.5) for t in tokens]
    total = sum(weights) or 1.0
    span = max(end_ms - start_ms, 1)

    out: list[Word] = []
    acc = 0.0
    for i, (token, weight) in enumerate(zip(tokens, weights)):
        w_start = int(round(start_ms + span * acc / total))
        acc += weight
        w_end = end_ms if i == len(tokens) - 1 else int(round(start_ms + span * acc / total))
        if w_end <= w_start:
            w_end = w_start + 1
        out.append(Word(text=token, start_ms=w_start, end_ms=min(w_end, end_ms)))
    return out
