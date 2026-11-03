"""SQLite 存储层：一张 videos + 一行一行的 segments/words。

本地单人用，不引 ORM，也不做并发池：每次操作开一个连接，WAL 模式。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Iterable, Iterator

from .config import DB_FILE, ensure_dirs
from .models import Segment

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

CREATE INDEX IF NOT EXISTS idx_segments_video ON segments(video_pk, seq);
CREATE INDEX IF NOT EXISTS idx_words_segment  ON words(segment_id, seq);
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
