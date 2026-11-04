"""复习调度：FSRS-4.5 的简化实现（默认参数，不做个人化训练）。

为什么不是 SM-2：FSRS 用「记忆稳定性 + 难度 + 可提取概率」建模，同等正确率下复习次数
更少、到期分布更准（Anki 社区有大规模对照）。这里只取核心公式，默认权重，够用且可解释。

输入输出都是毫秒时间戳 + 浮点天数，不依赖 datetime：
  - 评分 1=忘了 2=勉强 3=记得 4=简单（与 FSRS 的 Again/Hard/Good/Easy 对齐）
  - 只有 grade 3/4 才按「记住了」更新稳定性；grade 1 走遗忘公式并很快重新出现。
"""

from __future__ import annotations

import math
from typing import Any

DAY_MS = 86_400_000
MINUTES_MS = 60_000

# FSRS-4.5 默认权重（w0..w16）。不要微调这些数字：它们是公开基准上拟合出来的，
# 这个项目没有用户复习数据可训练，改了只会更差。
W = (
    0.4872, 1.4003, 3.7145, 13.8206, 5.1618, 1.2298, 0.8975, 0.031,
    1.6474, 0.1367, 1.0461, 2.1072, 0.0793, 0.3246, 1.587, 0.2272, 2.8755,
)

FACTOR = 19.0 / 81.0   # FSRS-4.5 的曲线常数
DECAY = -0.5
TARGET_RETENTION = 0.9  # 目标可提取概率：90% 时复习间隔约等于稳定性 S

DEFAULT_STABILITY = 0.5
DEFAULT_DIFFICULTY = 5.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def init_stability(grade: int) -> float:
    return max(0.1, W[max(0, min(3, grade - 1))])


def init_difficulty(grade: int) -> float:
    g = max(1, min(4, grade))
    return _clamp(W[4] - math.exp(W[5] * (g - 1)) + 1.0, 1.0, 10.0)


def retrievability(elapsed_days: float, stability: float) -> float:
    """经过 elapsed_days 天后还记得的概率。"""
    if stability <= 0:
        return 0.0
    return (1.0 + FACTOR * max(0.0, elapsed_days) / stability) ** DECAY


def next_difficulty(difficulty: float, grade: int) -> float:
    g = max(1, min(4, grade))
    d = difficulty - W[6] * (g - 3)
    # 向「容易」回归一点，避免难度单调漂到极端
    d = W[7] * init_difficulty(4) + (1.0 - W[7]) * d
    return _clamp(d, 1.0, 10.0)


def next_recall_stability(stability: float, difficulty: float, r: float, grade: int) -> float:
    hard_penalty = W[15] if grade == 2 else 1.0
    easy_bonus = W[16] if grade == 4 else 1.0
    growth = (
        math.exp(W[8])
        * (11.0 - difficulty)
        * (stability ** -W[9])
        * (math.exp(W[10] * (1.0 - r)) - 1.0)
        * hard_penalty
        * easy_bonus
    )
    return max(0.1, stability * (1.0 + growth))


def next_forget_stability(stability: float, difficulty: float, r: float) -> float:
    value = (
        W[11]
        * (difficulty ** -W[12])
        * ((stability + 1.0) ** W[13] - 1.0)
        * math.exp(W[14] * (1.0 - r))
    )
    return max(0.1, value)


def interval_days(stability: float, retention: float = TARGET_RETENTION) -> float:
    """目标保持率 retention 下的间隔天数（FSRS 里 R(S, S)=0.9 的定义）。"""
    raw = stability / FACTOR * (retention ** (1.0 / DECAY) - 1.0)
    return _clamp(raw, 1.0, 365.0 * 10)


def schedule(state: dict[str, Any] | None, grade: int, now_ms: int) -> dict[str, Any]:
    """根据旧状态和本次评分，算出新状态与到期时间。

    state 为 None 表示第一次复习。返回：
      stability / difficulty / due_at(ms) / reps / lapses / interval_days / last_review
    """
    grade = max(1, min(4, int(grade)))
    now_ms = int(now_ms or 0)
    s = float((state or {}).get("stability") or 0.0)
    d = float((state or {}).get("difficulty") or 0.0)
    reps = int((state or {}).get("reps") or 0)
    lapses = int((state or {}).get("lapses") or 0)
    last = int((state or {}).get("last_review") or 0)

    first = reps <= 0 or s <= 0.0
    if first:
        stability = init_stability(grade)
        difficulty = init_difficulty(grade)
        elapsed_days = 0.0
    else:
        elapsed_days = max(0.0, (now_ms - last) / DAY_MS) if last else 0.0
        r = retrievability(elapsed_days, s)
        difficulty = next_difficulty(d or DEFAULT_DIFFICULTY, grade)
        if grade == 1:
            stability = next_forget_stability(s, difficulty, r)
        else:
            stability = next_recall_stability(s, difficulty, r, grade)

    reps += 1
    if grade == 1 and not first:
        lapses += 1

    # 忘了：10 分钟后立刻再见一面（当天再看到，比隔天更符合「重新学」的直觉）；
    # 其余按 FSRS 间隔，最少 1 天。
    if grade == 1:
        interval = 0.0
        due_at = now_ms + 10 * MINUTES_MS
    else:
        interval = interval_days(stability)
        due_at = now_ms + int(interval * DAY_MS)

    return {
        "stability": round(stability, 4),
        "difficulty": round(difficulty, 4),
        "due_at": int(due_at),
        "reps": reps,
        "lapses": lapses,
        "interval_days": round(interval, 2),
        "last_review": now_ms,
        "elapsed_days": round(elapsed_days, 2),
    }
