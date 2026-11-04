#!/usr/bin/env python3
"""构建 ECDICT 裁剪词典 data/dict/ecdic.db。

用法::

    # 首次：下载源文件并构建
    python3 tools/build_dict.py --download

    # 已有源文件，重新构建（换裁剪档位）
    python3 tools/build_dict.py --max-rank 30000

    # 构建后跑示例查询（walked → walk、take 变形、随机抽词、未收录词）
    python3 tools/build_dict.py --check

    # 只对已有库跑自查，不重建
    python3 tools/build_dict.py --check-only

源文件默认放在 data/dict/src/，构建产物默认 data/dict/ecdic.db；
数据库 schema 见 docs/vocab-dict-api.md 第 1 节。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.dictbuild import SCHEMA_VERSION, build  # noqa: E402

CSV_URL = "https://raw.githubusercontent.com/skywind3000/ECDICT/master/ecdict.csv"
LEMMA_URL = "https://raw.githubusercontent.com/skywind3000/ECDICT/master/lemma.en.txt"

DEFAULT_SOURCE = ROOT / "data" / "dict" / "src" / "ecdict.csv"
DEFAULT_LEMMA = ROOT / "data" / "dict" / "src" / "lemma.en.txt"
DEFAULT_OUT = ROOT / "data" / "dict" / "ecdic.db"

_USER_AGENT = "VideoEnglishLearner/1.0 (+https://github.com/skywind3000/ECDICT)"


def human_size(num: int | float) -> str:
    if num >= 1024 * 1024:
        return f"{num / (1024 * 1024):.1f} MB"
    if num >= 1024:
        return f"{num / 1024:.0f} KB"
    return f"{int(num)} B"


def _download_once(url: str, dest: Path) -> Path:
    """下载到 dest（带缓存续传）；成功返回 dest。"""
    part = dest.with_name(dest.name + ".part")
    offset = part.stat().st_size if part.exists() else 0
    headers = {"User-Agent": _USER_AGENT}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    request = urllib.request.Request(url, headers=headers)
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=60) as resp:
        status = getattr(resp, "status", 200)
        if offset and status == 200:
            # 服务端不支持断点续传，从头来
            offset = 0
        total = int(resp.headers.get("Content-Length") or 0) + offset
        mode = "ab" if offset and status == 206 else "wb"
        done = offset
        last_pct = -1
        with open(part, mode) as fh:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if total:
                    pct = min(done * 100 // total, 100)
                    if pct != last_pct and (pct % 2 == 0 or pct == 100):
                        last_pct = pct
                        speed = done / max(time.monotonic() - started, 0.01)
                        print(f"\r  下载 {dest.name}: {pct:3d}%  "
                              f"{human_size(done)}/{human_size(total)}  "
                              f"{human_size(speed)}/s", end="", file=sys.stderr,
                              flush=True)
                else:
                    print(f"\r  下载 {dest.name}: {human_size(done)}",
                          end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)
    os.replace(part, dest)
    return dest


def download(url: str, dest: Path, *, force: bool = False) -> Path:
    """下载源文件；已存在则跳过（force 时重新下载）。失败最多重试 3 次。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0 and not force:
        print(f"[跳过] 已存在 {dest} ({human_size(dest.stat().st_size)})")
        return dest
    print(f"[下载] {url}\n    -> {dest}")
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            return _download_once(url, dest)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last_error = exc
            print(f"[警告] 第 {attempt} 次下载失败: {exc}", file=sys.stderr)
            if attempt < 3:
                time.sleep(2 * attempt)
    raise SystemExit(f"[错误] 下载失败: {last_error}")


def make_progress(prefix: str = "  构建"):
    """构建进度回调：每约 1% 打印一次到 stderr，最多 100 行输出。"""
    state = {"last": -1}

    def _callback(done: int, total: int) -> None:
        if not total:
            print(f"\r{prefix} {done} 行", end="", file=sys.stderr, flush=True)
            return
        pct = min(done * 100 // total, 100)
        if pct != state["last"]:
            state["last"] = pct
            print(f"\r{prefix} {done}/{total} ({pct}%)", end="",
                  file=sys.stderr, flush=True)

    return _callback


def _lookup(conn: sqlite3.Connection, word: str) -> dict | None:
    """模拟 app/dict.py 的查词：先查原词，再经 form 表找原形。"""
    row = conn.execute(
        "SELECT word, phonetic, translation, definition, pos, collins, oxford,"
        " tag, bnc, frq, forms FROM dict WHERE word = ?", (word,)
    ).fetchone()
    source = "direct"
    if row is None:
        found = conn.execute(
            "SELECT d.word, d.phonetic, d.translation, d.definition, d.pos,"
            " d.collins, d.oxford, d.tag, d.bnc, d.frq, d.forms"
            " FROM form f JOIN dict d ON d.word = f.word WHERE f.form = ?",
            (word,),
        ).fetchone()
        row, source = found, ("lemma" if found else "none")
    if row is None:
        return None
    keys = ("word", "phonetic", "translation", "definition", "pos", "collins",
            "oxford", "tag", "bnc", "frq", "forms")
    item = dict(zip(keys, row))
    item["source"] = source
    return item


def _first_line(text: str, limit: int = 64) -> str:
    line = (text or "").split("\n", 1)[0].strip()
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _show_word(label: str, item: dict | None) -> None:
    if item is None:
        print(f"  {label:<12} 未收录")
        return
    print(f"  {label:<12} -> {item['word']}  [{item['source']}]  "
          f"音标 {item['phonetic'] or '-'}  "
          f"bnc={item['bnc']} frq={item['frq']}  "
          f"tag={item['tag'] or '-'}")
    print(f"  {'':<12}    释义: {_first_line(item['translation']) or '-'}")
    if item["forms"]:
        print(f"  {'':<12}    变形: {item['forms']}")


def run_checks(db_path: Path, *, samples: int = 10) -> bool:
    """示例查询自查；返回 True 表示全部通过。"""
    db_path = Path(db_path)
    if not db_path.is_file():
        print(f"[错误] 数据库不存在: {db_path}", file=sys.stderr)
        return False
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    ok = True
    try:
        entries = conn.execute("SELECT COUNT(*) FROM dict").fetchone()[0]
        forms = conn.execute("SELECT COUNT(*) FROM form").fetchone()[0]
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        print("\n== 词典自查 ==")
        print(f"  文件      {db_path} ({human_size(db_path.stat().st_size)})")
        print(f"  entries   {entries}    forms {forms}")
        print(f"  meta      {meta}")
        print(f"  integrity {conn.execute('PRAGMA integrity_check').fetchone()[0]}")

        print("\n  -- 词形还原 / tag / 音标 --")
        _show_word("walked", _lookup(conn, "walked"))
        _show_word("walk", _lookup(conn, "walk"))
        walked = _lookup(conn, "walked")
        if walked is None or walked["word"] != "walk":
            print("  [FAIL] walked 应还原到 walk")
            ok = False

        print("\n  -- take 的变形 --")
        take = _lookup(conn, "take")
        _show_word("take", take)
        if take is None:
            print("  [FAIL] take 未收录")
            ok = False
        else:
            variants = take["forms"].split()
            print(f"  期望包含: took taken taking takes")
            missing = [v for v in ("took", "taken", "taking", "takes")
                       if v not in variants]
            if missing:
                print(f"  [FAIL] take.forms 缺少 {missing}")
                ok = False
            for form in ("took", "taken", "taking", "takes"):
                got = conn.execute(
                    "SELECT word FROM form WHERE form = ?", (form,)
                ).fetchone()
                if not got or got[0] != "take":
                    print(f"  [FAIL] form 表 {form} -> {got}")
                    ok = False

        print("\n  -- 随机抽查 --")
        rows = conn.execute(
            "SELECT word, translation, bnc, frq FROM dict "
            "ORDER BY RANDOM() LIMIT ?", (int(samples),)
        ).fetchall()
        for word, translation, bnc, frq in rows:
            print(f"  {word:<22} bnc={bnc:<6} frq={frq:<6} "
                  f"{_first_line(translation, 40) or '-'}")

        print("\n  -- 未收录词示例（超出 60000 档/生僻词）--")
        probe = ["pneumonoultramicroscopicsilicovolcanoconiosis",
                 "floccinaucinihilipilification", "zyzzyva",
                 "sesquipedalianism", "antidisestablishmentarianism",
                 "obstreperousness"]
        for word in probe:
            item = _lookup(conn, word)
            _show_word(word, item)
            if item is not None and item["word"] == word:
                # 收录了也正常（有 tag/frq），不算失败
                pass

        print("\n  -- 覆盖率预估 --")
        ranked = conn.execute(
            "SELECT COUNT(*) FROM dict WHERE bnc > 0 OR frq > 0").fetchone()[0]
        print(f"  带词频排名的词条: {ranked}/{entries}")
        return ok
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    # 进度输出在 stderr、结果输出在 stdout；给 stdout 开行缓冲，避免重定向时
    # 两股流在日志里交错错位（终端里本来就正常）。
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(
        description="构建 ECDICT 裁剪词典 data/dict/ecdic.db",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--source", default=str(DEFAULT_SOURCE),
                        help="ecdict.csv 路径")
    parser.add_argument("--lemma", default=str(DEFAULT_LEMMA),
                        help="lemma.en.txt 路径")
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help="输出 .db 路径")
    parser.add_argument("--max-rank", type=int, default=60000,
                        help="bnc/frq 词频裁剪档位")
    parser.add_argument("--download", action="store_true",
                        help="从 GitHub 下载 ecdict.csv 与 lemma.en.txt 到 src 目录")
    parser.add_argument("--force", action="store_true",
                        help="配合 --download：已存在也重新下载")
    parser.add_argument("--check", action="store_true",
                        help="构建后跑示例查询并打印")
    parser.add_argument("--check-only", action="store_true",
                        help="只对已有数据库跑示例查询（跳过构建）")
    args = parser.parse_args(argv)

    source = Path(args.source).expanduser()
    lemma = Path(args.lemma).expanduser()
    out = Path(args.out).expanduser()

    if args.check_only:
        return 0 if run_checks(out) else 1

    if args.download:
        download(CSV_URL, source, force=args.force)
        download(LEMMA_URL, lemma, force=args.force)

    if not source.is_file():
        print(f"[错误] 找不到词库源文件: {source}\n"
              f"       先运行: python3 {Path(__file__).relative_to(ROOT)} --download",
              file=sys.stderr)
        return 2

    started = time.monotonic()
    print(f"[构建] {source}\n    -> {out}  (max_rank={args.max_rank})")
    try:
        info = build(source, out,
                     lemma_path=lemma if lemma.is_file() else None,
                     max_rank=args.max_rank,
                     progress=make_progress())
    except Exception as exc:  # noqa: BLE001 - CLI 顶层统一报错
        print(f"\n[错误] 构建失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    elapsed = time.monotonic() - started
    print(f"\r[完成] entries={info['entries']}  forms={info['forms']}  "
          f"体积={human_size(info['size_bytes'])}  "
          f"built_at={info['built_at']}  耗时={elapsed:.1f}s",
          file=sys.stderr)
    print(f"entries={info['entries']} forms={info['forms']} "
          f"size_bytes={info['size_bytes']} built_at={info['built_at']}")
    print(f"耗时 {elapsed:.1f}s，schema_version={SCHEMA_VERSION}")

    if args.check:
        passed = run_checks(out)
        if not passed:
            print("\n[自查失败]", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
