"""ECDICT 裁剪词典构建（纯标准库）。

从 ECDICT 的 ``ecdict.csv``（76 万词条）裁剪出 ``data/dict/ecdic.db``，
schema 见 ``docs/vocab-dict-api.md`` 第 1 节。

用法（一般不直接跑本模块，用 ``tools/build_dict.py``）::

    from app.dictbuild import build
    info = build(Path("data/dict/src/ecdict.csv"),
                 Path("data/dict/ecdic.db"),
                 lemma_path=Path("data/dict/src/lemma.en.txt"),
                 max_rank=60000,
                 progress=lambda done, total: print(done, total))

``build`` 是原子的：先写同目录临时文件，成功后再 ``os.replace`` 覆盖目标文件。
"""
from __future__ import annotations

import csv
import os
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 1

# ECDICT 列顺序；真实文件首行是表头，这里只作为缺表头时的兜底。
_DEFAULT_COLUMNS = (
    "word",
    "phonetic",
    "definition",
    "translation",
    "pos",
    "collins",
    "oxford",
    "tag",
    "bnc",
    "frq",
    "exchange",
    "detail",
    "audio",
)

# exchange 中表示「变形形式」的键：
#   p 过去式  d 过去分词  i 现在分词 3 第三人称单数 r 比较级 t 最高级 s 复数
# 0 = 当前词的 lemma；1 = 当前词相对 lemma 的变形类型标记（值不是词形）。
_FORM_KEYS = frozenset("pdi3rts")

_BATCH = 5000
_PROGRESS_STEP = 5000

_CREATE_DICT = """
CREATE TABLE dict (
    word        TEXT PRIMARY KEY,
    phonetic    TEXT DEFAULT '',
    translation TEXT DEFAULT '',
    definition  TEXT DEFAULT '',
    pos         TEXT DEFAULT '',
    collins     INTEGER DEFAULT 0,
    oxford      INTEGER DEFAULT 0,
    tag         TEXT DEFAULT '',
    bnc         INTEGER DEFAULT 0,
    frq         INTEGER DEFAULT 0,
    forms       TEXT DEFAULT ''
)
"""

_CREATE_FORM = """
CREATE TABLE form (
    form TEXT PRIMARY KEY,
    word TEXT NOT NULL
)
"""

_CREATE_META = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT
)
"""

_INSERT_DICT = (
    "INSERT OR REPLACE INTO dict "
    "(word, phonetic, translation, definition, pos, collins, oxford, tag, "
    "bnc, frq, forms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

_INSERT_FORM = "INSERT OR REPLACE INTO form (form, word) VALUES (?, ?)"


def _to_int(value: object, default: int = 0) -> int:
    """把 CSV 字段转 int；空串 / 非法值 / None → default。"""
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    try:
        return int(text)
    except ValueError:
        try:
            return int(float(text))
        except (TypeError, ValueError):
            return default


def _clean(value: object) -> str:
    """去掉首尾空白并把 CRLF/CR 统一成 \\n（保留字段内部换行）。"""
    if value is None:
        return ""
    return str(value).replace("\r\n", "\n").replace("\r", "\n").strip()


def _clean_multiline(value: object) -> str:
    """translation/definition：转成真正的多行文本。

    注意 ECDICT 的 csv 里换行是**字面量转义**（``\\n``、``\\r\\n`` 两个字符），
    不是字段内真实换行；这里统一还原为 ``\\n``，并把真实 CRLF/CR 也规范化。
    其他非换行转义（如 ``\\_``、``\\[``）原样保留。
    """
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\\r\\n", "\n").replace("\\n", "\n")
    return text.strip()


def _parse_exchange(value: object) -> tuple[list[str], str]:
    """解析 exchange 字段。

    ECDICT 的 exchange 形如 ``p:walked/d:walked/i:walking/3:walks``；
    值内还可能继续用 ``/`` 分隔多个变形（如 ``p:was/were``），
    因此遇到不带 ``key:`` 的片段时并入上一个键。
    返回 ``(variants, lemma)``：variants 是去重后的 p/d/i/3/r/t/s 变形，
    lemma 是 ``0:`` 给出的原形（没有则为空串）。
    """
    variants: list[str] = []
    lemma = ""
    if not value:
        return variants, lemma
    current = ""
    for chunk in str(value).split("/"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" in chunk:
            key, _, rest = chunk.partition(":")
            key = key.strip()
            rest = rest.strip()
            if key == "0":
                lemma = rest
                current = ""
            elif key == "1":
                current = ""  # 1:p 之类只是「这个词是原形的哪种变形」，值不是词形
            elif key in _FORM_KEYS:
                current = key
                if rest:
                    variants.append(rest)
            else:
                current = ""
        elif current:
            variants.append(chunk)
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in variants:
        item = item.strip().lower()
        if not item or item in seen:
            continue
        seen.add(item)
        cleaned.append(item)
    return cleaned, lemma.strip().lower()


def _read_lemma_file(lemma_path: Path) -> dict[str, str]:
    """读取 lemma.en.txt，返回 {变形: 原形}。

    文件格式（2026-11 master 版本）::

        ; En Lemma Database (version 1.0.3)   ← 分号开头是注释
        ; ...
        be/4109826 -> is,was,are,were,'s,been,being,'re,'m,am,m
        walk/24949 -> walked,walking,walks
        zymase -> zymases

    即 ``原形[/词频] -> 变形1,变形2,...``；逐行解析，注释/空行跳过。
    若某行没有 ``->`` 视为脏行跳过。原形内部不做分词。
    """
    mapping: dict[str, str] = {}
    if lemma_path is None:
        return mapping
    lemma_path = Path(lemma_path)
    if not lemma_path.exists():
        return mapping
    with open(lemma_path, "r", encoding="utf-8-sig", errors="replace",
              newline="") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith(";"):
                continue
            if "->" not in line:
                continue
            left, _, right = line.partition("->")
            # 原形可能带 "/词频" 后缀，例如 be/4109826
            lemma = left.split("/", 1)[0].strip().lower()
            if not lemma:
                continue
            for item in right.split(","):
                form = item.strip().lower()
                if not form or form == lemma:
                    continue
                mapping.setdefault(form, lemma)
    return mapping


def _count_records(csv_path: Path) -> int:
    """估算/统计 CSV 记录数（用于 progress 的 total，多行字段也能正确计一条）。

    读一遍文件但不处理字段，代价约 1 秒/63MB。
    """
    limit = min(1 << 24, 2 ** 31 - 1)
    csv.field_size_limit(limit)
    count = 0
    try:
        with open(csv_path, "r", encoding="utf-8-sig", errors="replace",
                  newline="") as fh:
            reader = csv.reader(fh)
            for _ in reader:
                count += 1
    except csv.Error:
        # 统计失败不影响构建，按 0 处理（调用方会退化为不报 total）
        return 0
    return count


def _read_csv_rows(csv_path: Path):
    """逐行产出 (row_no, dict)，字段名由首行表头决定；缺表头则用默认列序。"""
    limit = min(1 << 24, 2 ** 31 - 1)
    csv.field_size_limit(limit)
    with open(csv_path, "r", encoding="utf-8-sig", errors="replace",
              newline="") as fh:
        reader = csv.reader(fh)
        try:
            first = next(reader)
        except StopIteration:
            return
        first_l = [cell.strip().lower() for cell in first]
        if first_l and first_l[0] == "word":
            columns = first_l
            data_row = None
        else:
            columns = list(_DEFAULT_COLUMNS)
            data_row = first
        width = len(columns)
        index = {name: i for i, name in enumerate(columns)}

        def make(row: list[str]) -> dict[str, str]:
            if len(row) < width:
                row = row + [""] * (width - len(row))
            return {name: row[i] for name, i in index.items()}

        row_no = 0
        if data_row is not None:
            row_no = 1
            yield row_no, make(data_row)
        try:
            for row in reader:
                row_no += 1
                yield row_no, make(row)
        except csv.Error:
            # 极少数脏行会让 csv.reader 抛错；交给上层决定是否记录。
            # 生成器在此结束，已处理的记录不受影响。
            return


def _keep(row: dict[str, str], max_rank: int) -> bool:
    """裁剪规则：bnc/frq 排名进档，或带考试 tag，或牛津核心词，或柯林斯星级。"""
    bnc = _to_int(row.get("bnc"))
    frq = _to_int(row.get("frq"))
    if 0 < bnc <= max_rank:
        return True
    if 0 < frq <= max_rank:
        return True
    if _clean(row.get("tag")):
        return True
    if _to_int(row.get("oxford")) == 1:
        return True
    if _to_int(row.get("collins")) > 0:
        return True
    return False


def build(csv_path: Path, out_path: Path, *, lemma_path: Path | None = None,
          max_rank: int = 60000,
          progress: Callable[[int, int], None] | None = None) -> dict:
    """从 ECDICT csv 构建裁剪 SQLite 词典。

    :param csv_path: ecdict.csv 路径
    :param out_path: 输出 .db 路径（原子替换）
    :param lemma_path: lemma.en.txt 路径，可选；作为 exchange 的补充
    :param max_rank: bnc/frq 裁剪档位（<=max_rank 的进库）。注意 ECDICT csv 里
        bnc 最大 50000、frq 最大 47062，档位高于 ~50000 后不再增加词条；此时库里
        是全量「带词频排名 + 带 tag/oxford/collins」的约 5.9 万条。
    :param progress: 可选回调 ``progress(done, total)``；``done`` 是已扫描行数，
        ``total`` 是 CSV 总记录数（统计失败时为 0）。``total`` 可能为 0 时调用方
        应显示为不确定进度。
    :return: ``{"entries", "forms", "size_bytes", "built_at"}``
    """
    csv_path = Path(csv_path)
    out_path = Path(out_path)
    if not csv_path.is_file():
        raise FileNotFoundError(f"词库源文件不存在: {csv_path}")
    if lemma_path is not None:
        lemma_path = Path(lemma_path)
        if not lemma_path.is_file():
            raise FileNotFoundError(f"lemma 文件不存在: {lemma_path}")
    max_rank = int(max_rank)

    # ---- 第一遍：统计总行数（progress 用），同时确认 CSV 可解析 ----
    total_rows = _count_records(csv_path)
    header_offset = 1 if total_rows else 0
    total = max(total_rows - header_offset, 0)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_name(f"{out_path.name}.{os.getpid()}.tmp")
    if tmp_path.exists():
        try:
            tmp_path.unlink()
        except OSError:
            pass

    entries: dict[str, tuple] = {}
    exchange_forms: dict[str, str] = {}
    last_row = 0
    built_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    def report(done: int) -> None:
        if progress is not None:
            try:
                progress(done, total)
            except Exception:  # 进度回调不应打断构建
                pass

    try:
        # ---- 第二遍：解析 + 裁剪，同时收集 exchange 词形 ----
        for row_no, row in _read_csv_rows(csv_path):
            last_row = row_no
            if progress is not None and row_no % _PROGRESS_STEP == 0:
                report(row_no)
            word = _clean(row.get("word")).lower()
            if not word:
                continue
            # 收集词形：即便词条本身不被裁剪保留，只要原形在库里最终就会入库；
            # 这里先记下所有 exchange 变形，第二阶段按「原形在 dict」过滤。
            variants, lemma_value = _parse_exchange(row.get("exchange"))
            if variants:
                for form in variants:
                    if form and form != word:
                        exchange_forms.setdefault(form, word)
            if lemma_value and lemma_value != word:
                # 当前词是 lemma_value 的变形（0: 字段）
                exchange_forms.setdefault(word, lemma_value)
            if not _keep(row, max_rank):
                continue
            forms_text = " ".join(variants)
            entries[word] = (
                word,
                _clean(row.get("phonetic")),
                _clean_multiline(row.get("translation")),
                _clean_multiline(row.get("definition")),
                _clean(row.get("pos")),
                _to_int(row.get("collins")),
                _to_int(row.get("oxford")),
                _clean(row.get("tag")),
                _to_int(row.get("bnc")),
                _to_int(row.get("frq")),
                forms_text,
            )
        report(min(last_row, total))

        # csv.reader 遇到无法解析的行会抛错并中断（_read_csv_rows 会吞掉异常），
        # 这里做最后校验，避免静默写出一份不完整的库。
        if total and last_row < total - 1:
            raise RuntimeError(
                f"CSV 解析在第 {last_row}/{total} 条记录后中断，"
                f"拒绝写入不完整的词典数据库"
            )

        # ---- lemma.en.txt 作为补充；exchange 已有的映射优先，不覆盖 ----
        lemma_forms: dict[str, str] = {}
        if lemma_path is not None:
            for form, lemma in _read_lemma_file(lemma_path).items():
                if form in exchange_forms:
                    continue
                lemma_forms[form] = lemma

        # form 表：只保留原形（lemma）确实在 dict 里的映射。
        # exchange 优先，lemma 补充；同一 form 已存在时不覆盖（保持先到的映射）。
        final_forms: dict[str, str] = {}
        for form, lemma in exchange_forms.items():
            if form != lemma and lemma in entries:
                final_forms[form] = lemma
        for form, lemma in lemma_forms.items():
            if form != lemma and lemma in entries:
                final_forms.setdefault(form, lemma)

        # ---- 写库（先建表，再批量插数据，最后建索引） ----
        conn = sqlite3.connect(tmp_path)
        try:
            conn.execute("PRAGMA journal_mode=OFF")
            conn.execute("PRAGMA synchronous=OFF")
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("PRAGMA cache_size=-64000")
            conn.executescript(
                _CREATE_DICT + ";" + _CREATE_FORM + ";" + _CREATE_META
            )

            batch: list[tuple] = []
            for record in entries.values():
                batch.append(record)
                if len(batch) >= _BATCH:
                    conn.executemany(_INSERT_DICT, batch)
                    batch.clear()
            if batch:
                conn.executemany(_INSERT_DICT, batch)
                batch.clear()

            for form, lemma in final_forms.items():
                batch.append((form, lemma))
                if len(batch) >= _BATCH:
                    conn.executemany(_INSERT_FORM, batch)
                    batch.clear()
            if batch:
                conn.executemany(_INSERT_FORM, batch)
                batch.clear()

            conn.execute("CREATE INDEX idx_dict_bnc ON dict(bnc)")
            conn.execute("CREATE INDEX idx_dict_frq ON dict(frq)")

            meta = {
                "entries": str(len(entries)),
                "forms": str(len(final_forms)),
                "built_at": built_at,
                "source": csv_path.name,
                "max_rank": str(max_rank),
                "schema_version": str(SCHEMA_VERSION),
            }
            conn.executemany(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                list(meta.items()),
            )
            conn.commit()
            conn.execute("VACUUM")
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.commit()
        finally:
            conn.close()

        # ---- 原子替换 ----
        os.replace(tmp_path, out_path)
    except BaseException:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise

    size_bytes = os.path.getsize(out_path)
    return {
        "entries": len(entries),
        "forms": len(final_forms),
        "size_bytes": size_bytes,
        "built_at": built_at,
    }


if __name__ == "__main__":  # 简单直接运行：python3 -m app.dictbuild
    import argparse

    parser = argparse.ArgumentParser(description="构建 ECDICT 裁剪词典")
    parser.add_argument("--source", default="data/dict/src/ecdict.csv")
    parser.add_argument("--lemma", default="data/dict/src/lemma.en.txt")
    parser.add_argument("--out", default="data/dict/ecdic.db")
    parser.add_argument("--max-rank", type=int, default=60000)
    args = parser.parse_args()

    def _show(done: int, total: int) -> None:
        if total:
            print(f"\r构建中 {done}/{total} ({done * 100 // total}%)",
                  end="", flush=True)
        else:
            print(f"\r构建中 {done}", end="", flush=True)

    info = build(Path(args.source), Path(args.out),
                 lemma_path=Path(args.lemma), max_rank=args.max_rank,
                 progress=_show)
    print()
    print(info)
