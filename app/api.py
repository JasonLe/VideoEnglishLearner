"""HTTP API。前端只有这一层，没有别的服务端逻辑。

`/api/videos/{pk}/media` 自己实现了 HTTP Range：
浏览器拖动进度条时靠 206 分段读取，用 FileResponse 的话 seek 会卡。
"""

from __future__ import annotations

import mimetypes
import re
from pathlib import Path
from typing import Any, Iterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from . import db, jobs, settings
from .config import DATA_DIR, MEDIA_DIR, cfg, resolve_media_file

router = APIRouter(prefix="/api")

CHUNK_SIZE = 512 * 1024
_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")

_EXTRA_MIME = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
}


def guess_mime(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in _EXTRA_MIME:
        return _EXTRA_MIME[ext]
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


MODES = {"": "ingest", "video+subs": "ingest", "subs-only": "ingest-subs", "video-only": "ingest-video"}


class CreateBody(BaseModel):
    url: str
    mode: str = ""


class PlaybackBody(BaseModel):
    position_ms: int = 0
    rate: float | None = None


class SettingsBody(BaseModel):
    values: dict[str, Any] = {}


class FfmpegCheckBody(BaseModel):
    path: str = ""


class AnalyzeBody(BaseModel):
    force: bool = False


class LlmCheckBody(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""


def _jobs_payload() -> dict[str, Any]:
    """任务队列 + 当前任务对应的进度，供顶栏和卡片显示。

    进度不存队列里，而是现读 db：任务本身就在写 db，队列只负责「谁在跑」。
    """
    state = jobs.status()
    current = state.get("current")
    if current:
        video = db.get_video(current["pk"]) or {}
        if current["kind"] == "analyze":
            total = int(video.get("analysis_total") or 0)
            done = int(video.get("analysis_done") or 0)
            percent = int(done * 100 / total) if total else 0
            current = dict(
                current,
                progress=percent,
                stage="analyzing",
                detail=f"解析中 {done}/{total}" if total else "解析中",
            )
        else:
            current = dict(
                current,
                progress=int(video.get("progress") or 0),
                stage=video.get("status") or "",
                detail=video.get("detail") or "",
            )
    return {"queued": state["queued"], "current": current}


def _require(pk: int) -> dict[str, Any]:
    video = db.get_video(pk)
    if not video:
        raise HTTPException(status_code=404, detail="视频不存在")
    return video


@router.get("/health")
def health() -> dict[str, Any]:
    from . import llm
    from .download import ffmpeg_status

    ff = ffmpeg_status()
    return {
        "ok": True,
        "jobs": _jobs_payload(),
        "ffmpeg": bool(ff.get("ok")),
        "ffmpeg_exe": ff.get("exe", ""),
        "ffmpeg_version": ff.get("version", ""),
        "ffmpeg_message": ff.get("message", ""),
        "download_mode": str(cfg.get("download.mode", "video+subs")),
        "asr_backend": str(cfg.get("asr.backend", "auto")),
        "proxy": bool(cfg.proxy),
        "llm": {
            "configured": llm.configured(),
            "base_url": llm.base_url(),
            "model": llm.model(),
        },
    }


@router.get("/settings")
def get_settings() -> dict[str, Any]:
    return settings.snapshot()


@router.put("/settings")
def put_settings(body: SettingsBody) -> dict[str, Any]:
    try:
        return settings.apply(body.values)
    except settings.SettingsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/settings/ffmpeg-check")
def ffmpeg_check(body: FfmpegCheckBody) -> dict[str, Any]:
    return settings.check_ffmpeg(body.path)


@router.post("/settings/llm-check")
def llm_check(body: LlmCheckBody) -> dict[str, Any]:
    from . import llm

    return llm.probe(body.base_url, body.api_key, body.model)


@router.post("/videos")
def create_video(body: CreateBody) -> dict[str, Any]:
    url = body.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="链接不能为空")
    from .download import video_id_from_url

    if not video_id_from_url(url):
        raise HTTPException(
            status_code=400,
            detail="只支持 YouTube 链接：https://www.youtube.com/watch?v=… 或 https://youtu.be/…",
        )
    mode = (body.mode or "").strip()
    if mode not in MODES:
        raise HTTPException(
            status_code=400,
            detail="mode 只能是 video+subs / subs-only / video-only 三种之一",
        )
    kind = MODES[mode]

    try:
        from . import pipeline

        pk = pipeline.ingest(url)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    video = db.get_video(pk)
    if not video:
        raise HTTPException(status_code=500, detail="入库失败")

    # 已经就绪的不重复排队
    if video["status"] != "ready":
        db.set_status(pk, "queued", "排队中", progress=0)
        jobs.enqueue(pk, url, kind)
    return {"id": pk}


@router.get("/videos")
def list_videos() -> list[dict[str, Any]]:
    rows = db.list_videos()
    for row in rows:
        row["segments_count"] = db.count_segments(row["id"])
    return rows


@router.get("/videos/{pk}")
def get_video(pk: int) -> dict[str, Any]:
    video = _require(pk)
    video["playback"] = db.get_playback(pk)
    return video


@router.get("/videos/{pk}/subtitle-file")
def get_subtitle_file(pk: int):
    video = _require(pk)
    path = DATA_DIR / f"{pk}.srt"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="还没有字幕文件（视频没处理完，或没取到字幕）")
    name = re.sub(r"[^\w一-龥-]+", "_", str(video["title"] or f"video-{pk}"))[:60]
    return FileResponse(path, media_type="text/plain; charset=utf-8", filename=f"{name}.srt")


@router.get("/videos/{pk}/subtitles")
def get_subtitles(pk: int) -> dict[str, Any]:
    _require(pk)
    return {"segments": db.get_segments(pk)}


@router.post("/videos/{pk}/analyze")
def analyze_video(pk: int, body: AnalyzeBody | None = None) -> dict[str, Any]:
    from . import llm

    video = _require(pk)
    force = bool(body and body.force)
    if video["status"] != "ready" and not force:
        raise HTTPException(status_code=400, detail="视频还没处理完，等字幕切分完再解析")
    if not db.get_segments(pk):
        raise HTTPException(status_code=400, detail="这个视频还没有字幕，无法解析")
    if jobs.is_running(pk, "analyze") or video["analysis_status"] in ("queued", "running"):
        raise HTTPException(status_code=409, detail="正在解析中，稍等一下")
    if not llm.configured():
        raise HTTPException(
            status_code=400,
            detail="还没配大模型：右上角「设置」→「解析」分组，填 base_url / api_key / model 后点「检测」",
        )
    cached = db.count_analysis(pk)
    db.mark_analysis_queued(pk)
    jobs.enqueue_analyze(pk, video["url"], force=force)
    return {"queued": True, "total": len(db.get_segments(pk)), "cached": cached}


@router.get("/videos/{pk}/analysis")
def get_analysis(pk: int) -> dict[str, Any]:
    video = _require(pk)
    items = db.get_analysis(pk)
    return {
        "status": video["analysis_status"] or "none",
        "done": int(video["analysis_done"] or len(items)),
        "total": int(video["analysis_total"] or 0),
        "model": video["analysis_model"] or "",
        "error": video["analysis_error"] or "",
        "items": {str(seq): payload for seq, payload in sorted(items.items())},
    }


@router.delete("/videos/{pk}/analysis")
def delete_analysis(pk: int) -> dict[str, Any]:
    _require(pk)
    if jobs.is_running(pk, "analyze"):
        raise HTTPException(status_code=409, detail="正在解析中，等它跑完再清空")
    db.clear_analysis(pk)
    return {"ok": True}


@router.patch("/videos/{pk}/playback")
@router.post("/videos/{pk}/playback")  # sendBeacon 只能 POST（关页/暂停时存进度）
def save_playback(pk: int, body: PlaybackBody) -> dict[str, Any]:
    _require(pk)
    db.set_playback(pk, max(int(body.position_ms), 0), body.rate)
    return {"ok": True}


@router.post("/videos/{pk}/retry")
def retry_video(pk: int) -> dict[str, Any]:
    video = _require(pk)
    rows_existing = len(db.get_segments(pk))
    # 已经好了就别动它：误点一次「重试」不该把能看的视频重新排队
    if video["status"] == "ready" and rows_existing > 0:
        return {"ok": True, "kind": "noop"}
    media = resolve_media_file(video["media_path"])
    has_media = media is not None
    # 视频在、只是没字幕（典型是 429）→ 只补字幕，别重下几十兆
    kind = "subs" if has_media and rows_existing == 0 else "ingest"
    db.set_status(pk, "queued", "重新排队", progress=0)
    jobs.enqueue(pk, video["url"], kind)
    return {"ok": True, "kind": kind}


@router.post("/videos/{pk}/subs")
def fetch_subs(pk: int) -> dict[str, Any]:
    """只补字幕：视频已在本地时用这个，不重新下载视频。"""
    video = _require(pk)
    if jobs.is_running(pk, "subs") or jobs.is_running(pk, "ingest"):
        raise HTTPException(status_code=409, detail="这个视频已经在处理中")
    db.set_status(pk, "queued", "排队补字幕", progress=0)
    jobs.enqueue_subs(pk, video["url"])
    return {"queued": True}


@router.delete("/videos/{pk}")
def delete_video(pk: int) -> dict[str, Any]:
    video = _require(pk)
    media = resolve_media_file(video["media_path"])
    if media is not None:
        try:
            media.unlink()
        except OSError:
            pass
    for extra in (DATA_DIR / f"{pk}.srt", *MEDIA_DIR.glob(f"{pk}.*")):
        try:
            if extra.is_file():
                extra.unlink()
        except OSError:
            pass
    db.delete_video(pk)
    return {"ok": True}


def _iter_bytes(path: Path, start: int, length: int) -> Iterator[bytes]:
    with path.open("rb") as fh:
        fh.seek(start)
        remaining = length
        while remaining > 0:
            chunk = fh.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


@router.get("/videos/{pk}/media")
def get_media(pk: int, request: Request):
    video = _require(pk)
    path = resolve_media_file(video["media_path"])
    if path is None:
        raise HTTPException(status_code=404, detail="视频文件不在磁盘上，可能已被清理")

    size = path.stat().st_size
    mime = guess_mime(path)
    base_headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600"}

    raw_range = request.headers.get("range") or request.headers.get("Range")
    if not raw_range:
        headers = dict(base_headers, **{"Content-Length": str(size)})
        return StreamingResponse(
            _iter_bytes(path, 0, size), media_type=mime, headers=headers
        )

    match = _RANGE_RE.match(raw_range.strip())
    if not match:
        raise HTTPException(status_code=416, detail="Range 头不支持")

    start_raw, end_raw = match.group(1), match.group(2)
    if start_raw == "" and end_raw == "":
        raise HTTPException(status_code=416, detail="Range 头为空")

    if start_raw == "":  # bytes=-N  末尾 N 字节
        length = min(int(end_raw), size)
        start = size - length
        end = size - 1
    else:
        start = int(start_raw)
        end = int(end_raw) if end_raw else size - 1
        end = min(end, size - 1)

    if start >= size or start > end:
        raise HTTPException(
            status_code=416,
            detail="请求范围超出文件",
            headers={"Content-Range": f"bytes */{size}"},
        )

    length = end - start + 1
    headers = dict(
        base_headers,
        **{
            "Content-Length": str(length),
            "Content-Range": f"bytes {start}-{end}/{size}",
        },
    )
    return StreamingResponse(
        _iter_bytes(path, start, length),
        status_code=206,
        media_type=mime,
        headers=headers,
    )
