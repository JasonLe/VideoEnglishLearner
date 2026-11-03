"""本地假模型：假装成一个 OpenAI 兼容接口，用来离线验证解析链路。

    python tools/stub_llm.py [port]      # 默认 8899

然后设置页里填 base_url = http://127.0.0.1:8899/v1、api_key 随便非空、model = stub，
点「检测」应该看到「连接正常」。它只回固定格式的 JSON，用来验证：
请求发得出去、回包能解析、结果能落库、进度能推进、缓存能命中。
"""

from __future__ import annotations

import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LINE_RE = re.compile(r"^(\d+)\.\s?(.*)$")


def fake_items(prompt: str) -> list[dict]:
    items = []
    for line in prompt.splitlines():
        matched = LINE_RE.match(line.strip())
        if not matched:
            continue
        seq = int(matched.group(1))
        text = matched.group(2)
        items.append(
            {
                "seq": seq,
                "translation": f"【假模型】{text}",
                "grammar": [
                    {
                        "point": "示例语法点",
                        "explain": "这行是 stub 回的，用来验证结构能落库。",
                        "example": "This is a stub example.",
                    }
                ],
                "phrases": [{"term": text.split()[0] if text.split() else "stub", "meaning": "示例短语"}],
                "note": "",
            }
        )
    return items


class Handler(BaseHTTPRequestHandler):
    server_version = "StubLLM/1.0"

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {}
        messages = payload.get("messages") or []
        prompt = messages[-1].get("content", "") if messages else ""
        body = {
            "id": "chatcmpl-stub",
            "object": "chat.completion",
            "model": payload.get("model") or "stub",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": json.dumps({"items": fake_items(prompt)}, ensure_ascii=False),
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args) -> None:  # 安静点
        sys.stderr.write("[stub] " + fmt % args + "\n")


def main(argv: list[str]) -> int:
    # 直接 `python tools/stub_llm.py` 时 sys.path[0] 是 tools/，把项目根塞回去才能 import app
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app import ensure_utf8_stdio

    ensure_utf8_stdio()
    port = int(argv[1]) if len(argv) > 1 else 8899
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"stub LLM 已启动：http://127.0.0.1:{port}/v1")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
