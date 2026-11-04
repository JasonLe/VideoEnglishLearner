"""本地词典：ECDICT 裁剪库的下载 / 构建 / 查询 / AI 详解。

设计取舍：
- 词典是**本地 SQLite**，查词 0 网络、毫秒级；大模型只在用户点「AI 详解」时用，
  所以平时查词不花钱、离线也能用。构建产物不进 git，运行时下载（设置页按钮）。
- 下载走标准库 urllib + config [network] proxy，与 llm.py 同理由：少依赖。
- 查词先查原词，再走 form 表（walked → walk），所以点字幕里任何变形都能命中。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterator

from . import db, llm, net
from .config import DATA_DIR, ROOT, cfg

# 考试大纲标签：顺序固定，前端直接按这个顺序渲染 chips
TAG_LABELS: tuple[tuple[str, str], ...] = (
    ("zk", "中考"),
    ("gk", "高考"),
    ("cet4", "四级"),
    ("cet6", "六级"),
    ("ky", "考研"),
    ("toefl", "托福"),
    ("ielts", "雅思"),
    ("gre", "GRE"),
)
_TAG_ORDER = {code: i for i, (code, _label) in enumerate(TAG_LABELS)}

BASELINE_RANK = 2000   # 词频排名 <= 2000 视为已知（覆盖日常对话 95% 左右）
TARGET_COVERAGE = 0.98  # 无辅助听懂的理想覆盖率
CHUNK = 256 * 1024

# 后台安装状态（单进程；重启后回到 idle，前端会看到已安装/未安装）
_STATE: dict[str, Any] = {
    "phase": "idle",     # idle | downloading | building | done | error
    "progress": 0,       # 0-100
    "message": "",
    "error": "",
}
_THREAD: threading.Thread | None = None
_LOCK = threading.Lock()
_ROW_CACHE: dict[str, dict[str, Any] | None] = {}
_LEMMA_CACHE: dict[str, str] = {}
_FORMS_CACHE: dict[str, list[str]] = {}


class DictError(RuntimeError):
    """带 HTTP 语义的词典错误（api.py 转成 detail）。"""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


# ------------------------------------------------------------------ 路径 / 状态

def default_path() -> Path:
    return DATA_DIR / "dict" / "ecdic.db"


def db_path() -> Path:
    raw = str(cfg.get("dict.db_path", "") or "").strip()
    if not raw:
        return default_path()
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (ROOT / path)


def is_installed() -> bool:
    return db_path().is_file()


def invalidate() -> None:
    """设置改了 db_path / 删库 / 重建后清缓存，避免查到旧词条。"""
    _ROW_CACHE.clear()
    _LEMMA_CACHE.clear()
    _FORMS_CACHE.clear()


def _connect_ro() -> sqlite3.Connection | None:
    path = db_path()
    if not path.is_file():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _meta(conn: sqlite3.Connection) -> dict[str, str]:
    try:
        rows = conn.execute("SELECT key, value FROM meta").fetchall()
    except sqlite3.Error:
        return {}
    return {str(r["key"]): str(r["value"]) for r in rows}


def _count(conn: sqlite3.Connection, table: str) -> int:
    try:
        return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
    except sqlite3.Error:
        return 0


def _busy() -> bool:
    return _THREAD is not None and _THREAD.is_alive()


def status() -> dict[str, Any]:
    path = db_path()
    installed = path.is_file()
    meta: dict[str, str] = {}
    entries = forms = 0
    if installed:
        conn = None
        try:
            conn = _connect_ro()
            if conn is not None:
                meta = _meta(conn)
                entries = int(meta.get("entries") or _count(conn, "dict"))
                forms = int(meta.get("forms") or _count(conn, "form"))
        except sqlite3.Error:
            # 文件坏了：当没装，让用户重建
            installed = False
        finally:
            if conn is not None:
                conn.close()

    if _busy():
        phase = _STATE["phase"]
        downloading = True
    elif installed:
        phase = "done"
        downloading = False
    elif _STATE["error"]:
        phase = "error"
        downloading = False
    else:
        phase = "idle"
        downloading = False

    return {
        "installed": installed,
        "path": str(path),
        "size_bytes": int(path.stat().st_size) if installed else 0,
        "entries": entries,
        "forms": forms,
        "built_at": meta.get("built_at", ""),
        "downloading": downloading,
        "phase": phase,
        "progress": int(_STATE["progress"] or 0),
        "message": _STATE["message"],
        "error": _STATE["error"],
        "source_url": str(cfg.get("dict.source_url", "") or ""),
        "max_rank": int(cfg.get("dict.max_rank", 60000) or 60000),
    }


# ------------------------------------------------------------------ 下载 / 构建

def install(force: bool = False) -> dict[str, Any]:
    """开后台线程下载并构建。已在下载/构建时返回 started=False。"""
    global _THREAD
    with _LOCK:
        if _busy():
            return {"started": False, "message": "正在下载/构建中，稍等"}
        if is_installed() and not force:
            return {"started": False, "installed": True, "message": "词典已安装"}
        _STATE.update(phase="downloading", progress=0, message="准备下载…", error="")
        _THREAD = threading.Thread(target=_install_worker, name="dict-install", daemon=True)
        _THREAD.start()
    return {"started": True}


def delete() -> dict[str, Any]:
    if _busy():
        raise DictError("正在下载/构建，不能删除", status=409)
    path = db_path()
    removed = False
    for target in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        try:
            target.unlink()
            removed = removed or target == path
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise DictError(f"删除失败：{exc}", status=500) from exc
    _STATE.update(phase="idle", progress=0, message="", error="")
    _ROW_CACHE.clear()
    _LEMMA_CACHE.clear()
    _FORMS_CACHE.clear()
    return {"ok": True, "removed": removed}


def _install_worker() -> None:
    src_dir = db_path().parent / "src"
    csv_path = src_dir / "ecdict.csv"
    lemma_path = src_dir / "lemma.en.txt"
    try:
        src_dir.mkdir(parents=True, exist_ok=True)
        _STATE.update(phase="downloading", progress=0, message="下载 ecdict.csv（约 63MB）…")
        _download(
            str(cfg.get("dict.source_url", "") or ""),
            csv_path,
            lambda f: _set_progress(0, 72, f),
            min_bytes=5_000_000,
        )

        lemma: Path | None = lemma_path
        try:
            _STATE.update(progress=72, message="下载 lemma.en.txt（约 2.2MB）…")
            _download(
                str(cfg.get("dict.lemma_url", "") or ""),
                lemma_path,
                lambda f: _set_progress(72, 78, f),
                min_bytes=50_000,
            )
        except Exception:
            lemma = None  # 缺 lemma 表只是词形还原弱一点，不中断

        _STATE.update(phase="building", progress=80, message="构建 SQLite…（首次约 20–60 秒）")
        from . import dictbuild  # 延迟导入：没下载词典时不需要它

        out = db_path()
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + ".tmp")
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass

        result = dictbuild.build(
            csv_path,
            tmp,
            lemma_path=lemma if lemma and lemma.is_file() else None,
            max_rank=int(cfg.get("dict.max_rank", 60000) or 60000),
            progress=lambda done, total: _set_progress(80, 99, done / max(1, total)),
        )
        if not tmp.is_file():
            raise DictError("构建没有产出词典文件")
        for suffix in ("-wal", "-shm"):
            try:
                out.with_name(out.name + suffix).unlink()
            except FileNotFoundError:
                pass
        os.replace(tmp, out)
        _ROW_CACHE.clear()
        _LEMMA_CACHE.clear()
        _FORMS_CACHE.clear()
        try:
            csv_path.unlink()  # 省 63MB；重建会重新下载
        except OSError:
            pass
        entries = int((result or {}).get("entries") or 0)
        _STATE.update(
            phase="done",
            progress=100,
            message=f"词典就绪：{entries:,} 条" if entries else "词典就绪",
            error="",
        )
    except Exception as exc:  # noqa: BLE001 - 后台线程必须吞掉异常，写进状态给前端看
        _STATE.update(phase="error", progress=0, message="安装失败", error=_friendly(exc))


def _set_progress(start: int, end: int, fraction: float) -> None:
    fraction = max(0.0, min(1.0, fraction))
    _STATE["progress"] = int(start + (end - start) * fraction)


def _friendly(exc: BaseException) -> str:
    if isinstance(exc, DictError):
        return str(exc)
    if isinstance(exc, urllib.error.HTTPError):
        return f"下载失败：HTTP {exc.code}（地址可能失效，可在设置里换镜像）"
    if isinstance(exc, urllib.error.URLError):
        return f"网络连不上：{getattr(exc, 'reason', exc)}（国内网络请在设置里填代理）"
    return str(exc) or exc.__class__.__name__


def _opener() -> urllib.request.OpenerDirector:
    return net.opener(str(cfg.proxy or ""))


def _download(url: str, dest: Path, progress: Callable[[float], None] | None,
              *, min_bytes: int = 0) -> None:
    url = (url or "").strip()
    if not url:
        raise DictError("下载地址是空的：设置页「词典」里填 source_url")
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "vel/1.0 (ECDICT fetcher)"})
    try:
        with _opener().open(request, timeout=120) as resp, open(part, "wb") as fh:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done / total if total else min(0.99, done / (min_bytes or 1) / 8))
            if total and done < total:
                raise DictError(f"下载不完整：{done}/{total} 字节")
            if min_bytes and done < min_bytes:
                raise DictError("下载到的内容不完整（可能被重定向到错误页），请换镜像重试")
        os.replace(part, dest)
    except Exception:
        try:
            part.unlink()
        except OSError:
            pass
        raise


# ------------------------------------------------------------------ 查词

def _normalize(text: str) -> str:
    value = (text or "").strip().lower().replace("\u2019", "'").replace("\u2018", "'")
    value = re.sub(r"\s+", " ", value)
    return value.strip(" \t\r\n.,!?;:\"'()[]{}")


def _split_lines(raw: str) -> list[str]:
    text = (raw or "").replace("\\n", "\n")
    return [line.strip() for line in text.splitlines() if line.strip()]


def _parse_tags(raw: str) -> list[dict[str, str]]:
    codes = {code for code in (raw or "").split() if code in _TAG_ORDER}
    ordered = sorted(codes, key=lambda code: _TAG_ORDER[code])
    labels = dict(TAG_LABELS)
    return [{"code": code, "label": labels[code]} for code in ordered]


def _rank(bnc: Any, frq: Any) -> int:
    ranks = [int(v) for v in (bnc, frq) if v and int(v) > 0]
    return min(ranks) if ranks else 0


def _row_for(word: str) -> dict[str, Any] | None:
    """原词 → form 表 → 原形，返回 dict 表整行（带缓存）。"""
    if word in _ROW_CACHE:
        return _ROW_CACHE[word]
    conn = _connect_ro()
    if conn is None:
        return None
    row: sqlite3.Row | None = None
    try:
        row = conn.execute("SELECT * FROM dict WHERE word=?", (word,)).fetchone()
        if row is None:
            try:
                link = conn.execute("SELECT word FROM form WHERE form=?", (word,)).fetchone()
            except sqlite3.Error:
                link = None
            if link is not None:
                row = conn.execute("SELECT * FROM dict WHERE word=?", (link["word"],)).fetchone()
    finally:
        conn.close()
    item = dict(row) if row is not None else None
    _ROW_CACHE[word] = item
    return item


_SUFFIXES = ("ies", "es", "s", "ied", "ed", "ing", "ly", "est", "er")


def _strip_candidates(word: str) -> list[str]:
    """按常见词尾反推可能的原形（expected→expect、running→run、studies→study）。"""
    out: list[str] = []
    n = len(word)

    def add(cand: str) -> None:
        if len(cand) >= 2 and cand != word and cand not in out:
            out.append(cand)

    if word.endswith("ies") and n > 4:
        add(word[:-3] + "y")
    if word.endswith("es") and n > 3:
        add(word[:-2])
    if word.endswith("s") and n > 2:
        add(word[:-1])
    if word.endswith("ied") and n > 4:
        add(word[:-3] + "y")
    if word.endswith("ed") and n > 3:
        add(word[:-2])
        add(word[:-1])
        if n > 4 and word[-3] == word[-4]:
            add(word[:-3])
    if word.endswith("ing") and n > 4:
        add(word[:-3])
        add(word[:-3] + "e")
        if n > 5 and word[-4] == word[-5]:
            add(word[:-4])
    if word.endswith("ly") and n > 4:
        add(word[:-2])
    if word.endswith("est") and n > 5:
        add(word[:-3])
        add(word[:-2])
    if word.endswith("er") and n > 4:
        add(word[:-2])
        add(word[:-1])
    return out


def _forms_for(word: str) -> list[str]:
    """从 form 表取变形（比 dict.forms 列可靠：含空格的短语不会被拆坏）。"""
    if word in _FORMS_CACHE:
        return _FORMS_CACHE[word]
    forms: list[str] = []
    conn = _connect_ro()
    if conn is not None:
        try:
            forms = [str(r["form"]) for r in conn.execute(
                "SELECT form FROM form WHERE word=? LIMIT 40", (word,)
            )]
        except sqlite3.Error:
            forms = []
        finally:
            conn.close()
    _FORMS_CACHE[word] = forms
    return forms


def _base_form(word: str) -> str:
    """form 表里的原形（was→be、saw→see）；不是变形词或查不到就返回空串。"""
    if word in _LEMMA_CACHE:
        return _LEMMA_CACHE[word]
    base = ""
    conn = _connect_ro()
    if conn is not None:
        try:
            link = conn.execute("SELECT word FROM form WHERE form=?", (word,)).fetchone()
            if link is not None:
                base = str(link["word"])
        except sqlite3.Error:
            base = ""
        finally:
            conn.close()
    _LEMMA_CACHE[word] = base
    return base


_INFLECTION_MARKS = ("过去式", "过去分词", "现在分词", "第三人称", "复数", "比较级", "最高级", "pl.")


def _is_inflection_row(row: Any) -> bool:
    """词条本身是 was / taken / children 这种变形条目（译文里带「的过去式」「pl.」之类）。

    入参可能是 lookup 组装好的 item（translations 是列表）、dict 表整行，或 sqlite3.Row。
    """
    if not row:
        return False
    if isinstance(row, dict) and "translations" in row:
        text = " ".join(row.get("translations") or [])
        text += " " + " ".join(row.get("definition") or [])
    elif isinstance(row, dict):
        text = f"{row.get('translation') or ''} {row.get('definition') or ''}"
    else:
        try:
            text = f"{row['translation'] or ''} {row['definition'] or ''}"
        except (KeyError, IndexError, TypeError):
            return False
    return any(mark in text for mark in _INFLECTION_MARKS)


def _stem_row(word: str) -> dict[str, Any] | None:
    """词形查不到时按词尾反推（expected→expect、running→run），取词频最好的那个。"""
    conn = _connect_ro()
    if conn is None:
        return None
    best: dict[str, Any] | None = None
    best_rank = 0
    try:
        for candidate in _strip_candidates(word):
            found = conn.execute("SELECT * FROM dict WHERE word=?", (candidate,)).fetchone()
            if found is None:
                continue
            rank = _rank(found["bnc"], found["frq"])
            if best is None or (rank and (not best_rank or rank < best_rank)):
                best = dict(found)
                best_rank = rank
    finally:
        conn.close()
    return best


def _resolve_lemma(conn: sqlite3.Connection, token: str, row: sqlite3.Row | None) -> tuple[str, int]:
    """给 token 找「最适合拿来算词频」的原形；返回 (lemma, 排名)。"""
    def rank_of(candidate: str) -> int:
        found = conn.execute(
            "SELECT bnc, frq FROM dict WHERE word=?", (candidate,)
        ).fetchone()
        return _rank(found["bnc"], found["frq"]) if found is not None else 0

    best_lemma = token
    best_rank = _rank(row["bnc"], row["frq"]) if row is not None else 0
    candidates = _strip_candidates(token)
    try:
        link = conn.execute("SELECT word FROM form WHERE form=?", (token,)).fetchone()
    except sqlite3.Error:
        link = None
    if link is not None:
        candidates.insert(0, str(link["word"]))
    for candidate in candidates:
        cand_rank = rank_of(candidate)
        if cand_rank and (best_rank == 0 or cand_rank < best_rank):
            best_rank = cand_rank
            best_lemma = candidate
    return best_lemma, best_rank


def _best_rank(*rows: dict[str, Any] | None) -> int:
    """变形词自身的 bnc/frq 常常失真（was 排 41040），取它和原形里更常见的那个。"""
    ranks = [_rank(r.get("bnc"), r.get("frq")) for r in rows if r]
    ranks = [r for r in ranks if r]
    if not ranks:
        return 0
    best = min(ranks)
    return best if best <= BASELINE_RANK else 0


def _item_from_row(row: dict[str, Any]) -> dict[str, Any]:
    word = str(row.get("word") or "")
    bnc = int(row.get("bnc") or 0)
    frq = int(row.get("frq") or 0)
    rank = _rank(bnc, frq)
    forms = [f for f in str(row.get("forms") or "").split() if f and f != word]
    return {
        "word": word,
        "lemma": word,
        "phonetic": str(row.get("phonetic") or ""),
        "pos": str(row.get("pos") or ""),
        "tags": _parse_tags(str(row.get("tag") or "")),
        "translations": _split_lines(str(row.get("translation") or "")),
        "definition": _split_lines(str(row.get("definition") or "")),
        "collins": int(row.get("collins") or 0),
        "oxford": int(row.get("oxford") or 0),
        "bnc": bnc,
        "frq": frq,
        "forms": forms,
        "known_rank": rank if rank and rank <= BASELINE_RANK else 0,
    }


def lookup(q: str, pk: int | None = None, seq: int | None = None) -> dict[str, Any]:
    query = (q or "").strip()
    if not query:
        return {"found": False, "query": "", "source": "none", "installed": is_installed()}
    if not is_installed():
        return {"found": False, "query": query, "source": "none", "installed": False,
                "message": "本地词典未安装，到设置页「词典」里下载"}

    normalized = _normalize(query)
    candidates = [normalized]
    if normalized.endswith("'s"):
        candidates.append(normalized[:-2])
    if normalized.endswith("s'") and len(normalized) > 2:
        candidates.append(normalized[:-1])

    row: dict[str, Any] | None = None
    source = "none"
    for candidate in candidates:
        if not candidate:
            continue
        row = _row_for(candidate)
        if row is not None:
            source = "dict" if candidate == row.get("word") else "lemma"
            break
    if row is None:  # 最后再按词尾反推一层（expected / running / studies）
        row = _stem_row(normalized)
        if row is not None:
            source = "stem"
    if row is None:
        return {"found": False, "query": query, "source": "none", "installed": True}

    item = _item_from_row(row)
    forms = _forms_for(str(item.get("word") or ""))
    if forms:
        item["forms"] = forms
    conn = _connect_ro()
    if conn is not None:
        try:
            found = conn.execute(
                "SELECT word, bnc, frq FROM dict WHERE word=?", (item["word"],)
            ).fetchone()
            _lemma, best = _resolve_lemma(conn, item["word"], found)
            item["known_rank"] = best if best and best <= BASELINE_RANK else 0
        finally:
            conn.close()
    lemma = item["lemma"]
    # 已收藏状态：surface → lemma 都查一遍
    marks = db.vocab_status_map()
    mark = None
    for key in (normalized, item["word"], lemma):
        if key and key in marks:
            mark = marks[key]
            break
    item.update({
        "found": True,
        "query": query,
        "status": (mark or {}).get("status"),
        "vocab_id": (mark or {}).get("id"),
        "source": source,
        "installed": True,
    })
    if pk and seq:
        item["context"] = _context_row(int(pk), int(seq))
    return item


def _context_row(pk: int, seq: int) -> dict[str, Any] | None:
    with db.cursor() as con:
        row = con.execute(
            "SELECT seq, text, start_ms FROM segments WHERE video_pk=? AND seq=?",
            (pk, seq),
        ).fetchone()
    if not row:
        return None
    return {"pk": pk, "seq": int(row["seq"]), "text": row["text"], "start_ms": int(row["start_ms"] or 0)}


# 覆盖率用的小工具：只要词形还原与词频，不构造完整 item
def meta_batch(words: Iterator[str] | list[str]) -> dict[str, dict[str, Any]]:
    """批量查 (原形, 词频排名)，一次连接搞定，给覆盖率用。"""
    tokens = {_normalize(w) for w in words if (w or "").strip()}
    out: dict[str, dict[str, Any]] = {}
    conn = _connect_ro()
    if conn is None:
        return out
    try:
        for token in tokens:
            if not token:
                continue
            row = conn.execute(
                "SELECT word, bnc, frq FROM dict WHERE word=?", (token,)
            ).fetchone()
            lemma, rank = _resolve_lemma(conn, token, row)
            out[token] = {"lemma": lemma, "rank": rank}
    finally:
        conn.close()
    return out


def forms_for(words: list[str]) -> list[str]:
    """给一批原形词，返回词典里登记的所有变形（供前端给字幕打「已收藏」标记）。"""
    tokens = [str(w or "").strip().lower() for w in words]
    tokens = list(dict.fromkeys([t for t in tokens if t]))
    if not tokens:
        return []
    conn = _connect_ro()
    if conn is None:
        return []
    out: set[str] = set()
    try:
        # SQLite 变量上限保守按 400 分块
        for i in range(0, len(tokens), 400):
            chunk = tokens[i : i + 400]
            marks = ",".join("?" * len(chunk))
            for row in conn.execute(
                f"SELECT form FROM form WHERE word IN ({marks}) LIMIT 8000", chunk
            ):
                form = str(row["form"] or "").strip().lower()
                if form and " " not in form and form[:1].isalpha():
                    out.add(form)
    finally:
        conn.close()
    return sorted(out)


def lemma_of(word: str) -> str:
    row = _row_for(_normalize(word))
    return str(row.get("word") or "") if row else ""


def rank_of(word: str) -> int:
    row = _row_for(_normalize(word))
    return _rank(row.get("bnc"), row.get("frq")) if row else 0


# ------------------------------------------------------------------ AI 详解

_SYSTEM_PROMPT = """你是给中文母语者做英语精听的精读助教。学生正在看视频，点了一个词想弄懂它在**这句话里**的意思。

只解释这个词在当前句子里的用法，不背词典、不铺开讲所有义项。要求：
- meaning：在这句话里的中文意思，一句话说清，可对比常见义；
- usage：用法说明，讲清它在这里的语法角色/搭配限制（1-2 句）；
- collocations：这句相关的固定搭配 0-3 条，每条 phrase + 中文 meaning；
- grammar：涉及的语法点（时态/从句/虚拟/被动等），一句话；没有就空字符串；
- examples：再造 1-2 个同用法的例句，en + zh；
- note：语感、连读、语气、文化背景里值得提醒的一点；没有就空字符串。

只输出 JSON 对象：
{"meaning":"...","usage":"...","collocations":[{"phrase":"...","meaning":"..."}],
 "grammar":"...","examples":[{"en":"...","zh":"..."}],"note":"..."}
用简体中文解释（例句英文），不要 markdown，不要多余字段。"""


def _clean_explanation(data: dict[str, Any]) -> dict[str, Any]:
    def text(key: str) -> str:
        value = data.get(key)
        return str(value).strip() if isinstance(value, (str, int, float)) else ""

    def pairs(key: str, first: str, second: str) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for entry in data.get(key) or []:
            if not isinstance(entry, dict):
                continue
            a = str(entry.get(first) or "").strip()
            b = str(entry.get(second) or "").strip()
            if a:
                out.append({first: a, second: b})
        return out[:3] if key == "collocations" else out[:2]

    return {
        "meaning": text("meaning"),
        "usage": text("usage"),
        "collocations": pairs("collocations", "phrase", "meaning"),
        "grammar": text("grammar"),
        "examples": pairs("examples", "en", "zh"),
        "note": text("note"),
    }


def explain(word: str, sentence: str, *, lemma: str = "", model: str | None = None) -> dict[str, Any]:
    """点「AI 详解」才走到这里：一个词 + 一句上下文 ≈ 几百 token。按 (word, 句子) 缓存。"""
    word = _normalize(word) or _normalize(lemma)
    sentence = (sentence or "").strip()
    if not word:
        raise DictError("缺 word")
    if not sentence:
        raise DictError("缺 sentence：AI 详解需要一句话做上下文")
    if not llm.configured():
        raise DictError("还没配大模型：到设置页「解析」分组填 base_url / api_key / model", status=400)

    speak = _normalize(lemma) if lemma else word
    context_hash = hashlib.sha1(f"{speak}|{sentence}".encode("utf-8")).hexdigest()[:16]
    cached = db.explain_get(speak, context_hash)
    if cached:
        try:
            payload = json.loads(cached)
        except ValueError:
            payload = None
        if isinstance(payload, dict) and payload.get("meaning"):
            return {
                "ok": True,
                "cached": True,
                "model": model or llm.model(),
                "explanation": _clean_explanation(payload),
            }

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": f"单词：{word}\n原形：{speak}\n句子：{sentence}"},
    ]
    name = model or llm.model()
    try:
        data = llm.json_chat(messages, model=name or None, temperature=0.3, max_tokens=800)
    except llm.LlmError as exc:
        raise DictError(str(exc), status=502) from exc
    explanation = _clean_explanation(data)
    if not explanation["meaning"]:
        raise DictError("模型没有给出释义，再点一次试试", status=502)
    db.explain_put(speak, context_hash, json.dumps(data, ensure_ascii=False), name or "")
    return {"ok": True, "cached": False, "model": name, "explanation": explanation}
