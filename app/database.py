"""SQLite 持久化：端、合同修订、换版尝试记录。

写操作在单连接内以 BEGIN IMMEDIATE 串行化；并发换版通过 revision 乐观锁
检测，冲突返回 409，失败保留原发布。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

DB_PATH = os.environ.get("CONTRACT_DB_PATH", "/data/contracts.db")

_init_lock = threading.Lock()
_initialized = False

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS endpoints (
    endpoint_id   TEXT PRIMARY KEY,
    role          TEXT NOT NULL CHECK (role IN ('producer', 'cleaner')),
    display_name  TEXT NOT NULL,
    revision      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contracts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint_id TEXT NOT NULL,
    revision    INTEGER NOT NULL,
    schema_json TEXT NOT NULL,
    published_at TEXT NOT NULL,
    UNIQUE (endpoint_id, revision),
    FOREIGN KEY (endpoint_id) REFERENCES endpoints(endpoint_id)
);

CREATE TABLE IF NOT EXISTS change_attempts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint_id     TEXT NOT NULL,
    role            TEXT NOT NULL,
    expected_revision INTEGER,
    candidate_json  TEXT NOT NULL,
    result          TEXT NOT NULL CHECK (result IN ('activated', 'rejected_incompatible', 'conflict', 'invalid_schema')),
    detail          TEXT NOT NULL,
    counterexample_json TEXT,
    rejected_cleaner_id TEXT,
    created_at      TEXT NOT NULL
);
"""

MAX_CLEANERS = 3
MAX_PRODUCERS = 1


def _connect() -> sqlite3.Connection:
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    # check_same_thread=False：FastAPI 可能把请求调度到不同线程；
    # 写事务由 BEGIN IMMEDIATE 串行化，配合 busy_timeout 保证并发安全。
    conn = sqlite3.connect(
        DB_PATH, timeout=30, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    global _initialized
    with _init_lock:
        if not _initialized:
            conn = _connect()
            try:
                conn.executescript(SCHEMA_SQL)
            finally:
                conn.close()
            _initialized = True
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# 读操作
# --------------------------------------------------------------------------- #
def list_endpoints(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute("SELECT * FROM endpoints ORDER BY role, endpoint_id").fetchall()
    return [dict(r) for r in rows]


def get_endpoint(conn: sqlite3.Connection, endpoint_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM endpoints WHERE endpoint_id=?", (endpoint_id,)).fetchone()
    return dict(row) if row else None


def get_current_contract(conn: sqlite3.Connection, endpoint_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        """
        SELECT c.* FROM contracts c
        JOIN endpoints e ON e.endpoint_id = c.endpoint_id
        WHERE c.endpoint_id=? AND c.revision = e.revision
        """,
        (endpoint_id,),
    ).fetchone()
    if not row:
        return None
    data = dict(row)
    data["schema"] = json.loads(data.pop("schema_json"))
    return data


def list_revisions(conn: sqlite3.Connection, endpoint_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT revision, published_at FROM contracts WHERE endpoint_id=? ORDER BY revision",
        (endpoint_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_revision(conn: sqlite3.Connection, endpoint_id: str, revision: int) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM contracts WHERE endpoint_id=? AND revision=?",
        (endpoint_id, revision),
    ).fetchone()
    if not row:
        return None
    data = dict(row)
    data["schema"] = json.loads(data.pop("schema_json"))
    return data


def list_attempts(conn: sqlite3.Connection, limit: int = 50) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM change_attempts ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["candidate"] = json.loads(d.pop("candidate_json"))
        if d.get("counterexample_json"):
            d["counterexample"] = json.loads(d.pop("counterexample_json"))
        else:
            d.pop("counterexample_json", None)
            d["counterexample"] = None
        out.append(d)
    return out


def published_schemas(conn: sqlite3.Connection, role: str) -> List[Dict[str, Any]]:
    """某角色下所有已发布（revision>=1）端的当前合同。"""
    rows = conn.execute(
        """
        SELECT e.endpoint_id, e.display_name, c.schema_json, c.revision
        FROM endpoints e
        JOIN contracts c ON c.endpoint_id = e.endpoint_id AND c.revision = e.revision
        WHERE e.role=? AND e.revision >= 1
        ORDER BY e.endpoint_id
        """,
        (role,),
    ).fetchall()
    return [
        {
            "endpoint_id": r["endpoint_id"],
            "display_name": r["display_name"],
            "revision": r["revision"],
            "schema": json.loads(r["schema_json"]),
        }
        for r in rows
    ]


# --------------------------------------------------------------------------- #
# 写操作
# --------------------------------------------------------------------------- #
def insert_endpoint(conn: sqlite3.Connection, endpoint_id: str, role: str, display_name: str) -> None:
    conn.execute(
        "INSERT INTO endpoints (endpoint_id, role, display_name, revision, created_at) VALUES (?,?,?,0,?)",
        (endpoint_id, role, display_name, utc_now()),
    )


def insert_contract(conn: sqlite3.Connection, endpoint_id: str, revision: int, schema: dict) -> None:
    conn.execute(
        "INSERT INTO contracts (endpoint_id, revision, schema_json, published_at) VALUES (?,?,?,?)",
        (endpoint_id, revision, json.dumps(schema, ensure_ascii=False), utc_now()),
    )


def bump_revision(conn: sqlite3.Connection, endpoint_id: str, revision: int) -> None:
    conn.execute(
        "UPDATE endpoints SET revision=? WHERE endpoint_id=?", (revision, endpoint_id)
    )


def insert_attempt(
    conn: sqlite3.Connection,
    endpoint_id: str,
    role: str,
    expected_revision: Optional[int],
    candidate: dict,
    result: str,
    detail: str,
    counterexample: Optional[dict] = None,
    rejected_cleaner_id: Optional[str] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO change_attempts
          (endpoint_id, role, expected_revision, candidate_json, result, detail,
           counterexample_json, rejected_cleaner_id, created_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            endpoint_id,
            role,
            expected_revision,
            json.dumps(candidate, ensure_ascii=False),
            result,
            detail,
            json.dumps(counterexample, ensure_ascii=False) if counterexample is not None else None,
            rejected_cleaner_id,
            utc_now(),
        ),
    )
