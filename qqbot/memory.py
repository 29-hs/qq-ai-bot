"""会话记忆：SQLite 持久化，重启不丢上下文。

一个「会话」= 一个私聊用户，或一个群。
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterator

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    session TEXT    NOT NULL,
    role    TEXT    NOT NULL,
    content TEXT    NOT NULL,
    ts      REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session, id);

CREATE TABLE IF NOT EXISTS sessions (
    key        TEXT PRIMARY KEY,
    enabled    INTEGER NOT NULL DEFAULT 1,
    persona    TEXT,
    updated_at REAL NOT NULL
);
"""


class MemoryStore:
    def __init__(self, db_path: str | Path, logger: Any = None) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.logger = logger
        self._lock = threading.Lock()
        self._init_db()

    # ------------------------------------------------------------------ #
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @contextlib.contextmanager
    def _cursor(self) -> Iterator[sqlite3.Connection]:
        """一次操作一个连接，事务自动提交，用完一定要 close。"""
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._lock, self._cursor() as conn:
            conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------ #
    # 对话
    # ------------------------------------------------------------------ #
    def add_turn(self, session: str, user_content: str, assistant_content: str) -> None:
        now = time.time()
        with self._lock, self._cursor() as conn:
            conn.executemany(
                "INSERT INTO messages(session, role, content, ts) VALUES (?, ?, ?, ?)",
                [
                    (session, "user", user_content, now),
                    (session, "assistant", assistant_content, now),
                ],
            )

    def history(
        self,
        session: str,
        max_turns: int = 10,
        max_chars: int = 4000,
        ttl_hours: float = 24.0,
    ) -> list[dict[str, str]]:
        """取最近若干轮对话，按时间正序返回，并做字数裁剪。"""
        if max_turns <= 0:
            return []
        limit = max_turns * 2
        cutoff = time.time() - ttl_hours * 3600 if ttl_hours > 0 else 0.0
        with self._lock, self._cursor() as conn:
            rows = conn.execute(
                "SELECT role, content FROM messages "
                "WHERE session = ? AND ts >= ? ORDER BY id DESC LIMIT ?",
                (session, cutoff, limit),
            ).fetchall()
        rows.reverse()

        messages = [{"role": row[0], "content": row[1]} for row in rows]

        # 按总字数从最早开始丢，至少保留最近一条
        if max_chars > 0:
            total = sum(len(m["content"]) for m in messages)
            while len(messages) > 1 and total > max_chars:
                total -= len(messages.pop(0)["content"])

        # 让上下文以「用户说的话」开头，避免模型一上来就接自己的话
        while messages and messages[0]["role"] != "user":
            messages.pop(0)
        return messages

    def reset(self, session: str) -> int:
        with self._lock, self._cursor() as conn:
            cur = conn.execute("DELETE FROM messages WHERE session = ?", (session,))
            return cur.rowcount

    def prune(self, ttl_hours: float) -> int:
        if ttl_hours <= 0:
            return 0
        cutoff = time.time() - ttl_hours * 3600
        with self._lock, self._cursor() as conn:
            cur = conn.execute("DELETE FROM messages WHERE ts < ?", (cutoff,))
            return cur.rowcount

    # ------------------------------------------------------------------ #
    # 会话设置（开关 / 自定义人设）
    # ------------------------------------------------------------------ #
    def _ensure_session(self, conn: sqlite3.Connection, session: str) -> None:
        conn.execute(
            "INSERT INTO sessions(key, enabled, persona, updated_at) VALUES (?, 1, NULL, ?) "
            "ON CONFLICT(key) DO NOTHING",
            (session, time.time()),
        )

    def is_enabled(self, session: str) -> bool:
        with self._lock, self._cursor() as conn:
            row = conn.execute("SELECT enabled FROM sessions WHERE key = ?", (session,)).fetchone()
        return True if row is None else bool(row[0])

    def set_enabled(self, session: str, enabled: bool) -> None:
        with self._lock, self._cursor() as conn:
            self._ensure_session(conn, session)
            conn.execute(
                "UPDATE sessions SET enabled = ?, updated_at = ? WHERE key = ?",
                (1 if enabled else 0, time.time(), session),
            )

    def get_persona(self, session: str) -> str | None:
        with self._lock, self._cursor() as conn:
            row = conn.execute("SELECT persona FROM sessions WHERE key = ?", (session,)).fetchone()
        return row[0] if row and row[0] else None

    def set_persona(self, session: str, persona: str | None) -> None:
        with self._lock, self._cursor() as conn:
            self._ensure_session(conn, session)
            conn.execute(
                "UPDATE sessions SET persona = ?, updated_at = ? WHERE key = ?",
                (persona, time.time(), session),
            )

    # ------------------------------------------------------------------ #
    def stats(self) -> dict[str, int]:
        with self._lock, self._cursor() as conn:
            messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            active = conn.execute("SELECT COUNT(DISTINCT session) FROM messages").fetchone()[0]
        return {"messages": int(messages), "sessions": int(sessions), "active_sessions": int(active)}
