"""配置加载 / 写回：config.toml + 内置默认值，缺项一律回落到默认。

设置页要能改配置，所以这里除了「读」还负责「写」：
- `Config.set()` 只改内存，`save()` 才落盘；
- `render_toml()` 按固定顺序重写文件并带上注释，避免设置页保存后把注释洗掉；
- `reload()` 原地替换 `cfg` 的 `_data`，其它模块手里握着的 `cfg` 引用继续有效
  （download / pipeline / transcribe 都是 import 时拿的引用，换对象会让它们看不到新值）。
"""

from __future__ import annotations

import copy
import json
import os
import tomllib
from pathlib import Path
from typing import Any

from . import APP_NAME, APP_SLUG

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "config.toml"
WEB_DIR = ROOT / "web"
DATA_DIR = ROOT / "data"
MEDIA_DIR = DATA_DIR / "media"
DB_FILE = DATA_DIR / "vel.db"

# 改名前（vlearn）的数据库：启动时直接改名接管，别让人以为数据丢了
LEGACY_DB_NAME = "vlearn.db"

DEFAULTS: dict[str, Any] = {
    "server": {"host": "127.0.0.1", "port": 8787},
    "network": {"proxy": "", "cookies_file": ""},
    "download": {"max_height": 720, "mode": "video+subs", "ffmpeg_location": ""},
    "asr": {
        "backend": "auto",
        "whisper_model": "large-v3",
        "device": "cuda",
        "compute_type": "int8_float16",
        "language": "en",
        "vad_filter": True,
        "beam_size": 5,
    },
    "llm": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        "batch_size": 10,
        "timeout_ms": 90000,
    },
    "segment": {
        "max_chars": 42,
        "target_chars": 32,
        "max_ms": 7000,
        "min_ms": 900,
    },
    "dict": {
        "db_path": "",
        "source_url": "https://raw.githubusercontent.com/skywind3000/ECDICT/master/ecdict.csv",
        "lemma_url": "https://raw.githubusercontent.com/skywind3000/ECDICT/master/lemma.en.txt",
        "max_rank": 60000,
    },
}

_SECTION_ORDER = ("server", "network", "download", "asr", "llm", "segment", "dict")

_KEY_ORDER: dict[str, tuple[str, ...]] = {
    "server": ("host", "port"),
    "network": ("proxy", "cookies_file"),
    "download": ("ffmpeg_location", "max_height", "mode"),
    "asr": (
        "backend",
        "whisper_model",
        "device",
        "compute_type",
        "language",
        "vad_filter",
        "beam_size",
    ),
    "llm": ("base_url", "api_key", "model", "batch_size", "timeout_ms"),
    "segment": ("max_chars", "target_chars", "max_ms", "min_ms"),
    "dict": ("db_path", "source_url", "lemma_url", "max_rank"),
}

_KEY_COMMENT: dict[str, str] = {
    "server.host": "只监听本机；想让局域网其它设备访问改成 0.0.0.0（自己清楚风险就行）",
    "server.port": "改了端口要重启服务",
    "network.proxy": '国内访问 YouTube 必须走代理，例 "http://127.0.0.1:7890"；能直连就留空',
    "network.cookies_file": "遇到「人机校验」时填：浏览器插件导出的 cookies（Netscape 格式）文件路径",
    "download.ffmpeg_location": (
        "ffmpeg.exe 的完整路径，或它所在的目录；留空 = 用 PATH 里的 ffmpeg\n"
        "YouTube 只提供分离的音视频流，没有 ffmpeg 合并不出能播的视频"
    ),
    "download.max_height": "最高分辨率，0 = 不限；学习场景 720p 足够，再高只是白等",
    "download.mode": (
        "video+subs = 下载视频 + 字幕（播放器必需）\n"
        "subs-only  = 只取字幕（省带宽；没装 ffmpeg 时也能先把链路跑通）"
    ),
    "asr.backend": (
        "auto    = 先找 YouTube 官方/自动字幕；视频确实没有字幕时才本地转写\n"
        "youtube = 只用 YouTube 字幕，拿不到就报错\n"
        "whisper = 强制本地转写\n"
        "（被 429 限流不算「没有字幕」：auto 只让你稍后补字幕，不白跑转写）"
    ),
    "asr.whisper_model": '显存吃紧可换 "medium" 或 "distil-large-v3"',
    "asr.device": '没独显就填 "cpu"',
    "asr.compute_type": '6GB 显存用 "int8_float16" 最稳；CPU 用 "int8"',
    "asr.language": "转写语言；英文视频填 en，留空让模型自己判断",
    "asr.vad_filter": "用 VAD 跳过静音段，能显著减少 Whisper 的幻听",
    "asr.beam_size": "解码束宽，越大越准也越慢",
    "llm.base_url": ("任何 OpenAI 兼容接口都行：DeepSeek 填 https://api.deepseek.com/v1；本地 Ollama 填 http://localhost:11434/v1，模型名填你 pull 下来的本地模型"),
    "llm.api_key": "DeepSeek 的 key 形如 sk-…；本地 Ollama 随便填一个非空串即可。只存在这台机器上",
    "llm.model": 'DeepSeek 用 "deepseek-chat"；本地 Ollama 填你 pull 下来的模型名',
    "llm.batch_size": "每次请求喂几行字幕。太大容易超时/漏项，太小请求次数多；10 行是稳的",
    "llm.timeout_ms": "单次请求超时（毫秒）。本地小模型出字慢，可以调到 180000",
    "segment.max_chars": "一行字幕的视觉长度上限（英文≈字符数）",
    "segment.target_chars": "切分的理想行长，比上限小一点看起来更稳",
    "segment.max_ms": "一行最长显示时间，超了强制再切",
    "segment.min_ms": "太短的行会闪，切分时尽量避免",
    "dict.db_path": "词典 SQLite 文件路径；留空 = data/dict/ecdic.db（设置页里点「下载并构建」会生成）",
    "dict.source_url": (
        "ECDICT 的 ecdict.csv 地址（约 63MB，MIT 协议）；GitHub 慢可以换成镜像\n"
        "https://github.com/skywind3000/ECDICT"
    ),
    "dict.lemma_url": "ECDICT 的 lemma.en.txt 地址（约 2.2MB，词形→原形补充表）",
    "dict.max_rank": (
        "词频裁剪档位：只保留 BNC/当代语料排名 <= 该值的词条（外加考试大纲词）。\n"
        "60000 约 8–15 万词条；调大库更全但体积、内存都涨"
    ),
}

_FILE_HEADER = (
    f"# {APP_SLUG}（{APP_NAME}）配置。可以直接改这个文件，也可以在网页右上角「设置」里改（保存时会写回这里）。\n"
    "# 改完重启服务生效；设置页里改的 proxy / ffmpeg 路径等是对后续任务立即生效的。"
)


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(str(value), ensure_ascii=False)


def render_toml(data: dict) -> str:
    """把配置渲染成带注释的 toml 文本。未知的键/段原样保留，别把用户手写的东西弄丢。"""
    lines: list[str] = [_FILE_HEADER, ""]

    sections = [s for s in _SECTION_ORDER if isinstance(data.get(s), dict)]
    sections += [
        s for s, v in data.items() if s not in _SECTION_ORDER and isinstance(v, dict)
    ]

    for section in sections:
        body = data[section]
        lines.append(f"[{section}]")
        keys = [k for k in _KEY_ORDER.get(section, ()) if k in body]
        keys += [k for k in body if k not in keys]
        for key in keys:
            value = body[key]
            if isinstance(value, dict):
                continue
            comment = _KEY_COMMENT.get(f"{section}.{key}")
            if comment:
                for line in comment.splitlines():
                    lines.append(f"# {line}")
            lines.append(f"{key} = {_toml_value(value)}")
        lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n"


class Config:
    def __init__(self, data: dict | None = None) -> None:
        self._data = _merge(DEFAULTS, data or {})

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self._data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        cur: dict = self._data
        for part in parts[:-1]:
            nxt = cur.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cur[part] = nxt
            cur = nxt
        cur[parts[-1]] = value

    @property
    def proxy(self) -> str | None:
        value = str(self.get("network.proxy", "") or "").strip()
        return value or None

    @property
    def cookies_file(self) -> str | None:
        value = str(self.get("network.cookies_file", "") or "").strip()
        return value or None

    def as_dict(self) -> dict:
        return copy.deepcopy(self._data)

    def save(self) -> Path:
        """写回 config.toml。先写临时文件再 replace，避免写一半断电留下残缺配置。"""
        tmp = CONFIG_FILE.with_name(CONFIG_FILE.name + ".tmp")
        tmp.write_text(render_toml(self._data), encoding="utf-8")
        os.replace(tmp, CONFIG_FILE)
        return CONFIG_FILE


def load() -> Config:
    data: dict = {}
    if CONFIG_FILE.exists():
        with CONFIG_FILE.open("rb") as fh:
            data = tomllib.load(fh)
    return Config(data)


def reload() -> Config:
    """重新读盘并原地更新 cfg，保持其它模块手里的引用有效。"""
    fresh = load()
    cfg._data = fresh._data
    return cfg


def defaults() -> dict:
    return copy.deepcopy(DEFAULTS)


def resolve_media_file(raw: str | None) -> Path | None:
    """把库里存的 media_path 解析成真实文件。

    库里的路径是绝对路径，项目目录改名（vlearn → vel）或换机器后会失效，
    所以找不到时退回「按文件名去 MEDIA_DIR 里找」。
    """
    if not raw:
        return None
    path = Path(raw)
    if path.is_file():
        return path
    alt = MEDIA_DIR / path.name
    return alt if alt.is_file() else None


def migrate_legacy_data() -> None:
    """把改名前留下的 vlearn.db 改名为 vel.db。只在目标不存在时动手。"""
    old = DATA_DIR / LEGACY_DB_NAME
    if not old.exists() or DB_FILE.exists():
        return
    try:
        old.rename(DB_FILE)
    except OSError:
        return
    for suffix in ("-wal", "-shm"):
        side = DATA_DIR / f"{LEGACY_DB_NAME}{suffix}"
        if side.exists():
            try:
                side.rename(DATA_DIR / f"{DB_FILE.name}{suffix}")
            except OSError:
                pass


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    migrate_legacy_data()


cfg: Config = load()
