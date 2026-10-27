"""SQLite 持久层:任务、段落、翻译缓存。连接即开即关,WAL 模式,线程安全。"""
from __future__ import annotations

import datetime as _dt
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

_lock = threading.Lock()
_db_path: str | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    file_path TEXT NOT NULL,
    engine TEXT NOT NULL,
    retries INTEGER,
    pages TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paragraphs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    page INTEGER NOT NULL,
    source TEXT NOT NULL,
    translation TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    col INTEGER NOT NULL DEFAULT 0,
    wide INTEGER NOT NULL DEFAULT 0,
    top REAL NOT NULL DEFAULT 0,
    bottom REAL NOT NULL DEFAULT 0,
    left REAL NOT NULL DEFAULT 0,
    right REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_paragraphs_job ON paragraphs(job_id, seq);
CREATE TABLE IF NOT EXISTS pages_meta (
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    page INTEGER NOT NULL,
    w REAL NOT NULL,
    h REAL NOT NULL,
    ncols INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (job_id, page)
);
CREATE TABLE IF NOT EXISTS cache (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def init(db_path: str | Path) -> None:
    global _db_path
    _db_path = str(db_path)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        try:  # 旧库迁移:jobs 补 pages 列
            conn.execute("ALTER TABLE jobs ADD COLUMN pages TEXT")
        except sqlite3.OperationalError:
            pass
        conn.commit()
    finally:
        conn.close()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def execute(sql: str, params: tuple = ()) -> int:
    with _lock, closing(_connect()) as conn, conn:
        cur = conn.execute(sql, params)
        return cur.lastrowid


def query(sql: str, params: tuple = ()) -> list[dict]:
    with _lock, closing(_connect()) as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


# ---- 任务 ----

def create_job(filename: str, file_path: str, engine: str, retries: int | None = None,
               pages: str | None = None) -> int:
    return execute(
        "INSERT INTO jobs(filename, file_path, engine, retries, pages, status, created_at, updated_at)"
        " VALUES(?,?,?,?,?,'queued',?,?)",
        (filename, file_path, engine, retries, pages, _now(), _now()),
    )


def get_job(job_id: int) -> dict | None:
    rows = query("SELECT * FROM jobs WHERE id=?", (job_id,))
    return rows[0] if rows else None


def list_jobs() -> list[dict]:
    return query(
        "SELECT j.*,"
        " COUNT(p.id) AS total,"
        " SUM(CASE WHEN p.status='ok' THEN 1 ELSE 0 END) AS done,"
        " SUM(CASE WHEN p.status='failed' THEN 1 ELSE 0 END) AS failed"
        " FROM jobs j LEFT JOIN paragraphs p ON p.job_id=j.id"
        " GROUP BY j.id ORDER BY j.id DESC"
    )


def set_job(job_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    execute(
        f"UPDATE jobs SET {cols}, updated_at=? WHERE id=?",
        (*fields.values(), _now(), job_id),
    )


def job_counts(job_id: int) -> dict:
    rows = query(
        "SELECT COUNT(*) AS total,"
        " SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS done,"
        " SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,"
        " SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending"
        " FROM paragraphs WHERE job_id=?",
        (job_id,),
    )
    r = rows[0]
    return {k: r[k] or 0 for k in ("total", "done", "failed", "pending")}


def recover_interrupted() -> None:
    """服务重启后,把残留的运行中任务标记为 interrupted,由用户手动续跑。"""
    execute(
        "UPDATE jobs SET status='interrupted', updated_at=?"
        " WHERE status IN ('queued','extracting','translating')",
        (_now(),),
    )


def delete_job(job_id: int) -> None:
    execute("DELETE FROM paragraphs WHERE job_id=?", (job_id,))
    execute("DELETE FROM jobs WHERE id=?", (job_id,))


# ---- 段落 ----

def add_paragraphs(job_id: int, paras: list) -> None:
    conn = sqlite3.connect(_db_path, timeout=15)
    try:
        with _lock:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executemany(
                "INSERT INTO paragraphs(job_id, seq, page, source, col, wide, top, bottom, left, right)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                [(job_id, p.seq, p.page, p.text, p.col, int(p.wide),
                  p.top, p.bottom, p.left, p.right) for p in paras],
            )
            conn.commit()
    finally:
        conn.close()


def set_pages_meta(job_id: int, metas: list[dict]) -> None:
    conn = sqlite3.connect(_db_path, timeout=15)
    try:
        with _lock:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executemany(
                "INSERT OR REPLACE INTO pages_meta(job_id, page, w, h, ncols)"
                " VALUES(?,?,?,?,?)",
                [(job_id, m["page"], m["w"], m["h"], m["ncols"]) for m in metas],
            )
            conn.commit()
    finally:
        conn.close()


def get_pages_meta(job_id: int) -> list[dict]:
    return query(
        "SELECT page, w, h, ncols FROM pages_meta WHERE job_id=? ORDER BY page",
        (job_id,),
    )


def clear_paragraphs(job_id: int) -> None:
    execute("DELETE FROM paragraphs WHERE job_id=?", (job_id,))


def pending_paragraphs(job_id: int, limit: int) -> list[dict]:
    return query(
        "SELECT * FROM paragraphs WHERE job_id=? AND status='pending'"
        " ORDER BY seq LIMIT ?",
        (job_id, limit),
    )


def job_paragraphs(job_id: int) -> list[dict]:
    return query(
        "SELECT seq, page, source, translation, status, error, col, wide, top, bottom, left, right"
        " FROM paragraphs WHERE job_id=? ORDER BY seq",
        (job_id,),
    )


def mark_para(para_id: int, status: str, translation: str | None = None,
              error: str | None = None) -> None:
    execute(
        "UPDATE paragraphs SET status=?, translation=COALESCE(?, translation), error=?"
        " WHERE id=?",
        (status, translation, error, para_id),
    )


def reset_for_retry(job_id: int) -> None:
    """只重置失败段落;已完成段落保留,避免重复请求引擎。"""
    execute(
        "UPDATE paragraphs SET status='pending', error=NULL"
        " WHERE job_id=? AND status='failed'",
        (job_id,),
    )


# ---- 翻译缓存 ----

def cache_get(key: str) -> str | None:
    rows = query("SELECT v FROM cache WHERE k=?", (key,))
    return rows[0]["v"] if rows else None


def cache_put(key: str, value: str) -> None:
    execute(
        "INSERT OR REPLACE INTO cache(k, v, created_at) VALUES(?,?,?)",
        (key, value, _now()),
    )


# ---- 运行时设置(LLM 配置等;库值优先于 .env) ----

def get_setting(key: str) -> str | None:
    rows = query("SELECT v FROM settings WHERE k=?", (key,))
    return rows[0]["v"] if rows else None


def set_setting(key: str, value: str) -> None:
    execute(
        "INSERT INTO settings(k, v) VALUES(?,?)"
        " ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (key, value),
    )
