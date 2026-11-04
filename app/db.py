"""SQLite 存储层：一张 videos + 一行一行的 segments/words。

本地单人用，不引 ORM，也不做并发池：每次操作开一个连接，WAL 模式。
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Iterable, Iterator

from .config import DB_FILE, ensure_dirs
from .models import Segment

_DAY_MS = 86_400_000
_VOCAB_STATUSES = ("learning", "known", "ignored")

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id     TEXT    NOT NULL UNIQUE,
    url          TEXT    NOT NULL,
    title        TEXT    DEFAULT '',
    channel      TEXT    DEFAULT '',
    duration_ms  INTEGER DEFAULT 0,
    thumb        TEXT    DEFAULT '',
    status       TEXT    NOT NULL DEFAULT 'queued',
    detail       TEXT    DEFAULT '',
    error        TEXT    DEFAULT '',
    media_path   TEXT    DEFAULT '',
    source       TEXT    DEFAULT '',
    progress     INTEGER DEFAULT 0,
    analysis_status TEXT DEFAULT 'none',
    analysis_done   INTEGER DEFAULT 0,
    analysis_total  INTEGER DEFAULT 0,
    analysis_model  TEXT    DEFAULT '',
    analysis_error  TEXT    DEFAULT '',
    created_at   TEXT    DEFAULT (datetime('now')),
    updated_at   TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS segments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    video_pk   INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    start_ms   INTEGER NOT NULL,
    end_ms     INTEGER NOT NULL,
    text       TEXT    NOT NULL,
    UNIQUE (video_pk, seq)
);

CREATE TABLE IF NOT EXISTS words (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    start_ms   INTEGER NOT NULL,
    end_ms     INTEGER NOT NULL,
    text       TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS playback (
    video_pk    INTEGER PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
    position_ms INTEGER NOT NULL DEFAULT 0,
    rate        REAL    NOT NULL DEFAULT 1.0,
    updated_at  TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS analysis (
    video_pk   INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    payload    TEXT    NOT NULL,
    model      TEXT    DEFAULT '',
    updated_at TEXT    DEFAULT (datetime('now')),
    PRIMARY KEY (video_pk, seq)
);

CREATE TABLE IF NOT EXISTS vocab (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    word          TEXT    NOT NULL UNIQUE,   -- 小写原词形（查词时的 surface）
    lemma         TEXT    DEFAULT '',        -- 词典里的原形（walked -> walk）
    phonetic      TEXT    DEFAULT '',
    pos           TEXT    DEFAULT '',
    tags          TEXT    DEFAULT '',        -- JSON: [{"code":"cet4","label":"四级"}]
    translation   TEXT    DEFAULT '',
    definition    TEXT    DEFAULT '',
    status        TEXT    NOT NULL DEFAULT 'learning',  -- learning / known / ignored
    note          TEXT    DEFAULT '',
    source_pk     INTEGER REFERENCES videos(id) ON DELETE SET NULL,
    source_seq    INTEGER DEFAULT 0,
    source_sentence TEXT  DEFAULT '',
    source_start_ms INTEGER DEFAULT 0,
    created_at    TEXT    DEFAULT (datetime('now')),
    updated_at    TEXT    DEFAULT (datetime('now')),
    stability     REAL    DEFAULT 0,
    difficulty    REAL    DEFAULT 0,
    due_at        INTEGER DEFAULT 0,
    reps          INTEGER DEFAULT 0,
    lapses        INTEGER DEFAULT 0,
    last_review   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS review_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    vocab_id      INTEGER NOT NULL REFERENCES vocab(id) ON DELETE CASCADE,
    grade         INTEGER NOT NULL,
    reviewed_at   INTEGER NOT NULL,
    elapsed_days  REAL    DEFAULT 0,
    scheduled_days REAL   DEFAULT 0,
    stability     REAL    DEFAULT 0,
    difficulty    REAL    DEFAULT 0
);

CREATE TABLE IF NOT EXISTS dict_explain (
    word          TEXT    NOT NULL,
    context_hash  TEXT    NOT NULL,
    payload       TEXT    NOT NULL,
    model         TEXT    DEFAULT '',
    created_at    TEXT    DEFAULT (datetime('now')),
    PRIMARY KEY (word, context_hash)
);

CREATE INDEX IF NOT EXISTS idx_segments_video ON segments(video_pk, seq);
CREATE INDEX IF NOT EXISTS idx_words_segment  ON words(segment_id, seq);
CREATE INDEX IF NOT EXISTS idx_vocab_status_due ON vocab(status, due_at);
CREATE INDEX IF NOT EXISTS idx_review_vocab ON review_log(vocab_id);
"""

ACTIVE_STATUSES = ("queued", "downloading", "transcribing", "segmenting")

# 老库升级：CREATE TABLE IF NOT EXISTS 不会给已存在的表补列，这里按需 ALTER。
_ADDED_COLUMNS: dict[str, tuple[str, str]] = {
    "progress": ("INTEGER", "0"),
    "analysis_status": ("TEXT", "'none'"),
    "analysis_done": ("INTEGER", "0"),
    "analysis_total": ("INTEGER", "0"),
    "analysis_model": ("TEXT", "''"),
    "analysis_error": ("TEXT", "''"),
}


def connect() -> sqlite3.Connection:
    ensure_dirs()
    con = sqlite3.connect(DB_FILE, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


@contextmanager
def cursor() -> Iterator[sqlite3.Connection]:
    con = connect()
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _ensure_columns(con: sqlite3.Connection) -> None:
    have = {row["name"] for row in con.execute("PRAGMA table_info(videos)")}
    for name, (kind, default) in _ADDED_COLUMNS.items():
        if name not in have:
            con.execute(f"ALTER TABLE videos ADD COLUMN {name} {kind} DEFAULT {default}")
    # 老库里的 ready 记录没有进度概念，补成 100，别让卡片上顶着个 0%
    con.execute("UPDATE videos SET progress=100 WHERE status='ready' AND progress=0")


def init_db() -> None:
    with cursor() as con:
        con.executescript(SCHEMA)
        _ensure_columns(con)


# ---------------------------------------------------------------- videos

def create_video(url: str, video_id: str, title: str = "", channel: str = "",
                 duration_ms: int = 0, thumb: str = "") -> int:
    with cursor() as con:
        con.execute(
            """INSERT INTO videos (url, video_id, title, channel, duration_ms, thumb)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(video_id) DO UPDATE SET
                 url=excluded.url, title=excluded.title, channel=excluded.channel,
                 duration_ms=excluded.duration_ms, thumb=excluded.thumb,
                 updated_at=datetime('now')""",
            (url, video_id, title, channel, duration_ms, thumb),
        )
        row = con.execute("SELECT id FROM videos WHERE video_id=?", (video_id,)).fetchone()
        return int(row["id"])


def get_video(pk: int) -> dict[str, Any] | None:
    with cursor() as con:
        row = con.execute("SELECT * FROM videos WHERE id=?", (pk,)).fetchone()
    return dict(row) if row else None


def find_by_video_id(video_id: str) -> dict[str, Any] | None:
    with cursor() as con:
        row = con.execute("SELECT * FROM videos WHERE video_id=?", (video_id,)).fetchone()
    return dict(row) if row else None


def list_videos() -> list[dict[str, Any]]:
    with cursor() as con:
        rows = con.execute(
            "SELECT * FROM videos ORDER BY updated_at DESC, id DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def count_segments(video_pk: int) -> int:
    with cursor() as con:
        row = con.execute(
            "SELECT COUNT(*) AS n FROM segments WHERE video_pk=?", (video_pk,)
        ).fetchone()
    return int(row["n"]) if row else 0


def update_video(pk: int, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with cursor() as con:
        con.execute(
            f"UPDATE videos SET {cols}, updated_at=datetime('now') WHERE id=?",
            (*fields.values(), pk),
        )


def set_status(pk: int, status: str, detail: str = "", error: str = "",
               progress: int | None = None) -> None:
    fields: dict[str, Any] = {"status": status, "detail": detail, "error": error}
    if progress is not None:
        fields["progress"] = max(0, min(100, int(progress)))
    update_video(pk, **fields)


def set_progress(pk: int, progress: float, detail: str | None = None,
                 status: str | None = None) -> None:
    """只推进度，不重置其它字段。流水线每走一步都调它，前端 2 秒轮询看这里。"""
    fields: dict[str, Any] = {"progress": max(0, min(100, int(round(progress * 100))))}
    if detail is not None:
        fields["detail"] = detail
    if status is not None:
        fields["status"] = status
    update_video(pk, **fields)


def delete_video(pk: int) -> None:
    with cursor() as con:
        con.execute("DELETE FROM videos WHERE id=?", (pk,))


def recover_interrupted() -> int:
    """进程重启时，把中断在中间态的任务标成失败，避免一直转圈。"""
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    with cursor() as con:
        cur = con.execute(
            f"""UPDATE videos SET status='failed', error='上次运行被中断，可重试',
                detail='', updated_at=datetime('now')
                WHERE status IN ({placeholders})""",
            ACTIVE_STATUSES,
        )
        return cur.rowcount


# ------------------------------------------------------------- subtitles

def replace_segments(video_pk: int, segments: Iterable[Segment]) -> int:
    segments = list(segments)
    with cursor() as con:
        con.execute("DELETE FROM segments WHERE video_pk=?", (video_pk,))
        total = 0
        for seg in segments:
            cur = con.execute(
                "INSERT INTO segments (video_pk, seq, start_ms, end_ms, text) VALUES (?,?,?,?,?)",
                (video_pk, seg.seq, seg.start_ms, seg.end_ms, seg.text),
            )
            seg_id = cur.lastrowid
            if seg.words:
                con.executemany(
                    "INSERT INTO words (segment_id, seq, start_ms, end_ms, text) VALUES (?,?,?,?,?)",
                    [
                        (seg_id, i, w.start_ms, w.end_ms, w.text)
                        for i, w in enumerate(seg.words)
                    ],
                )
            total += 1
    return total


def get_segments(video_pk: int) -> list[dict[str, Any]]:
    with cursor() as con:
        seg_rows = con.execute(
            "SELECT id, seq, start_ms, end_ms, text FROM segments WHERE video_pk=? ORDER BY seq",
            (video_pk,),
        ).fetchall()
        if not seg_rows:
            return []
        ids = [r["id"] for r in seg_rows]
        word_map: dict[int, list[dict[str, Any]]] = {i: [] for i in ids}
        placeholders = ",".join("?" for _ in ids)
        for w in con.execute(
            f"""SELECT segment_id, start_ms, end_ms, text FROM words
                WHERE segment_id IN ({placeholders}) ORDER BY segment_id, seq""",
            ids,
        ):
            word_map[w["segment_id"]].append(
                {"text": w["text"], "start_ms": w["start_ms"], "end_ms": w["end_ms"]}
            )
    return [
        {
            "seq": r["seq"],
            "start_ms": r["start_ms"],
            "end_ms": r["end_ms"],
            "text": r["text"],
            "words": word_map[r["id"]],
        }
        for r in seg_rows
    ]


# -------------------------------------------------------------- playback

def set_playback(video_pk: int, position_ms: int, rate: float | None = None) -> None:
    with cursor() as con:
        con.execute(
            """INSERT INTO playback (video_pk, position_ms, rate, updated_at)
               VALUES (?, ?, COALESCE(?, 1.0), datetime('now'))
               ON CONFLICT(video_pk) DO UPDATE SET
                 position_ms=excluded.position_ms,
                 rate=COALESCE(?, playback.rate),
                 updated_at=datetime('now')""",
            (video_pk, position_ms, rate, rate),
        )


def get_playback(video_pk: int) -> dict[str, Any]:
    with cursor() as con:
        row = con.execute("SELECT * FROM playback WHERE video_pk=?", (video_pk,)).fetchone()
    return dict(row) if row else {"position_ms": 0, "rate": 1.0}


# -------------------------------------------------------------- analysis

def start_analysis(video_pk: int, total: int, model: str) -> None:
    update_video(
        video_pk,
        analysis_status="running",
        analysis_done=0,
        analysis_total=int(total),
        analysis_model=model,
        analysis_error="",
    )


def bump_analysis(video_pk: int, done: int) -> None:
    update_video(video_pk, analysis_done=int(done))


def finish_analysis(video_pk: int, status: str, error: str = "") -> None:
    update_video(video_pk, analysis_status=status, analysis_error=error)


def mark_analysis_queued(video_pk: int) -> None:
    update_video(video_pk, analysis_status="queued", analysis_error="")


def put_analysis(video_pk: int, seq: int, payload: dict[str, Any], model: str = "") -> None:
    import json

    with cursor() as con:
        con.execute(
            """INSERT INTO analysis (video_pk, seq, payload, model, updated_at)
               VALUES (?, ?, ?, ?, datetime('now'))
               ON CONFLICT(video_pk, seq) DO UPDATE SET
                 payload=excluded.payload, model=excluded.model,
                 updated_at=datetime('now')""",
            (video_pk, int(seq), json.dumps(payload, ensure_ascii=False), model),
        )


def get_analysis(video_pk: int) -> dict[int, dict[str, Any]]:
    import json

    with cursor() as con:
        rows = con.execute(
            "SELECT seq, payload FROM analysis WHERE video_pk=? ORDER BY seq",
            (video_pk,),
        ).fetchall()
    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        try:
            out[int(row["seq"])] = json.loads(row["payload"])
        except ValueError:
            continue
    return out


def count_analysis(video_pk: int) -> int:
    with cursor() as con:
        row = con.execute(
            "SELECT COUNT(*) AS n FROM analysis WHERE video_pk=?", (video_pk,)
        ).fetchone()
    return int(row["n"]) if row else 0


def clear_analysis(video_pk: int) -> None:
    with cursor() as con:
        con.execute("DELETE FROM analysis WHERE video_pk=?", (video_pk,))
    update_video(
        video_pk,
        analysis_status="none",
        analysis_done=0,
        analysis_total=0,
        analysis_model="",
        analysis_error="",
    )


def recover_interrupted_analysis() -> int:
    """进程重启时，把停在「解析中」的记录标成失败，否则界面会一直转圈。"""
    with cursor() as con:
        cur = con.execute(
            """UPDATE videos SET analysis_status='failed',
                 analysis_error='上次运行被中断，可重试', updated_at=datetime('now')
               WHERE analysis_status IN ('queued', 'running')"""
        )
        return cur.rowcount


# ----------------------------------------------------------- 生词本 / 复习

_VOCAB_STATUSES = ("learning", "known", "ignored")
_DAY_MS = 86_400_000


def now_ms() -> int:
    return int(time.time() * 1000)


def today_start_ms() -> int:
    local = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return int(local.timestamp() * 1000)


def _load_tags(raw: str | None) -> list[dict[str, str]]:
    try:
        data = json.loads(raw or "[]")
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, str]] = []
    for entry in data:
        if isinstance(entry, dict) and entry.get("code"):
            out.append({"code": str(entry["code"]), "label": str(entry.get("label") or "")})
    return out


def _vocab_item(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["tags"] = _load_tags(item.get("tags"))
    due = int(item.get("due_at") or 0)
    if item.get("status") == "learning" and due:
        item["due_in_days"] = max(0, math.ceil((due - now_ms()) / _DAY_MS))
    elif item.get("status") == "learning":
        item["due_in_days"] = 0
    else:
        item["due_in_days"] = None
    item.setdefault("source_title", "")
    item["source_pk"] = item.get("source_pk") or None
    return item


_VOCAB_SELECT = """SELECT vocab.*, videos.title AS source_title
                     FROM vocab LEFT JOIN videos ON videos.id = vocab.source_pk"""


def vocab_get(vocab_id: int) -> dict[str, Any] | None:
    with cursor() as con:
        row = con.execute(_VOCAB_SELECT + " WHERE vocab.id=?", (vocab_id,)).fetchone()
    return _vocab_item(row) if row else None


def vocab_status_map() -> dict[str, dict[str, Any]]:
    """小写词形 → {id, status}，给查词接口回填「已收藏/已掌握」，不做 N 次查询。"""
    with cursor() as con:
        rows = con.execute("SELECT id, word, lemma, status FROM vocab").fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        mark = {"id": int(row["id"]), "status": row["status"]}
        for key in (row["word"], row["lemma"]):
            key = str(key or "").strip().lower()
            if key:
                out[key] = mark
    return out


def vocab_upsert(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """按 word 去重；已存在时只补空字段和来源，不覆盖用户改过的状态/笔记。"""
    word = str(payload.get("word") or "").strip().lower()
    if not word:
        raise ValueError("word 不能为空")
    tags = payload.get("tags") or []
    tags_json = json.dumps(
        [{"code": str(t.get("code")), "label": str(t.get("label") or "")}
         for t in tags if isinstance(t, dict) and t.get("code")],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    source = payload.get("source") or {}
    stamp = now_ms()
    with cursor() as con:
        # 前端可能带着已经删掉的视频 pk 过来（比如从生词本里点旧来源），别让 FK 报 500
        source_pk = source.get("pk")
        if source_pk is not None and not con.execute(
            "SELECT 1 FROM videos WHERE id=?", (source_pk,)
        ).fetchone():
            source_pk = None
        existing = con.execute("SELECT * FROM vocab WHERE word=?", (word,)).fetchone()
        if existing:
            con.execute(
                """UPDATE vocab SET
                     lemma       = COALESCE(NULLIF(?, ''), lemma),
                     phonetic    = COALESCE(NULLIF(?, ''), phonetic),
                     pos         = COALESCE(NULLIF(?, ''), pos),
                     tags        = CASE WHEN ?<>'' THEN ? ELSE tags END,
                     translation = COALESCE(NULLIF(?, ''), translation),
                     definition  = COALESCE(NULLIF(?, ''), definition),
                     source_pk   = COALESCE(?, source_pk),
                     source_seq  = CASE WHEN ? THEN ? ELSE source_seq END,
                     source_sentence = COALESCE(NULLIF(?, ''), source_sentence),
                     source_start_ms = CASE WHEN ? THEN ? ELSE source_start_ms END,
                     updated_at  = datetime('now')
                   WHERE id=?""",
                (
                    str(payload.get("lemma") or ""),
                    str(payload.get("phonetic") or ""),
                    str(payload.get("pos") or ""),
                    tags_json, tags_json,
                    str(payload.get("translation") or ""),
                    str(payload.get("definition") or ""),
                    source_pk,
                    source_pk is not None, source.get("seq") or 0,
                    str(source.get("sentence") or ""),
                    source.get("start_ms") is not None, int(source.get("start_ms") or 0),
                    int(existing["id"]),
                ),
            )
            created = False
            vocab_id = int(existing["id"])
        else:
            status = str(payload.get("status") or "learning").strip()
            if status not in _VOCAB_STATUSES:
                status = "learning"
            cur = con.execute(
                """INSERT INTO vocab
                     (word, lemma, phonetic, pos, tags, translation, definition,
                      status, note, source_pk, source_seq, source_sentence,
                      source_start_ms, due_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    word,
                    str(payload.get("lemma") or "").strip().lower(),
                    str(payload.get("phonetic") or ""),
                    str(payload.get("pos") or ""),
                    tags_json,
                    str(payload.get("translation") or ""),
                    str(payload.get("definition") or ""),
                    status,
                    str(payload.get("note") or ""),
                    source_pk,
                    int(source.get("seq") or 0),
                    str(source.get("sentence") or ""),
                    int(source.get("start_ms") or 0),
                    stamp if status == "learning" else 0,
                ),
            )
            vocab_id = int(cur.lastrowid or 0)
            created = True
    item = vocab_get(vocab_id)
    if not item:
        raise RuntimeError("生词写入后读不回来")
    return item, created


def vocab_list(
    *,
    status: str = "all",
    q: str = "",
    tag: str = "",
    limit: int = 50,
    offset: int = 0,
    sort: str = "recent",
) -> tuple[list[dict[str, Any]], int]:
    where: list[str] = []
    params: list[Any] = []
    status = (status or "all").strip()
    if status == "due":
        where.append("vocab.status='learning' AND vocab.due_at<=?")
        params.append(now_ms())
    elif status in _VOCAB_STATUSES:
        where.append("vocab.status=?")
        params.append(status)
    elif status not in ("all", ""):
        raise ValueError("status 只能是 all/learning/known/ignored/due")
    if q.strip():
        like = f"%{q.strip().lower()}%"
        where.append("(lower(vocab.word) LIKE ? OR lower(vocab.lemma) LIKE ? OR lower(vocab.translation) LIKE ?)")
        params.extend([like, like, like])
    if tag.strip():
        where.append("vocab.tags LIKE ?")
        params.append('%"code":"%s"%%' % tag.strip())
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    order = {
        "recent": "vocab.created_at DESC, vocab.id DESC",
        "due": "vocab.due_at ASC, vocab.id ASC",
        "alpha": "lower(vocab.word) ASC",
    }.get(sort, "vocab.created_at DESC, vocab.id DESC")
    limit = max(1, min(int(limit or 50), 500))
    offset = max(0, int(offset or 0))
    with cursor() as con:
        total = int(con.execute(
            "SELECT COUNT(*) AS n FROM vocab LEFT JOIN videos ON videos.id=vocab.source_pk" + clause,
            params,
        ).fetchone()["n"])
        rows = con.execute(
            _VOCAB_SELECT + clause + f" ORDER BY {order} LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
    return [_vocab_item(r) for r in rows], total


def vocab_stats() -> dict[str, Any]:
    now = now_ms()
    today = today_start_ms()
    with cursor() as con:
        counts = {row["status"]: int(row["n"]) for row in con.execute(
            "SELECT status, COUNT(*) AS n FROM vocab GROUP BY status"
        )}
        due_now = int(con.execute(
            "SELECT COUNT(*) AS n FROM vocab WHERE status='learning' AND due_at<=?", (now,)
        ).fetchone()["n"])
        due_today = int(con.execute(
            "SELECT COUNT(*) AS n FROM vocab WHERE status='learning' AND due_at<=?",
            (today + _DAY_MS,),
        ).fetchone()["n"])
        reviews_today = int(con.execute(
            "SELECT COUNT(*) AS n FROM review_log WHERE reviewed_at>=?", (today,)
        ).fetchone()["n"])
        days = [row["day"] for row in con.execute(
            """SELECT DISTINCT date(reviewed_at/1000, 'unixepoch', 'localtime') AS day
                 FROM review_log ORDER BY day DESC"""
        )]
    streak = 0
    if days:
        cursor_day = datetime.now().date()
        seen = set(days)
        # 今天没复习也不算断：从今天或昨天开始往前数连续天数
        if cursor_day.isoformat() not in seen:
            cursor_day = cursor_day - timedelta(days=1)
        while cursor_day.isoformat() in seen:
            streak += 1
            cursor_day = cursor_day - timedelta(days=1)
    return {
        "all": sum(counts.values()),
        "learning": counts.get("learning", 0),
        "known": counts.get("known", 0),
        "ignored": counts.get("ignored", 0),
        "due_now": due_now,
        "due_today": due_today,
        "reviews_today": reviews_today,
        "streak_days": streak,
    }


def vocab_update(vocab_id: int, *, status: str | None = None,
                 note: str | None = None) -> dict[str, Any] | None:
    fields: dict[str, Any] = {}
    if status is not None:
        status = status.strip()
        if status not in _VOCAB_STATUSES:
            raise ValueError("status 只能是 learning/known/ignored")
        fields["status"] = status
        if status != "learning":
            fields["due_at"] = 0
        elif not vocab_get(vocab_id):
            return None
    if note is not None:
        fields["note"] = str(note)
    if not fields:
        return vocab_get(vocab_id)
    cols = ", ".join(f"{k}=?" for k in fields)
    with cursor() as con:
        con.execute(f"UPDATE vocab SET {cols}, updated_at=datetime('now') WHERE id=?",
                    (*fields.values(), vocab_id))
    return vocab_get(vocab_id)


def vocab_delete(vocab_id: int) -> bool:
    with cursor() as con:
        cur = con.execute("DELETE FROM vocab WHERE id=?", (vocab_id,))
        return cur.rowcount > 0


def vocab_known() -> dict[str, list[str]]:
    """字幕打点用：learning 的词要在字幕里画线，known/ignored 不画。"""
    out: dict[str, list[str]] = {"learning": [], "known": [], "ignored": []}
    with cursor() as con:
        for row in con.execute("SELECT word, lemma, status FROM vocab"):
            status = row["status"] if row["status"] in out else "learning"
            for value in (row["word"], row["lemma"]):
                value = str(value or "").strip().lower()
                if value and value not in out[status]:
                    out[status].append(value)
    return out


def vocab_occurrences(word: str, lemma: str = "", limit: int = 30) -> list[dict[str, Any]]:
    """这句单词出现在哪些字幕行（跨视频，按最近更新排）。"""
    needle = (word or lemma or "").strip().lower()
    if not needle:
        return []
    pattern = "%" + needle + "%"
    with cursor() as con:
        rows = con.execute(
            """SELECT s.seq, s.text, s.start_ms, v.id AS pk, v.title
                 FROM segments s JOIN videos v ON v.id = s.video_pk
                WHERE lower(s.text) LIKE ? AND v.status='ready'
                ORDER BY v.updated_at DESC, v.id DESC, s.seq ASC
                LIMIT ?""",
            (pattern, max(int(limit) * 6, 60)),
        ).fetchall()
    regex = re.compile(rf"(?<![a-z]){re.escape(needle)}(?![a-z])", re.IGNORECASE)
    out: list[dict[str, Any]] = []
    for row in rows:
        text = str(row["text"] or "")
        if not regex.search(text.lower()):
            continue
        out.append({
            "pk": int(row["pk"]),
            "title": row["title"] or "",
            "seq": int(row["seq"]),
            "text": text,
            "start_ms": int(row["start_ms"] or 0),
        })
        if len(out) >= limit:
            break
    return out


def add_review(vocab_id: int, *, grade: int, reviewed_at: int, elapsed_days: float,
               scheduled_days: float, stability: float, difficulty: float) -> None:
    with cursor() as con:
        con.execute(
            """INSERT INTO review_log
                 (vocab_id, grade, reviewed_at, elapsed_days, scheduled_days, stability, difficulty)
               VALUES (?,?,?,?,?,?,?)""",
            (vocab_id, int(grade), int(reviewed_at), float(elapsed_days),
             float(scheduled_days), float(stability), float(difficulty)),
        )


def save_review_state(vocab_id: int, state: dict[str, Any]) -> dict[str, Any] | None:
    with cursor() as con:
        con.execute(
            """UPDATE vocab SET stability=?, difficulty=?, due_at=?, reps=?, lapses=?,
                 last_review=?, updated_at=datetime('now') WHERE id=?""",
            (
                float(state["stability"]), float(state["difficulty"]),
                int(state["due_at"]), int(state["reps"]), int(state["lapses"]),
                int(state["last_review"]), int(vocab_id),
            ),
        )
    return vocab_get(vocab_id)


def explain_get(word: str, context_hash: str) -> str | None:
    with cursor() as con:
        row = con.execute(
            "SELECT payload FROM dict_explain WHERE word=? AND context_hash=?",
            (word, context_hash),
        ).fetchone()
    return str(row["payload"]) if row else None


def explain_put(word: str, context_hash: str, payload: str, model: str = "") -> None:
    with cursor() as con:
        con.execute(
            """INSERT INTO dict_explain (word, context_hash, payload, model, created_at)
               VALUES (?,?,?,?,datetime('now'))
               ON CONFLICT(word, context_hash) DO UPDATE SET
                 payload=excluded.payload, model=excluded.model, created_at=datetime('now')""",
            (word, context_hash, payload, model),
        )
