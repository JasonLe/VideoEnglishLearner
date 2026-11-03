"""按句解析：英文 → 中文译文 + 语法点 + 短语 + 语气提示。

一次喂 `llm.batch_size` 行（默认 10）给模型，要回一个严格 JSON。
为什么按批而不是一句一请求：一句一请求在 20 分钟 vlog 上要发几百次，又慢又贵，
而模型在同一次请求里看到上下文反而翻得更准。

结果落 `analysis` 表（按 video_pk + seq 缓存），重跑时只补缺的行。
"""

from __future__ import annotations

import json
from typing import Any, Callable

from . import db, llm
from .config import cfg

Progress = Callable[[int, int], None]

SYSTEM_PROMPT = """你是给中文母语者做英语精听的老师，熟悉 YouTube vlog、访谈、生活类口语。

我会给你若干行英文字幕（每行前面是行号 seq）。你要逐行给出：

1. translation：自然、口语化的中文翻译。不要机翻腔，把语气和省略的主语补出来。
2. grammar：这行里**值得讲**的语法点，0–2 条。只挑对理解或表达真正有用的：
   时态、语气（虚拟/委婉）、倒装、非谓语、冠词特例、介词搭配、常见句型。不要为了凑数硬讲
   （比如普通的一般现在时不用讲）。每条：
   - point: 语法点名称（简短，如「现在完成进行时」「虚拟语气（与过去事实相反）」）
   - explain: 一两句中文，说清结构 + 为什么这么说
   - example: 一个 vlog 口吻的英文例句
3. phrases：值得记下来的短语 / 俚语 / 固定搭配，0–3 条：
   - term: 英文原形
   - meaning: 中文简释
4. note：可选。语气、连读/弱读提示、文化梗、说话人态度。没有就给空字符串。

输出严格 JSON，格式：
{"items":[{"seq":1,"translation":"…","grammar":[{"point":"…","explain":"…","example":"…"}],
"phrases":[{"term":"…","meaning":"…"}],"note":""}]}

规则：
- 只能针对我给的 seq，每行都要有一条，不能漏、不能编。
- 不要输出 JSON 以外的任何文字。
- translation 不要照抄英文原文。
- 如果某行只有语气词（如 "yeah" "so"），translation 给自然口语译法，grammar 可为空数组。"""


def _clip(text: Any, limit: int) -> str:
    value = str(text or "").strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _clean_grammar(raw: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not isinstance(raw, list):
        return out
    for item in raw[:2]:
        if not isinstance(item, dict):
            continue
        point = _clip(item.get("point"), 60)
        explain = _clip(item.get("explain"), 300)
        if not point and not explain:
            continue
        out.append(
            {
                "point": point,
                "explain": explain,
                "example": _clip(item.get("example"), 200),
            }
        )
    return out


def _clean_phrases(raw: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not isinstance(raw, list):
        return out
    for item in raw[:3]:
        if not isinstance(item, dict):
            continue
        term = _clip(item.get("term"), 80)
        meaning = _clip(item.get("meaning"), 200)
        if not term and not meaning:
            continue
        out.append({"term": term, "meaning": meaning})
    return out


def clean_payload(raw: Any, seq: int) -> dict[str, Any] | None:
    """把模型输出洗成固定结构。缺 translation 的条目直接丢掉（宁可重试也不存半成品）。"""
    if not isinstance(raw, dict):
        return None
    translation = _clip(raw.get("translation"), 400)
    if not translation:
        return None
    return {
        "seq": int(seq),
        "translation": translation,
        "grammar": _clean_grammar(raw.get("grammar")),
        "phrases": _clean_phrases(raw.get("phrases")),
        "note": _clip(raw.get("note"), 300),
    }


def _user_prompt(batch: list[dict[str, Any]]) -> str:
    lines = "\n".join(f"{seg['seq']}. {seg['text']}" for seg in batch)
    return (
        f"共 {len(batch)} 行，行号从我给的 seq 抄，不要重新编号：\n\n{lines}\n\n"
        "按上面的 JSON 格式返回。"
    )


def _collect(data: dict[str, Any], batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从模型回包里挑出这一批的行，按 seq 对齐。"""
    wanted = {int(seg["seq"]) for seg in batch}
    items = data.get("items")
    if not isinstance(items, list):
        raise llm.LlmError("模型返回的 JSON 里没有 items 数组")

    got: dict[int, dict[str, Any]] = {}
    for idx, raw in enumerate(items):
        if not isinstance(raw, dict):
            continue
        try:
            seq = int(raw.get("seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        if seq not in wanted:
            # 有的模型会漏 seq 或者写错，退化成按顺序对齐
            seq = int(batch[idx]["seq"]) if idx < len(batch) else 0
        if seq not in wanted:
            continue
        cleaned = clean_payload(raw, seq)
        if cleaned:
            got[seq] = cleaned

    missing = [seg["seq"] for seg in batch if seg["seq"] not in got]
    if missing:
        raise llm.LlmError(
            f"模型漏掉了 {len(missing)} 行（seq={missing[:5]}…），重试一次"
        )
    return [got[int(seg["seq"])] for seg in batch]


def _analyze_batch(batch: list[dict[str, Any]], model_name: str) -> list[dict[str, Any]]:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _user_prompt(batch)},
    ]
    last: Exception | None = None
    for attempt in (1, 2):  # 网络抖动 / 模型漏行：重试一次就够，别把任务拖死
        try:
            data = llm.json_chat(messages)
            return _collect(data, batch)
        except llm.LlmError as exc:
            last = exc
            if attempt == 2:
                break
    raise last or llm.LlmError("解析失败")


def run(
    pk: int,
    on_progress: Progress | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """解析整个视频。已解析过的行默认跳过（缓存），force=True 时重跑。"""
    video = db.get_video(pk)
    if not video:
        raise RuntimeError("视频不存在")

    segments = db.get_segments(pk)
    if not segments:
        raise RuntimeError("这个视频还没有字幕，先把下载/切分跑完再解析")

    model_name = llm.model()
    db.start_analysis(pk, len(segments), model_name)

    existing = {} if force else db.get_analysis(pk)
    todo = [seg for seg in segments if int(seg["seq"]) not in existing]
    done = len(segments) - len(todo)
    if done:
        db.bump_analysis(pk, done)

    if on_progress:
        on_progress(done, len(segments))

    raw_batch = int(cfg.get("llm.batch_size", 10) or 10)
    batch_size = max(1, min(30, raw_batch))

    for start in range(0, len(todo), batch_size):
        batch = todo[start : start + batch_size]
        parsed = _analyze_batch(batch, model_name)
        for item in parsed:
            db.put_analysis(pk, int(item["seq"]), item, model_name)
        done += len(parsed)
        db.bump_analysis(pk, done)
        if on_progress:
            on_progress(done, len(segments))

    db.finish_analysis(pk, "done")
    return {
        "segments": len(segments),
        "analyzed": len(todo),
        "cached": len(existing),
        "model": model_name,
    }


def clear(pk: int) -> None:
    db.clear_analysis(pk)


def export_json(pk: int) -> dict[int, dict[str, Any]]:
    return db.get_analysis(pk)


def dump(path: str, pk: int) -> str:
    """把解析结果导成 JSON 文件，方便自己拿去看/喂给别的东西。"""
    data = db.get_analysis(pk)
    body = json.dumps(
        {str(seq): item for seq, item in sorted(data.items())},
        ensure_ascii=False,
        indent=2,
    )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path
