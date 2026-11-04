"""设置页的数据契约：字段描述 + 校验 + 落盘。

字段描述只在这里定义一份，前端按它渲染表单。加一个可配置项 = 在 FIELDS 里加一条，
再在 config.DEFAULTS 里给它一个默认值，前端和「恢复默认」自动跟着更新。

为什么不在前端写死表单：配置项的合法值（枚举、范围、分组）属于后端语义，
写两份迟早对不上，而且前端是纯静态文件、没有构建步骤，改一处漏一处很难发现。
"""

from __future__ import annotations

import copy
from typing import Any

from . import config as config_mod
from .config import cfg

MAX_TEXT_LEN = 500


class SettingsError(ValueError):
    """配置值不合法。API 层转成 400 + 中文文案。"""


FIELDS: list[dict[str, Any]] = [
    {
        "group": "网络",
        "path": "network.proxy",
        "label": "代理地址",
        "type": "text",
        "placeholder": "http://127.0.0.1:7890",
        "hint": "国内访问 YouTube 必须走代理。能直连就留空。改完对之后添加的视频立即生效。",
    },
    {
        "group": "网络",
        "path": "network.cookies_file",
        "label": "cookies 文件",
        "type": "path",
        "placeholder": "D:\\cookies\\youtube.txt",
        "hint": "遇到「Sign in to confirm you're not a bot」时，用浏览器插件导出 Netscape 格式的 cookies，填文件路径。",
    },
    {
        "group": "下载",
        "path": "download.ffmpeg_location",
        "label": "ffmpeg 路径",
        "type": "exe",
        "action": "probe",
        "placeholder": "留空 = 用 PATH 里的 ffmpeg",
        "hint": "填 ffmpeg.exe 的完整路径，或者它所在的目录。YouTube 现在只提供分离的音视频流，没有 ffmpeg 就合并不出可播放的视频。",
    },
    {
        "group": "下载",
        "path": "download.max_height",
        "label": "最高分辨率",
        "type": "int",
        "min": 0,
        "max": 4320,
        "hint": "0 = 不限。学习场景 720p 够用，再高只是白等。",
    },
    {
        "group": "下载",
        "path": "download.mode",
        "label": "下载模式",
        "type": "select",
        "options": [
            ["video+subs", "视频 + 字幕（播放器必需）"],
            ["subs-only", "只取字幕（不需要 ffmpeg）"],
        ],
        "hint": "只取字幕适合先把「下载 → 字幕 → 切分」这条链路跑通。",
    },
    {
        "group": "转写",
        "path": "asr.backend",
        "label": "字幕来源",
        "type": "select",
        "options": [
            ["auto", "优先 YouTube 字幕，没有才转写"],
            ["youtube", "只用 YouTube 字幕"],
            ["whisper", "总是本地 Whisper 转写"],
        ],
        "hint": "YouTube 自带字幕带逐词时间戳，比跑 Whisper 快两个数量级。被 429 限流时程序只会让你稍后补字幕，不会改跑转写。",
    },
    {
        "group": "转写",
        "path": "asr.whisper_model",
        "label": "Whisper 模型",
        "type": "text",
        "placeholder": "large-v3",
        "hint": "显存吃紧可换 medium 或 distil-large-v3。需要先装 faster-whisper（requirements-asr.txt）。",
    },
    {
        "group": "转写",
        "path": "asr.device",
        "label": "推理设备",
        "type": "select",
        "options": [
            ["cuda", "cuda（NVIDIA 显卡）"],
            ["cpu", "cpu"],
        ],
        "hint": "CUDA 起不来时程序会自动退到 CPU。",
    },
    {
        "group": "转写",
        "path": "asr.compute_type",
        "label": "推理精度",
        "type": "select",
        "options": [
            ["int8_float16", "int8_float16（6GB 显存推荐）"],
            ["float16", "float16"],
            ["int8", "int8（CPU 用）"],
            ["float32", "float32（最准最慢）"],
        ],
        "hint": "显存不够会 OOM：先降这个，再降模型。",
    },
    {
        "group": "转写",
        "path": "asr.language",
        "label": "语言",
        "type": "text",
        "placeholder": "en",
        "hint": "英文视频填 en；留空让模型自己判断。",
    },
    {
        "group": "解析",
        "path": "llm.base_url",
        "label": "模型地址",
        "type": "text",
        "action": "probe-llm",
        "placeholder": "https://api.deepseek.com/v1",
        "hint": "任何 OpenAI 兼容接口都行：DeepSeek 填 https://api.deepseek.com/v1；本地 Ollama 填 http://localhost:11434/v1。填完点「检测」。",
    },
    {
        "group": "解析",
        "path": "llm.api_key",
        "label": "API Key",
        "type": "text",
        "placeholder": "sk-…",
        "hint": "DeepSeek 控制台拿的 key，只存在这台机器上。本地 Ollama 随便填非空串（或留空，程序会自动放行本地地址）。也可以不填，改用环境变量 DEEPSEEK_API_KEY。",
    },
    {
        "group": "解析",
        "path": "llm.model",
        "label": "模型名",
        "type": "text",
        "placeholder": "deepseek-chat",
        "hint": "DeepSeek 用 deepseek-chat；本地 Ollama 填你 pull 下来的模型名，如 qwen2.5:14b-instruct。",
    },
    {
        "group": "解析",
        "path": "llm.batch_size",
        "label": "每次行数",
        "type": "int",
        "min": 1,
        "max": 30,
        "hint": "一次请求喂几行字幕。10 行比较稳；模型容易漏行就调小，想省钱调大。",
    },
    {
        "group": "解析",
        "path": "llm.timeout_ms",
        "label": "请求超时",
        "type": "int",
        "min": 5000,
        "max": 50000,
        "hint": "单次请求超时（毫秒）。本地小模型出字慢，可调到 180000。",
    },
    {
        "group": "切分",
        "path": "segment.max_chars",
        "label": "行长上限",
        "type": "int",
        "min": 16,
        "max": 120,
        "hint": "一行字幕的视觉长度上限（英文≈字符数）。42 约等于手机上的两行。",
    },
    {
        "group": "切分",
        "path": "segment.target_chars",
        "label": "理想行长",
        "type": "int",
        "min": 8,
        "max": 120,
        "hint": "DP 切分偏好的行长，比上限小一点，刷新时更不容易换行。",
    },
    {
        "group": "切分",
        "path": "segment.max_ms",
        "label": "最长显示",
        "type": "int",
        "min": 1000,
        "max": 20000,
        "hint": "一行最长显示多少毫秒，超了强制再切。",
    },
    {
        "group": "切分",
        "path": "segment.min_ms",
        "label": "最短显示",
        "type": "int",
        "min": 200,
        "max": 10000,
        "hint": "太短的行会闪，切分时尽量避免。",
    },
    {
        "group": "词典",
        "path": "dict.db_path",
        "label": "词典文件",
        "type": "path",
        "placeholder": "留空 = data/dict/ecdic.db",
        "hint": "本地词典的 SQLite 路径。不填就用默认位置；上面状态卡里的「下载并构建」会生成它。",
    },
    {
        "group": "词典",
        "path": "dict.source_url",
        "label": "词库源地址",
        "type": "text",
        "hint": "ECDICT 的 ecdict.csv（约 63MB，MIT 协议）。GitHub 慢可以换镜像；改完点「下载并构建」。",
    },
    {
        "group": "词典",
        "path": "dict.max_rank",
        "label": "词频裁剪档位",
        "type": "int",
        "min": 5000,
        "max": 60000,
        "hint": "只保留 BNC/当代语料排名不超过该值的词条（外加考试大纲词）。越大词条越多、库越大；60000 档实测约 5.9 万条 / 14MB，再往上基本饱和（BNC 排名上限 50000）。改完需要重建词典。",
    },
    {
        "group": "服务",
        "path": "server.host",
        "label": "监听地址",
        "type": "text",
        "restart": True,
        "hint": "只本机用就 127.0.0.1。改 0.0.0.0 要注意局域网可见。",
    },
    {
        "group": "服务",
        "path": "server.port",
        "label": "端口",
        "type": "int",
        "min": 1,
        "max": 65535,
        "restart": True,
        "hint": "改完需要重启服务。",
    },
]

_BY_PATH = {field["path"]: field for field in FIELDS}


def _default_of(path: str) -> Any:
    cur: Any = config_mod.DEFAULTS
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return copy.deepcopy(cur)


def _ffmpeg_status() -> dict:
    from .download import ffmpeg_status

    return ffmpeg_status()


def _llm_status() -> dict:
    from . import llm

    return llm.describe()


def _dict_status() -> dict:
    from . import dict as dictionary

    return dictionary.status()


def snapshot() -> dict:
    """设置页需要的全部内容：字段描述 + 当前值 + 默认值 + ffmpeg 现状。"""
    return {
        "config_file": str(config_mod.CONFIG_FILE),
        "fields": copy.deepcopy(FIELDS),
        "values": {field["path"]: cfg.get(field["path"]) for field in FIELDS},
        "defaults": {field["path"]: _default_of(field["path"]) for field in FIELDS},
        "ffmpeg": _ffmpeg_status(),
        "llm": _llm_status(),
        "dict": _dict_status(),
    }


def _coerce(field: dict, raw: Any) -> Any:
    path = field["path"]
    label = field.get("label") or path
    kind = field["type"]

    if kind in ("text", "path", "exe"):
        text = "" if raw is None else str(raw).strip()
        if len(text) > MAX_TEXT_LEN:
            raise SettingsError(f"{label} 太长了（超过 {MAX_TEXT_LEN} 字符）")
        return text

    if kind == "int":
        value = str(raw).strip() if raw is not None else ""
        if value == "":
            raise SettingsError(f"{label} 不能为空")
        try:
            number = int(float(value))
        except (TypeError, ValueError):
            raise SettingsError(f"{label} 必须是整数") from None
        low, high = field.get("min"), field.get("max")
        if low is not None and number < low:
            raise SettingsError(f"{label} 不能小于 {low}")
        if high is not None and number > high:
            raise SettingsError(f"{label} 不能大于 {high}")
        return number

    if kind == "select":
        allowed = [value for value, _ in field["options"]]
        text = "" if raw is None else str(raw).strip()
        if text not in allowed:
            raise SettingsError(f"{label} 只能是：{' / '.join(allowed)}")
        return text

    raise SettingsError(f"{label} 的类型 {kind} 不支持")


def _cross_check(staged: dict[str, Any]) -> None:
    """跨字段的约束放这里：单字段校验管不到「目标行长比上限还大」这种。"""

    def value(path: str) -> Any:
        return staged.get(path, cfg.get(path))

    max_chars = value("segment.max_chars")
    target_chars = value("segment.target_chars")
    if target_chars > max_chars:
        raise SettingsError(
            f"切分：理想行长（{target_chars}）不能大于行长上限（{max_chars}）"
        )

    min_ms = value("segment.min_ms")
    max_ms = value("segment.max_ms")
    if min_ms >= max_ms:
        raise SettingsError(
            f"切分：最短显示（{min_ms}ms）必须小于最长显示（{max_ms}ms）"
        )

    if not str(value("server.host") or "").strip():
        raise SettingsError("监听地址不能为空")


def apply(values: dict) -> dict:
    """校验并写回磁盘，然后返回新的 snapshot。

    只处理 FIELDS 里声明过的键：前端多传一个字段就当没看见，
    免得因为版本不一致直接 400 把用户挡在设置页外面。
    """
    if not isinstance(values, dict):
        raise SettingsError("请求体格式不对：需要 {\"values\": {...}}")

    staged: dict[str, Any] = {}
    for path, raw in values.items():
        field = _BY_PATH.get(path)
        if field is None:
            continue
        staged[path] = _coerce(field, raw)

    _cross_check(staged)

    for path, value in staged.items():
        cfg.set(path, value)
    try:
        cfg.save()
    except OSError as exc:
        raise SettingsError(f"写入 config.toml 失败：{exc}") from exc

    # 写完再读一遍，保证内存 = 磁盘（也顺便验证一下刚写的 toml 还能被解析）
    config_mod.reload()
    _invalidate_ffmpeg()
    from . import dict as dictionary

    dictionary.invalidate()
    return snapshot()


def check_ffmpeg(path: str) -> dict:
    """「检测」按钮：只探测，不落盘。可传空串表示用 PATH 里的。"""
    from .download import ffmpeg_exe_from, probe_ffmpeg_exe

    exe = ffmpeg_exe_from(path)
    if not exe:
        where = (path or "").strip()
        return {
            "ok": False,
            "exe": "",
            "version": "",
            "message": (
                f"这个路径下没找到 ffmpeg：{where}" if where else "PATH 里没有 ffmpeg"
            ),
        }
    return probe_ffmpeg_exe(exe)


def _invalidate_ffmpeg() -> None:
    from .download import clear_ffmpeg_cache

    clear_ffmpeg_cache()
