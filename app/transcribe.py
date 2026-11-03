"""faster-whisper 转写（兜底路径）。

只在「视频确实没有英文字幕」时才走这里：YouTube 字幕接口被 429 限流不算没有字幕。
large-v3 在 3060 上用 int8_float16 大约吃 3GB 显存；CUDA / cuDNN 跑不起来会
自动退回 CPU int8（慢，但能跑，代码里两条路产出同一种数据结构）。
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Callable

from .config import DATA_DIR, cfg

# 模型缓存放在项目的 data/models 下：不写用户的 ~/.cache（某些环境没权限），
# 整个项目也能直接搬走。国内下模型慢时先设 HF_ENDPOINT=https://hf-mirror.com
os.environ.setdefault("HF_HOME", str(DATA_DIR / "models"))
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")


def _register_cuda_dlls() -> None:
    """把 nvidia-*-cu12 wheel 里的 DLL 目录挂进搜索路径。

    Windows 的 os.add_dll_directory 只在进程内有效，所以必须在 ctranslate2
    加载之前执行（也就是在任何 faster_whisper import 之前）。没装这些 wheel
    就什么都不做，后面会自然退到 CPU。
    """
    if os.name != "nt":
        return
    seen: set[str] = set()
    found: list[str] = []
    for entry in sys.path:
        try:
            root = Path(entry or ".").resolve() / "nvidia"
        except (TypeError, OSError):
            continue
        if not root.is_dir():
            continue
        for bin_dir in sorted(root.glob("*/bin")):
            key = str(bin_dir)
            if key in seen:
                continue
            seen.add(key)
            found.append(key)
            try:
                os.add_dll_directory(key)
            except OSError:
                pass
    if found:
        # ctranslate2 里是普通 LoadLibrary，不走 add_dll_directory 的搜索列表，
        # 所以还得塞进 PATH 才认得 cublas64_12.dll 这些。
        os.environ["PATH"] = os.pathsep.join(found + [os.environ.get("PATH", "")])


_register_cuda_dlls()


from .models import Word  # noqa: E402
from .segment import distribute_words  # noqa: E402

_lock = threading.Lock()
_cache: dict[tuple[str, str, str], object] = {}

# 这些词出现在异常里，基本就是「显卡库没装好 / 显存不够」，换 CPU 还能救
_CUDA_HINTS = ("cublas", "cudnn", "cuda", "nvcuda", "no kernel image", "out of memory")


def _cuda_problem(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(hint in text for hint in _CUDA_HINTS)


def _load_model(
    on_progress: Callable[[float, str], None] | None = None,
    device: str | None = None,
    compute: str | None = None,
):
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "没装 faster-whisper：pip install -r requirements-asr.txt\n"
            '（也可以用 [asr] backend = "youtube" 只吃 YouTube 自带字幕）'
        ) from exc

    name = str(cfg.get("asr.whisper_model", "large-v3"))
    device = device or str(cfg.get("asr.device", "cuda"))
    compute = compute or str(cfg.get("asr.compute_type", "int8_float16"))
    key = (name, device, compute)
    with _lock:
        if key in _cache:
            return _cache[key]
        if on_progress:
            on_progress(0.02, f"准备语音模型 {name}（首次会下载）")
        if device != "cpu":
            try:
                model = WhisperModel(name, device=device, compute_type=compute)
                _cache[key] = model
                return model
            except Exception as exc:  # noqa: BLE001 - 缺 CUDA/cuDNN 时退 CPU
                if on_progress:
                    on_progress(0.02, f"显卡起不来（{str(exc)[:50]}），改用 CPU 转写")
        cpu_key = (name, "cpu", "int8")
        if cpu_key not in _cache:
            _cache[cpu_key] = WhisperModel(name, device="cpu", compute_type="int8")
        return _cache[cpu_key]


def transcribe_file(
    media_path: str | Path,
    on_progress: Callable[[float, str], None] | None = None,
) -> list[Word]:
    language = str(cfg.get("asr.language", "en")) or None
    beam_size = int(cfg.get("asr.beam_size", 5))
    vad_filter = bool(cfg.get("asr.vad_filter", True))

    def consume(model) -> tuple[list[Word], list[tuple[int, int, str]]]:
        """跑一遍模型，收集词级时间戳。GPU 的错可能到这一步才炸（生成器惰性）。"""
        segments, info = model.transcribe(
            str(media_path),
            language=language,
            beam_size=beam_size,
            vad_filter=vad_filter,
            vad_parameters={"min_silence_duration_ms": 500},
            word_timestamps=True,
            condition_on_previous_text=False,  # 关掉抗幻觉复读
        )
        total = float(getattr(info, "duration", 0.0) or 0.0)
        words: list[Word] = []
        seg_texts: list[tuple[int, int, str]] = []
        for seg in segments:
            seg_texts.append((int(seg.start * 1000), int(seg.end * 1000), seg.text or ""))
            if seg.words:
                for w in seg.words:
                    text = (w.word or "").strip()
                    if not text:
                        continue
                    start_ms = int(round(w.start * 1000))
                    end_ms = int(round(w.end * 1000))
                    words.append(
                        Word(text=text, start_ms=start_ms, end_ms=max(end_ms, start_ms + 1))
                    )
            if on_progress and total > 0:
                on_progress(min(seg.end / total, 0.99), "转写中")
        return words, seg_texts

    try:
        words, seg_texts = consume(_load_model(on_progress))
    except RuntimeError as exc:
        # 典型现场：模型加载看不出问题，缺 cublas64_12.dll 要到解码/编码阶段才炸
        if not _cuda_problem(exc):
            raise
        if on_progress:
            on_progress(0.02, "显卡缺 CUDA 运行库，改用 CPU 转写（会慢一些）")
        words, seg_texts = consume(_load_model(on_progress, device="cpu", compute="int8"))

    if on_progress:
        on_progress(1.0, "转写完成")

    # 某些模型/参数组合下 word_timestamps 会整段为空，用段级时间按字符比例摊
    if len(words) < max(3, len(seg_texts) // 2):
        words = []
        for start_ms, end_ms, text in seg_texts:
            words.extend(distribute_words(start_ms, end_ms, text))

    return words
