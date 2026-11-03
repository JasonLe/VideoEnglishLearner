"""进程内单 worker 任务队列。

单人本机工具没必要上 Celery/Redis：一个后台线程 + Queue 足够，
而且进程重启后 `recover_interrupted()` 会把卡住的记录标成失败。

队列里的任务：
- `ingest`      ：下载 + 字幕/转写 + 切分（pipeline.run）
- `ingest-subs` ：只取字幕并切分（pipeline.run skip_media=True）
- `ingest-video`：只取视频文件（pipeline.run skip_subs=True）
- `subs`        ：补字幕，不重下视频（pipeline.refetch_subs）
- `analyze`     ：调大模型逐句解析（analyze.run）
串行执行，一次一件 —— 转写和解析都在抢 CPU/网络，并行只会互相拖。
"""

from __future__ import annotations

import queue
import threading
import traceback
from typing import Any

from . import analyze, db, pipeline

Task = tuple[int, str, str, bool]  # (video_pk, url, kind, force)

_queue: "queue.Queue[Task]" = queue.Queue()
_thread: threading.Thread | None = None
_current: dict[str, Any] | None = None
_pending: set[tuple[int, str]] = set()
_lock = threading.Lock()


def _worker() -> None:
    global _current
    while True:
        pk, url, kind, force = _queue.get()
        with _lock:
            _current = {"pk": pk, "url": url, "kind": kind}
        try:
            if kind == "analyze":
                analyze.run(pk, force=force)
            elif kind == "subs":
                pipeline.refetch_subs(pk, url)
            elif kind == "ingest-video":
                pipeline.run(pk, url, skip_subs=True)
            elif kind == "ingest-subs":
                pipeline.run(pk, url, skip_media=True)
            else:
                pipeline.run(pk, url)
        except Exception as exc:  # noqa: BLE001 - 单条失败不影响队列
            message = str(exc) or exc.__class__.__name__
            if kind == "analyze":
                db.finish_analysis(pk, "failed", message)
            else:
                db.set_status(pk, "failed", "", message)
            traceback.print_exc()
        finally:
            with _lock:
                _pending.discard((pk, kind))
                _current = None
            _queue.task_done()


def start() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    db.init_db()
    recovered = db.recover_interrupted()
    if recovered:
        print(f"[jobs] {recovered} 个中断任务已标记为失败")
    interrupted = db.recover_interrupted_analysis()
    if interrupted:
        print(f"[jobs] {interrupted} 个中断的解析已标记为失败")
    _thread = threading.Thread(target=_worker, name="vel-worker", daemon=True)
    _thread.start()


def _submit(pk: int, url: str, kind: str, force: bool = False) -> int:
    start()
    with _lock:
        key = (pk, kind)
        already = key in _pending or (
            _current is not None and _current["pk"] == pk and _current["kind"] == kind
        )
        if not already:
            _pending.add(key)
            _queue.put((pk, url, kind, force))
    return _queue.qsize()


def enqueue(pk: int, url: str, kind: str = "ingest") -> int:
    return _submit(pk, url, kind)


def enqueue_subs(pk: int, url: str) -> int:
    """只补字幕：视频文件已在本地，省掉一次下载（也绕开已经下过的视频）。"""
    return _submit(pk, url, "subs")


def enqueue_analyze(pk: int, url: str = "", force: bool = False) -> int:
    return _submit(pk, url, "analyze", force)


def is_running(pk: int, kind: str) -> bool:
    with _lock:
        return (pk, kind) in _pending


def status() -> dict[str, Any]:
    with _lock:
        current = dict(_current) if _current else None
    return {"queued": _queue.qsize(), "current": current}
