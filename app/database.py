"""SQLite 持久化：事件仅追加（append-only），所有状态由重放派生。

追加在单个 IMMEDIATE 事务中完成：
1. 幂等键检查——同键同载荷复用既有响应，同键异载荷 409；
2. expected_next_seq 必须等于当前下一序号，否则 409（并发失败者）；
3. 插入事件行，提交。

事务开启即取写锁（BEGIN IMMEDIATE + busy_timeout），多客户端并发追加时，
序号不匹配的一方会收到 409 而不是互相覆盖。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Optional

from .errors import Conflict
from .replay import RawEvent

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq               INTEGER PRIMARY KEY AUTOINCREMENT,
    type              TEXT NOT NULL,
    submitted_by      TEXT NOT NULL,
    payload           TEXT NOT NULL,
    idempotency_key   TEXT,
    payload_hash      TEXT NOT NULL,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS idempotency (
    idempotency_key   TEXT PRIMARY KEY,
    seq               INTEGER NOT NULL,
    payload_hash      TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=10,
            isolation_level=None,  # 手动事务
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
        finally:
            conn.close()

    # ---- 读 ---------------------------------------------------------------

    def load_raw_events(self) -> list[RawEvent]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT seq, type, submitted_by, payload FROM events ORDER BY seq"
            ).fetchall()
            return [
                RawEvent(
                    seq=r["seq"],
                    type=r["type"],
                    submitted_by=r["submitted_by"],
                    payload=json.loads(r["payload"]),
                )
                for r in rows
            ]
        finally:
            conn.close()

    def next_seq(self) -> int:
        conn = self._connect()
        try:
            n = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
            return n + 1
        finally:
            conn.close()

    def find_idempotency(self, key: str) -> Optional[tuple[int, str]]:
        """预查幂等键：返回 (seq, payload_hash) 或 None。

        仅用于在昂贵的领域校验前短路；最终判定仍以 append_event 事务为准。
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT seq, payload_hash FROM idempotency WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            return (row["seq"], row["payload_hash"]) if row is not None else None
        finally:
            conn.close()

    # ---- 原子追加 ----------------------------------------------------------

    def append_event(
        self,
        *,
        event_type: str,
        submitted_by: str,
        payload: dict,
        payload_hash: str,
        expected_next_seq: int,
        idempotency_key: Optional[str],
    ) -> tuple[int, bool]:
        """返回 (seq, reused)；reused=True 表示幂等命中既有事件。"""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")

                if idempotency_key is not None:
                    existing = conn.execute(
                        "SELECT seq, payload_hash FROM idempotency WHERE idempotency_key = ?",
                        (idempotency_key,),
                    ).fetchone()
                    if existing is not None:
                        if existing["payload_hash"] != payload_hash:
                            raise Conflict(
                                "Idempotency-Key 已用于不同载荷",
                                code="idempotency_conflict",
                            )
                        conn.execute("COMMIT")
                        return existing["seq"], True

                count = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
                actual_next = count + 1
                if expected_next_seq != actual_next:
                    raise Conflict(
                        f"序号冲突：期望 {expected_next_seq}，实际下一序号 {actual_next}",
                        code="seq_conflict",
                    )

                cur = conn.execute(
                    "INSERT INTO events (type, submitted_by, payload, idempotency_key, payload_hash)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        event_type,
                        submitted_by,
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        idempotency_key,
                        payload_hash,
                    ),
                )
                seq = cur.lastrowid
                if idempotency_key is not None:
                    conn.execute(
                        "INSERT INTO idempotency (idempotency_key, seq, payload_hash)"
                        " VALUES (?, ?, ?)",
                        (idempotency_key, seq, payload_hash),
                    )
                conn.execute("COMMIT")
                return seq, False
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()
