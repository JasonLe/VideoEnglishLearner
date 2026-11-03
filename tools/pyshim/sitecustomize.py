"""pip 安装临时绕过 shim（仅本机安装依赖时用）。

本机沙箱下 `os.mkdir(path, 0o700)` 会生成一个「创建者自己都写不进去」的目录，
而 CPython 的 `tempfile.mkdtemp()` 恰好用 0o700 建目录 —— 于是 pip 解包 wheel 时
一律 `PermissionError: [Errno 13]`。

这里把 `tempfile.mkdtemp` 换成用 0o777 建目录的等价实现。
用法：PYTHONPATH=<本目录> python -m pip install ...
"""

from __future__ import annotations

import os
import tempfile


def _mkdtemp(suffix=None, prefix=None, dir=None):
    prefix, suffix, dir, _output_type = tempfile._sanitize_params(
        prefix, suffix, dir
    )
    names = tempfile._get_candidate_names()
    for _ in range(tempfile.TMP_MAX):
        name = next(names)
        path = os.path.join(dir, prefix + name + suffix)
        try:
            os.mkdir(path, 0o777)
        except FileExistsError:
            continue
        except PermissionError:
            continue
        return path
    raise FileExistsError("no usable temporary directory name found")


try:
    tempfile.mkdtemp = _mkdtemp  # type: ignore[assignment]
except Exception:  # pragma: no cover
    pass
