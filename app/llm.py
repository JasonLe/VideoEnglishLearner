"""OpenAI 兼容的 Chat Completions 客户端（只用标准库 urllib，不引 openai 包）。

只要服务端提供 `/chat/completions` 这一套，DeepSeek、OpenAI、Ollama、vLLM、LM Studio
都能用同一份代码 —— 差别只在 base_url / api_key / model 三个值。

为什么不用官方 SDK：只有一个调用点（对话 + 要 JSON），多一个依赖就多一份在
Windows + 旧 Python 上装不动的风险。HTTP 就够。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from .config import cfg

# 本地服务（Ollama / LM Studio）通常不校验 key；远端服务必须有。
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal")
_ENV_KEYS = ("VLEARN_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY")


class LlmError(RuntimeError):
    """调用大模型失败。消息里带原因，上层直接展示给用户。"""


def _text(value: Any) -> str:
    return str(value or "").strip()


def base_url() -> str:
    return _text(cfg.get("llm.base_url", ""))


def model() -> str:
    return _text(cfg.get("llm.model", "")) or "deepseek-chat"


def api_key() -> str:
    """配置里的 key 优先，其次环境变量（不把 key 写进 config.toml 的人用这个）。"""
    import os

    value = _text(cfg.get("llm.api_key", ""))
    if value:
        return value
    for name in _ENV_KEYS:
        env = _text(os.environ.get(name, ""))
        if env:
            return env
    return ""


def is_local(url: str | None = None) -> bool:
    target = (url if url is not None else base_url()).lower()
    return any(host in target for host in _LOCAL_HOSTS)


def configured() -> bool:
    """能不能解析：有 base_url，且（有 key 或是本地服务）。"""
    url = base_url()
    return bool(url) and (bool(api_key()) or is_local(url))


def describe() -> dict[str, Any]:
    """给 /api/health 和设置页看的现状，不含 key 本身。"""
    url = base_url()
    key = api_key()
    masked = ""
    if key:
        masked = f"{key[:4]}…{key[-4:]}" if len(key) > 10 else "已设置"
    return {
        "configured": configured(),
        "base_url": url,
        "model": model(),
        "api_key_set": bool(key),
        "api_key_masked": masked,
        "is_local": is_local(url),
    }


def _endpoint(url: str) -> str:
    base = url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def _opener() -> urllib.request.OpenerDirector:
    """走配置里的代理（国内访问 OpenAI 之类才需要；DeepSeek 和本地模型不需要）。"""
    proxy = cfg.proxy
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        return urllib.request.build_opener(handler)
    return urllib.request.build_opener()


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _friendly_http_error(code: int, body: str) -> LlmError:
    snippet = body.strip().replace("\n", " ")[:300]
    hints = {
        401: "api_key 不对或过期（401）",
        402: "账户余额不足（402）",
        403: "没有权限（403）",
        404: "base_url 或模型名不对（404）。DeepSeek 是 https://api.deepseek.com/v1，"
        "本地 Ollama 是 http://localhost:11434/v1",
        429: "请求太频繁或额度用完了（429）",
    }
    hint = hints.get(code, f"模型服务返回 HTTP {code}")
    return LlmError(f"{hint}：{snippet}" if snippet else hint)


def chat(
    messages: list[dict[str, str]],
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    temperature: float = 0.2,
    json_mode: bool = True,
    timeout_ms: int | None = None,
    max_tokens: int | None = None,
) -> str:
    """发一次对话请求，返回 message.content 文本。"""
    url = _text(base_url) if base_url is not None else globals()["base_url"]()
    if not url:
        raise LlmError("还没配大模型：设置页「解析」分组里填 base_url（DeepSeek 用 https://api.deepseek.com/v1）")

    key = _text(api_key) if api_key is not None else globals()["api_key"]()
    name = _text(model) if model is not None else globals()["model"]()
    if not name:
        raise LlmError("还没填模型名（DeepSeek 用 deepseek-chat）")
    if not key and not is_local(url):
        raise LlmError(
            "还没配 API key：设置页「解析」分组里填 llm.api_key，"
            "或者设环境变量 DEEPSEEK_API_KEY。"
        )

    payload: dict[str, Any] = {
        "model": name,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if max_tokens:
        payload["max_tokens"] = int(max_tokens)

    raw_timeout = timeout_ms if timeout_ms is not None else cfg.get("llm.timeout_ms", 90000)
    timeout = max(5.0, float(raw_timeout or 90000) / 1000.0)

    request = urllib.request.Request(
        _endpoint(url),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            **({"Authorization": f"Bearer {key}"} if key else {}),
        },
        method="POST",
    )

    try:
        with _opener().open(request, timeout=timeout) as resp:
            body = _decode(resp.read())
    except urllib.error.HTTPError as exc:  # 4xx/5xx 也算有响应，读 body 找原因
        raise _friendly_http_error(exc.code, _decode(exc.read() or b"")) from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise LlmError(
            f"连不上模型服务（{url}）：{reason}。本地模型没启动？或需要代理？"
        ) from exc
    except TimeoutError as exc:
        raise LlmError(f"模型服务 {timeout:.0f} 秒没响应，可以把 llm.timeout_ms 调大") from exc

    try:
        data = json.loads(body)
    except ValueError as exc:
        raise LlmError(f"模型服务返回的不是 JSON：{body[:200]}") from exc

    choices = data.get("choices") or []
    if not choices:
        error = (data.get("error") or {}).get("message") if isinstance(data.get("error"), dict) else ""
        raise LlmError(f"模型没有返回内容：{error or body[:200]}")
    content = (choices[0].get("message") or {}).get("content") or ""
    if not content.strip():
        raise LlmError("模型返回了空内容（可能是 max_tokens 太小或内容被过滤）")
    return content


def _strip_fence(text: str) -> str:
    """模型经常把 JSON 包在 ```json 里，去掉围栏再解析。"""
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else body
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
    return body.strip()


def json_chat(messages: list[dict[str, str]], **kwargs: Any) -> dict[str, Any]:
    """要一段 JSON 回来。解析失败把原文带出来，方便定位是模型不听话还是提示词有问题。"""
    text = chat(messages, json_mode=True, **kwargs)
    body = _strip_fence(text)
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise LlmError(f"模型返回的不是合法 JSON：{text[:300]}") from exc
    if not isinstance(data, dict):
        raise LlmError(f"模型返回的 JSON 不是对象：{text[:200]}")
    return data


def probe(
    base_url_override: str = "",
    api_key_override: str = "",
    model_override: str = "",
) -> dict[str, Any]:
    """设置页的「检测」按钮：真发一次极小的请求，验证 url / key / 模型名三者都对得上。"""
    url = _text(base_url_override) or base_url()
    key = _text(api_key_override) or api_key()
    name = _text(model_override) or model()
    if not url:
        return {"ok": False, "message": "base_url 是空的", "model": name, "latency_ms": 0}

    started = time.monotonic()
    try:
        chat(
            [{"role": "user", "content": "回复 ok 两个字"}],
            base_url=url,
            api_key=key,
            model=name,
            json_mode=False,
            max_tokens=8,
            timeout_ms=20000,
        )
    except LlmError as exc:
        return {
            "ok": False,
            "message": str(exc),
            "model": name,
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
    return {
        "ok": True,
        "message": f"能用：{name}",
        "model": name,
        "latency_ms": int((time.monotonic() - started) * 1000),
    }
