"""yt-dlp 封装：一次调用同时拿视频文件和英文字幕。

设计要点：
- 用 Python API 而不是 subprocess，省掉路径转义和解析 stdout 的麻烦。
- 优先拿 YouTube 自带字幕（官方 or 自动），**因为 json3 带逐词时间戳**，
  几秒就能拿到，比跑 Whisper 快两个数量级。拿不到才转写。
- proxy / cookies 从 config 读，缺代理时给中文可操作的报错。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from functools import lru_cache
from pathlib import Path
from typing import Callable

from .config import MEDIA_DIR, cfg, ensure_dirs
from .models import MediaInfo
from .subtitles import find_subtitle_files

_ANSI_RE = re.compile(re.escape(chr(27)) + r"\[[0-9;]*m")

_ID_PATTERNS = [
    re.compile(r"(?:v=|/v/|/embed/|/shorts/|/live/|youtu\.be/)([A-Za-z0-9_-]{11})"),
    re.compile(r"^([A-Za-z0-9_-]{11})$"),
]

VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".mov")
Progress = Callable[[float, str], None]


def video_id_from_url(url: str) -> str | None:
    url = (url or "").strip()
    for pat in _ID_PATTERNS:
        match = pat.search(url)
        if match:
            return match.group(1)
    return None


def _friendly_error(exc: Exception) -> RuntimeError:
    # yt-dlp 的报错里带终端颜色码，原样抛给用户会变成乱码
    text = _ANSI_RE.sub("", str(exc))
    low = text.lower()
    if "sign in to confirm" in low or "not a bot" in low:
        return RuntimeError(
            "YouTube 要求人机校验。请在 config.toml 里配置 [network] proxy，"
            "或用浏览器导出 cookies 后填 cookies_file。"
        )
    if "failed to resolve" in low or "getaddrinfo" in low or "timed out" in low:
        return RuntimeError(
            "连不上 YouTube。当前网络需要代理：请填 config.toml 的 [network] proxy，"
            "例如 proxy = \"http://127.0.0.1:7890\"。"
        )
    if "429" in text or "too many requests" in low:
        return RuntimeError(
            "YouTube 限流了（HTTP 429）：短时间内对这个 IP 请求太多。"
            "等 2–5 分钟再点一次「重试」；在「设置 → 网络」里填浏览器导出的 cookies 文件，"
            "被限流的概率会低很多。"
        )
    if "private" in low or "members-only" in low or "unavailable" in low:
        return RuntimeError("这个视频不可用（可能是私享、会员专属或已删除）。")
    if "requested format is not available" in low or "ffmpeg is not installed" in low:
        return RuntimeError(
            "这台机器上 ffmpeg 不可用。YouTube 现在只提供分离的音视频流，"
            "必须靠 ffmpeg 合并。装好 ffmpeg 并加进 PATH 后重试"
            "（Windows: winget install Gyan.FFmpeg），"
            "或者先只取字幕（--subs-only）。"
        )
    return RuntimeError(f"下载失败：{text.splitlines()[0] if text else exc!r}")


def _is_subtitle_error(exc: Exception) -> bool:
    """字幕（timedtext）请求失败，跟视频流是两条独立的路。"""
    low = str(exc).lower()
    return "subtitle" in low or "timedtext" in low


def _is_retryable(exc: Exception) -> bool:
    low = str(exc).lower()
    return any(
        hint in low
        for hint in (
            "429",
            "too many requests",
            "timed out",
            "timeout",
            "connection reset",
            "temporary failure",
            "503",
            "504",
        )
    )


def _drop_subtitles(opts: dict) -> dict:
    """把一次调用的字幕请求去掉，只留媒体——字幕失败时至少还能拿到视频。"""
    out = dict(opts)
    out["writesubtitles"] = False
    out["writeautomaticsub"] = False
    out.pop("subtitlesformat", None)
    return out


def _extract(url: str, opts: dict):
    from yt_dlp import YoutubeDL

    with YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=True)


def _base_opts() -> dict:
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 30,
        "ignoreerrors": False,
    }
    if cfg.proxy:
        opts["proxy"] = cfg.proxy
    if cfg.cookies_file:
        opts["cookiefile"] = cfg.cookies_file
    # 设置页里填了路径就显式告诉 yt-dlp，免得它只认 PATH
    exe = ffmpeg_exe()
    if exe:
        opts["ffmpeg_location"] = exe
    return opts


def fetch_info(url: str) -> MediaInfo:
    """只抽元信息，不下载。失败时抛中文 RuntimeError。"""
    try:
        from yt_dlp import YoutubeDL
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("没装 yt-dlp：pip install -r requirements.txt") from exc

    try:
        with YoutubeDL(_base_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001
        raise _friendly_error(exc) from exc

    if info is None:
        raise RuntimeError("yt-dlp 没拿到视频信息。")

    entries = info.get("entries")
    if entries:
        info = next((e for e in entries if e), None) or info

    duration = int(info.get("duration") or 0)
    return MediaInfo(
        video_id=str(info.get("id") or video_id_from_url(url) or ""),
        title=str(info.get("title") or ""),
        channel=str(info.get("uploader") or info.get("channel") or ""),
        duration_ms=duration * 1000,
        thumb=str(info.get("thumbnail") or ""),
        webpage_url=str(info.get("webpage_url") or url),
    )


def find_ffmpeg_in(directory: Path) -> str | None:
    for name in ("ffmpeg.exe", "ffmpeg"):
        candidate = directory / name
        if candidate.is_file():
            return str(candidate)
    return None


def ffmpeg_exe_from(location: str | None) -> str | None:
    """把配置里的 ffmpeg 路径解析成可执行的 exe。

    允许填三种东西：留空（用 PATH）、exe 的完整路径、exe 所在的目录。
    按目录填是最常见的写法（比如 C:\\ffmpeg\\bin），所以两个都认。
    """
    loc = (location or "").strip()
    if not loc:
        return shutil.which("ffmpeg")
    path = Path(loc).expanduser()
    if path.is_dir():
        return find_ffmpeg_in(path)
    if path.is_file():
        return str(path)
    return None


def ffmpeg_exe() -> str | None:
    return ffmpeg_exe_from(str(cfg.get("download.ffmpeg_location", "") or ""))


@lru_cache(maxsize=8)
def probe_ffmpeg_exe(exe: str) -> dict:
    """跑一下 -version，确认 exe 真的能启动。

    不能只看 shutil.which：有的受限环境能找到文件但 spawn 就失败，
    yt-dlp 那边只会笼统地报 'ffmpeg is not installed'，很难查。
    结果按 exe 路径缓存；配置改了或想重测时调 clear_ffmpeg_cache()。
    """
    try:
        proc = subprocess.run(
            [exe, "-version"], capture_output=True, text=True, timeout=15
        )
    except PermissionError:
        return {
            "ok": False,
            "exe": exe,
            "version": "",
            "message": "找到了 ffmpeg，但系统不让它启动（权限被拦）。在自己的终端里跑服务试试。",
        }
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "exe": exe, "version": "", "message": f"ffmpeg 启动失败：{exc}"}

    if proc.returncode != 0:
        detail = ((proc.stderr or proc.stdout or "").strip().splitlines() or [""])[0]
        return {
            "ok": False,
            "exe": exe,
            "version": "",
            "message": f"ffmpeg 返回错误码 {proc.returncode}：{detail}",
        }

    first = ((proc.stdout or "").strip().splitlines() or [""])[0].strip()
    return {"ok": True, "exe": exe, "version": first, "message": "可用"}


def ffmpeg_status() -> dict:
    """ffmpeg 的完整现状：找到没有 / 能不能跑 / 版本。设置页和 health 都用它。"""
    exe = ffmpeg_exe()
    if not exe:
        configured = str(cfg.get("download.ffmpeg_location", "") or "").strip()
        return {
            "ok": False,
            "exe": "",
            "version": "",
            "message": (
                f"配置的 ffmpeg 路径下没找到 ffmpeg：{configured}"
                if configured
                else "PATH 里没有 ffmpeg，可以在「设置」里填 ffmpeg.exe 的完整路径"
            ),
        }
    return probe_ffmpeg_exe(exe)


def clear_ffmpeg_cache() -> None:
    probe_ffmpeg_exe.cache_clear()


def ffmpeg_available() -> bool:
    return bool(ffmpeg_status().get("ok"))


def download(
    url: str,
    stem: str,
    on_progress: Progress | None = None,
    *,
    skip_media: bool = False,
    skip_subs: bool = False,
) -> tuple[Path | None, list[Path], dict]:
    """下载视频 + 英文字幕。返回 (视频文件, 字幕文件列表, 来源信息)。

    stem 用 video_pk，保证同一视频反复处理时文件名稳定、可断点复用。
    skip_media=True 时只要字幕（省带宽，也绕开 ffmpeg 合并）；
    skip_subs=True 时只要视频文件，不碰字幕接口。
    """
    try:
        from yt_dlp import YoutubeDL
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("没装 yt-dlp：pip install -r requirements.txt") from exc

    ensure_dirs()
    max_height = int(cfg.get("download.max_height", 720))
    have_ffmpeg = ffmpeg_available()

    def hook(d: dict) -> None:
        if on_progress is None:
            return
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            got = d.get("downloaded_bytes") or 0
            frac = (got / total) if total else 0.0
            on_progress(min(frac, 0.95), "下载中")
        elif d.get("status") == "finished":
            on_progress(0.97, "下载完成，合并中")

    opts = _base_opts()
    if have_ffmpeg:
        # 优先「视频流 + 音轨」分下再合并，画质最好
        fmt = (
            f"bv*[height<={max_height}][ext=mp4]+ba[ext=m4a]/"
            f"b[height<={max_height}]/bv*+ba/b"
        )
    else:
        # 没有可用 ffmpeg 只能拿已合并的单文件（YouTube 多数新视频已没有）
        fmt = (
            f"b[height<={max_height}][ext=mp4]/"
            f"b[height<={max_height}]/b[ext=mp4]/b"
        )
    opts.update(
        {
            "outtmpl": {"default": str(MEDIA_DIR / f"{stem}.%(ext)s")},
            "writesubtitles": True,
            "writeautomaticsub": True,
            # 每多一个语言就多一次 timedtext 请求，429 就是这么来的，收窄到两个
            "subtitleslangs": ["en", "en-orig"],
            "subtitlesformat": "json3/vtt/best",
            # 字幕之间歇一下，降低被限流的概率
            "sleep_interval_subtitles": 1,
            "progress_hooks": [hook],
        }
    )
    if skip_subs:
        # 只要视频文件：干脆不请求字幕，省一次可能被 429 的往返
        opts = _drop_subtitles(opts)
    if skip_media:
        # 只要字幕：不选格式、不下载媒体，省得被「格式不可用」卡住
        opts["skip_download"] = True
    else:
        opts["format"] = fmt
        if have_ffmpeg:
            opts["merge_output_format"] = "mp4"
            opts["postprocessor_args"] = {"ffmpeg": ["-movflags", "+faststart"]}

    # 字幕接口（timedtext）经常单独被 429 限流。分两种处理：
    # 网络类错误退避重试；字幕类错误则丢掉字幕请求、先把视频拿到手，
    # 让 pipeline 走 Whisper 兜底，而不是整条链路失败。
    subs_error = ""
    info = None
    last_exc: Exception | None = None
    attempts = 3 if not skip_media else 2
    for attempt in range(attempts):
        try:
            info = _extract(url, opts)
            last_exc = None
            break
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if _is_subtitle_error(exc):
                subs_error = str(exc)
                if skip_media:
                    break  # 只取字幕模式下没有退路
                opts = _drop_subtitles(opts)
                continue  # 立刻重来一次，这次只要媒体
            if _is_retryable(exc) and attempt < attempts - 1:
                time.sleep(2 + 4 * attempt)
                continue
            break
    if info is None:
        raise _friendly_error(last_exc or RuntimeError("下载失败")) from last_exc
    if on_progress:
        on_progress(1.0, "视频就绪（字幕被限流，稍后用语音识别）" if subs_error else "下载结束")

    media = None if skip_media else _pick_media(stem)
    if not skip_media and media is None:
        raise RuntimeError(
            "下载完成但没找到视频文件。YouTube 大多数视频只提供分离的音视频流，"
            "必须有可用的 ffmpeg 才能合并；当前 ffmpeg 不可用。"
        )

    subs = find_subtitle_files(MEDIA_DIR, stem)
    info = info or {}
    meta = {
        "manual_langs": sorted((info.get("subtitles") or {}).keys()),
        "auto_langs": sorted((info.get("automatic_captions") or {}).keys()),
        "source": "",
        "subs_error": subs_error,
    }
    if subs:
        manual_en = any(lang.lower().startswith("en") for lang in meta["manual_langs"])
        meta["source"] = "youtube-manual" if manual_en else "youtube-auto"
    return media, subs, meta


def _pick_media(stem: str) -> Path | None:
    found = [p for p in MEDIA_DIR.glob(f"{stem}.*") if p.suffix.lower() in VIDEO_EXTS]
    if not found:
        return None
    order = {".mp4": 0, ".mkv": 1, ".webm": 2, ".mov": 3}
    found.sort(key=lambda p: (order.get(p.suffix.lower(), 9), p.name))
    return found[0]
