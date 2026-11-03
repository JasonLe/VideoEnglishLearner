"""文本工具：拼词、视觉长度、断句类型判定。"""

from __future__ import annotations

import re

# 视觉宽度权重移植自 VideoLingo 的 calc_len：
# 中日文 / 全角符号按 1.75 个英文字符宽计，韩文 1.5，泰文 1，其余 1。
_CJK_JP = ((0x4E00, 0x9FFF), (0x3040, 0x30FF), (0x3400, 0x4DBF))
_KOREAN = ((0xAC00, 0xD7A3), (0x1100, 0x11FF))
_THAI = ((0x0E00, 0x0E7F),)
_FULLWIDTH = ((0xFF01, 0xFF5E), (0x3000, 0x303F))


def _in_ranges(code: int, ranges) -> bool:
    return any(lo <= code <= hi for lo, hi in ranges)


def visual_len(text: str) -> float:
    """按屏幕视觉宽度计长度：英文 1，中日 1.75，韩 1.5，泰 1。"""
    total = 0.0
    for ch in str(text):
        code = ord(ch)
        if _in_ranges(code, _CJK_JP) or _in_ranges(code, _FULLWIDTH):
            total += 1.75
        elif _in_ranges(code, _KOREAN):
            total += 1.5
        elif _in_ranges(code, _THAI):
            total += 1.0
        else:
            total += 1.0
    return total


# 这些字符前面不留空格，后面留。
_NO_SPACE_BEFORE = frozenset(",.;:!?%)\\]}>»”’'\"…、，。！？；：）】》")
_NO_SPACE_AFTER = frozenset("([{<«“‘（【《")

# "don ' t" -> "don't"；只合并常见缩写，避免把引号也吃掉
_CONTRACTION = re.compile(
    r"(?<![A-Za-z])([A-Za-z]+)\s+'\s*(?=(?:t|s|re|ve|ll|d|m)\b)", re.IGNORECASE
)


def join_words(words) -> str:
    """把词序列拼成自然的一句话：补空格、去标点前空格、合并 don ' t。

    words 可以是 str，也可以是带 .text 的 Word 对象。
    """
    parts = []
    for raw in words:
        text = str(getattr(raw, "text", raw)).strip()
        if text:
            parts.append(text)
    if not parts:
        return ""

    out = parts[0]
    for part in parts[1:]:
        if part[0] in _NO_SPACE_BEFORE or out[-1] in _NO_SPACE_AFTER:
            out += part
        else:
            out += " " + part

    return _CONTRACTION.sub(r"\1'", out).strip()


SENT_END = frozenset(".!?…。！？")
SOFT_BREAK = frozenset(",;:—–-、，；：")
ABBREV = {
    "mr", "mrs", "ms", "dr", "prof", "st", "jr", "sr", "vs", "etc",
    "e.g", "i.e", "u.s", "a.m", "p.m", "no", "fig", "approx", "inc", "ltd",
}


def ends_sentence(token: str) -> bool:
    """判断这个词是否结束一个句子（带缩略语保护）。"""
    t = str(token).strip()
    if not t:
        return False
    last = t[-1]
    if last not in SENT_END:
        return False
    if last == ".":
        core = t[:-1].strip().lower()
        if core in ABBREV:
            return False
        # 单字母 + 句点：多半是缩写（A. / J.）
        if len(core) == 1 and core.isalpha():
            return False
    return True
