"""四阶段流水线：下载 → 字幕/转写 → DP 切分 → 落库。

每个阶段都写一次 db.status，Web 端轮询 2 秒就能看到进度推进。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from . import db
from .config import DATA_DIR, MEDIA_DIR, cfg, ensure_dirs, resolve_media_file
from .download import download, fetch_info
from .segment import build_segments
from .srt import save_srt
from .subtitles import load_caption_file
from .transcribe import transcribe_file

Progress = Callable[[float, str], None]


def ingest(url: str, on_progress: Progress | None = None) -> int:
    """抽元信息 + 建库，返回 video_pk。真正的重活交给 run()。"""
    info = fetch_info(url)
    if not info.video_id:
        raise RuntimeError("没能从链接里认出 YouTube 视频 ID，请贴完整链接。")
    pk = db.create_video(
        url,
        info.video_id,
        info.title,
        info.channel,
        info.duration_ms,
        info.thumb,
    )
    if on_progress:
        on_progress(0.05, f"已识别：{info.title[:60]}")
    return pk


def run(
    pk: int,
    url: str,
    on_progress: Progress | None = None,
    *,
    skip_media: bool | None = None,
    skip_subs: bool = False,
) -> dict:
    """跑完整条链路。异常往上抛，由 jobs 层记为 failed。

    三种取法：默认「视频 + 字幕」；skip_media 只取字幕；
    skip_subs 只取视频文件（状态落 media-only，之后可以单独补字幕）。
    """
    ensure_dirs()
    if skip_media is None:
        skip_media = str(cfg.get("download.mode", "video+subs")) == "subs-only"
    if skip_media and skip_subs:
        raise RuntimeError("不能同时「只要字幕」和「只要视频」。")

    def report(frac: float, detail: str) -> None:
        # 两处都写：db 给 Web 端轮询，on_progress 给命令行看实时输出
        db.set_progress(pk, frac, detail)
        if on_progress:
            on_progress(frac, detail)

    db.set_status(pk, "downloading", "只取字幕" if skip_media else "开始下载", progress=2)
    media, subs, meta = download(
        url,
        str(pk),
        lambda f, d: report(0.02 + f * 0.33, d),
        skip_media=skip_media,
        skip_subs=skip_subs,
    )
    if media is not None:
        db.update_video(pk, media_path=str(media))
    db.update_video(pk, source=meta.get("source", ""))
    subs_error = str(meta.get("subs_error") or "")
    if skip_subs:
        if media is None:
            raise RuntimeError("要求只取视频，但没拿到视频文件。检查 ffmpeg 是否可用。")
        db.set_status(pk, "media-only", "只有视频（未取字幕）", progress=100)
        report(1.0, "只有视频，未取字幕")
        return {
            "segments": 0,
            "words": 0,
            "source": "",
            "media": str(media),
            "srt": "",
            "media_only": True,
        }
    report(0.38, "字幕被 YouTube 限流，视频已保下来" if subs_error else ("视频已就绪" if media is not None else "字幕已取到"))

    words = []
    if subs:
        db.set_status(pk, "transcribing", "解析 YouTube 字幕", progress=40)
        report(0.45, f"解析字幕：{subs[0].name}")
        words = load_caption_file(subs[0])

    backend = str(cfg.get("asr.backend", "auto"))
    # 字幕接口被限流 / 网络出错，不代表「这个视频没有字幕」：等几分钟补字幕就有一秒级的
    # 结果，没必要让用户多等几分钟的本地转写。只有视频真的没有字幕才走 Whisper。
    if not words and subs_error and backend != "whisper":
        db.set_progress(pk, 0.45, "字幕被限流，等几分钟补字幕")
        raise RuntimeError(
            "YouTube 字幕下载被限流（HTTP 429）或网络出错，这次没走语音识别"
            "（它不代表视频没有字幕，白白多花几分钟）。视频已经存好了，两条路："
            "① 等 2–5 分钟点「补字幕」；② 在「设置 → 网络」填浏览器导出的 cookies 文件。"
            "真想强制本地转写就把「字幕来源」设成 whisper。"
            f"原始错误：{subs_error}"
        )

    if not words and backend in ("auto", "whisper"):
        if media is None:
            raise RuntimeError(
                "没拿到 YouTube 字幕，而 [download].mode=subs-only 时无法转写音频。"
                "把 mode 改回 video+subs 再试。"
            )
        db.set_status(pk, "transcribing", "Whisper 转写中", progress=45)
        try:
            words = transcribe_file(media, lambda f, d: report(0.45 + f * 0.32, d))
        except RuntimeError as exc:
            raise RuntimeError(
                "这个视频没有英文字幕，需要本地语音识别来转写，但 faster-whisper 没装好。"
                "装一下就能用：pip install -r requirements-asr.txt（约 150MB，"
                "首次运行还会下模型）。原始错误：" + str(exc)
            ) from exc
        meta["source"] = "whisper"
        db.update_video(pk, source="whisper")

    if not words:
        if subs_error:
            raise RuntimeError(
                "字幕被 YouTube 限流（HTTP 429），且当前没有可用的语音识别兜底。"
                "等 2–5 分钟重试，或在「设置 → 网络」里填 cookies。"
            )
        raise RuntimeError(
            "这个视频没有英文字幕，而「字幕来源」设成了 youtube（只取字幕不转写）。"
            "改成 auto 就会用 faster-whisper 本地转写（需要先装 requirements-asr.txt）。"
        )

    db.set_status(pk, "segmenting", "切分字幕行", progress=80)
    report(0.80, "按 DP 切分字幕")
    segments = build_segments(words, cfg.get)
    count = db.replace_segments(pk, segments)
    # 行切分变了，之前按 seq 缓存的解析就对不上了，一并清掉重来
    db.clear_analysis(pk)

    srt_path: Path = DATA_DIR / f"{pk}.srt"
    save_srt(segments, srt_path)

    db.set_status(pk, "ready", f"{count} 行字幕", progress=100)
    report(1.0, f"完成：{count} 行")
    return {
        "segments": count,
        "words": len(words),
        "source": meta.get("source", ""),
        "media": "" if media is None else str(media),
        "srt": str(srt_path),
    }


def refetch_subs(pk: int, url: str, on_progress: Progress | None = None) -> dict:
    """只补字幕：视频已经在磁盘上就不重复下载（429 之后最快的修法）。"""
    ensure_dirs()

    def report(frac: float, detail: str) -> None:
        db.set_progress(pk, frac, detail)
        if on_progress:
            on_progress(frac, detail)

    db.set_status(pk, "downloading", "只补字幕", progress=2)
    _media, subs, meta = download(
        url, str(pk), lambda f, d: report(0.05 + f * 0.45, d), skip_media=True
    )

    words = []
    if subs:
        db.set_status(pk, "transcribing", "解析 YouTube 字幕", progress=55)
        report(0.6, f"解析字幕：{subs[0].name}")
        words = load_caption_file(subs[0])
        if not words:
            raise RuntimeError("字幕文件解析出来是空的，可能格式不兼容。")

    if not words:
        # 补字幕时才发现这个视频确实没有字幕：本地视频已经在手，不用再下一次，
        # 直接拿它做语音识别（只在「确实没有字幕」时才走这条路）。
        backend = str(cfg.get("asr.backend", "auto"))
        local_media = resolve_media_file((db.get_video(pk) or {}).get("media_path"))
        media_path = str(local_media) if local_media else ""
        if backend == "youtube":
            raise RuntimeError(
                "这个视频确实没有英文字幕，而「字幕来源」设成了 youtube（只取字幕不转写）。"
                "改成 auto 就会用 faster-whisper 对着本地视频转写。"
            )
        if not media_path:
            raise RuntimeError(
                "这个视频没有英文字幕，本机又没有存它的视频文件，没法本地转写。"
                "用「视频 + 字幕」模式重新添加一次就会下载视频并自动转写。"
            )
        db.set_status(pk, "transcribing", "没有字幕，改用语音识别", progress=45)
        report(0.45, "视频没有字幕，改用本地语音识别")
        try:
            words = transcribe_file(Path(media_path), lambda f, d: report(0.45 + f * 0.4, d))
        except RuntimeError as exc:
            raise RuntimeError(
                "这个视频没有英文字幕，需要本地语音识别转写，但 faster-whisper 没装好。"
                "装一下就能用：pip install -r requirements-asr.txt。原始错误：" + str(exc)
            ) from exc
        meta["source"] = "whisper"
        if not words:
            raise RuntimeError("语音识别没有转出任何文字，检查视频有没有音轨。")

    db.set_status(pk, "segmenting", "切分字幕行", progress=80)
    report(0.8, "按 DP 切分字幕")
    segments = build_segments(words, cfg.get)
    count = db.replace_segments(pk, segments)
    db.clear_analysis(pk)

    srt_path: Path = DATA_DIR / f"{pk}.srt"
    save_srt(segments, srt_path)
    db.update_video(pk, source=meta.get("source", ""))
    db.set_status(pk, "ready", f"{count} 行字幕", progress=100)
    report(1.0, f"完成：{count} 行")
    return {"segments": count, "words": len(words), "source": meta.get("source", ""), "srt": str(srt_path)}
