"""HTTP(S) 小工具：统一的 SSL 上下文与（可选）代理 opener。

为什么要单独一层：python.org 装的 macOS Python 默认没有 CA 证书（
`ssl.create_default_context()` 的 x509_ca 数量是 0），urllib 一碰 https 就报
CERTIFICATE_VERIFY_FAILED。系统里其实有 /etc/ssl/cert.pem，退一步用它能正常验证；
Windows/Linux 一般默认就有，不会走到回退分支。这样不必为了联网多装 certifi 依赖。
"""

from __future__ import annotations

import os
import ssl
import sys
import urllib.request

_CA_CANDIDATES = (
    "/etc/ssl/cert.pem",                      # macOS (LibreSSL / 系统根证书)
    "/usr/local/etc/ca-certificates.crt",     # 部分 Homebrew / Linux
    "/opt/homebrew/etc/ca-certificates/cert.pem",  # Apple Silicon Homebrew
    "/etc/pki/tls/certs/ca-bundle.crt",       # Fedora / CentOS
    "/etc/ssl/certs/ca-certificates.crt",     # Debian / Ubuntu
)


def ensure_ca_env() -> str | None:
    """把系统证书路径写进环境变量，供 yt-dlp 等用默认 SSL 上下文的第三方库使用。

    python.org 的 macOS Python 自带 CA 为空，而 yt-dlp 自己有网络层、不走本模块的
    ssl_context()，所以必须在进程启动早期设 SSL_CERT_FILE（以及 requests/curl 的
    等价变量），否则「下载视频」会在取 API 页面时报 CERTIFICATE_VERIFY_FAILED。
    """
    try:
        if ssl.create_default_context().cert_store_stats().get("x509_ca", 0) > 0:
            return None  # 系统/解释器本来就有证书，什么都不用做
    except (ssl.SSLError, OSError):
        pass

    candidates = [os.environ.get("SSL_CERT_FILE"), *_CA_CANDIDATES]
    prefix = getattr(sys, "prefix", "")
    if prefix:
        candidates.append(os.path.join(prefix, "etc", "openssl", "cert.pem"))
    try:
        import certifi  # type: ignore

        candidates.append(certifi.where())
    except Exception:  # noqa: BLE001 - 没装 certifi 就跳过
        pass

    for path in candidates:
        if path and os.path.isfile(path):
            for key in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
                os.environ.setdefault(key, path)
            return path
    return None


def ssl_context() -> ssl.SSLContext:
    """优先用解释器默认 CA；没有就逐个试系统证书文件；再不行试 certifi。"""
    try:
        context = ssl.create_default_context()
        if context.cert_store_stats().get("x509_ca", 0) > 0:
            return context
    except (ssl.SSLError, OSError):
        pass

    candidates = [os.environ.get("SSL_CERT_FILE"), *_CA_CANDIDATES]
    prefix = getattr(sys, "prefix", "")
    if prefix:
        candidates.append(os.path.join(prefix, "etc", "openssl", "cert.pem"))
    for path in candidates:
        if path and os.path.isfile(path):
            try:
                return ssl.create_default_context(cafile=path)
            except (ssl.SSLError, OSError):
                continue

    try:  # 用户自己装了 certifi 就用它
        import certifi  # type: ignore

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 - 没有 certifi 时保持默认（会报证书错误，但至少能起服务）
        return ssl.create_default_context()


def opener(proxy: str = "") -> urllib.request.OpenerDirector:
    """带证书上下文的 opener；proxy 形如 http://127.0.0.1:7890。"""
    handlers: list[urllib.request.BaseHandler] = []
    proxy = (proxy or "").strip()
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    handlers.append(urllib.request.HTTPSHandler(context=ssl_context()))
    return urllib.request.build_opener(*handlers)
