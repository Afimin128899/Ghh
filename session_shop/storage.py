from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


class QueueLimitError(ValueError):
    """Raised when adding items would exceed the configured queue limit."""


class Database:
    """Small SQLite queue with atomic item claiming and restart recovery."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA secure_delete = ON")
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            try:
                connection.execute("PRAGMA journal_mode = WAL")
            except sqlite3.OperationalError:
                pass
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS queue (
                    id TEXT PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    file_path TEXT NOT NULL,
                    original_name TEXT NOT NULL,
                    auto_2fa TEXT,
                    proxy_json TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    chat_id INTEGER PRIMARY KEY,
                    auto_mode INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_flags (
                    name TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                )
                """
            )
            self._recover_queue(connection)
        try:
            Path(self.db_path).chmod(0o600)
        except OSError:
            pass

    @staticmethod
    def _recover_queue(connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            "SELECT id, file_path, status FROM queue WHERE status IN ('pending', 'processing')"
        ).fetchall()
        for row in rows:
            if not Path(row["file_path"]).is_file():
                connection.execute("DELETE FROM queue WHERE id = ?", (row["id"],))
            elif row["status"] == "processing":
                connection.execute(
                    "UPDATE queue SET status = 'pending' WHERE id = ?", (row["id"],)
                )

    def add_queue_item(
        self,
        item_id: str,
        chat_id: int,
        file_path: str,
        original_name: str,
        auto_2fa: str | None,
        proxy_dict: dict[str, Any] | None,
    ) -> None:
        self.add_queue_items(
            [
                {
                    "id": item_id,
                    "chat_id": chat_id,
                    "file_path": file_path,
                    "original_name": original_name,
                    "auto_2fa": auto_2fa,
                    "proxy_dict": proxy_dict,
                }
            ]
        )

    def add_queue_items(
        self,
        items: list[dict[str, Any]],
        *,
        max_active: int | None = None,
    ) -> None:
        if not items:
            return
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if max_active is not None:
                chat_id = int(items[0]["chat_id"])
                if any(int(item["chat_id"]) != chat_id for item in items):
                    raise ValueError("A queue batch must belong to one chat")
                active = connection.execute(
                    """
                    SELECT COUNT(*) FROM queue
                    WHERE chat_id = ? AND status IN ('pending', 'processing')
                    """,
                    (chat_id,),
                ).fetchone()[0]
                if active + len(items) > max_active:
                    raise QueueLimitError("Queue limit exceeded")

            connection.executemany(
                """
                INSERT INTO queue
                    (id, chat_id, file_path, original_name, auto_2fa, proxy_json, status)
                VALUES (?, ?, ?, ?, ?, ?, 'pending')
                """,
                [
                    (
                        item["id"],
                        item["chat_id"],
                        item["file_path"],
                        item["original_name"],
                        item.get("auto_2fa"),
                        json.dumps(item.get("proxy_dict"))
                        if item.get("proxy_dict")
                        else None,
                    )
                    for item in items
                ],
            )

    def pop_next_item(self, chat_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM queue
                WHERE chat_id = ? AND status = 'pending'
                ORDER BY created_at ASC, rowid ASC
                LIMIT 1
                """,
                (chat_id,),
            ).fetchone()
            if row is None:
                return None
            updated = connection.execute(
                "UPDATE queue SET status = 'processing' WHERE id = ? AND status = 'pending'",
                (row["id"],),
            )
            if updated.rowcount != 1:
                return None
            return dict(row)

    def remove_item(self, item_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT file_path FROM queue WHERE id = ?", (item_id,)
            ).fetchone()
            connection.execute("DELETE FROM queue WHERE id = ?", (item_id,))
            return str(row["file_path"]) if row else None

    def clear_chat_queue(self, chat_id: int) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT file_path FROM queue WHERE chat_id = ?", (chat_id,)
            ).fetchall()
            connection.execute("DELETE FROM queue WHERE chat_id = ?", (chat_id,))
            return [str(row["file_path"]) for row in rows]

    def count_queue(self, chat_id: int) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM queue WHERE chat_id = ? AND status = 'pending'",
                (chat_id,),
            ).fetchone()
            return int(row[0])

    def count_active_queue(self, chat_id: int) -> int:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) FROM queue
                WHERE chat_id = ? AND status IN ('pending', 'processing')
                """,
                (chat_id,),
            ).fetchone()
            return int(row[0])

    def count_all_pending(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM queue WHERE status = 'pending'"
            ).fetchone()
            return int(row[0])

    def set_clean_mode(self, chat_id: int, mode: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO settings (chat_id, auto_mode) VALUES (?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET auto_mode = excluded.auto_mode
                """,
                (chat_id, mode),
            )

    def get_clean_mode(self, chat_id: int, default: int = 1) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT auto_mode FROM settings WHERE chat_id = ?", (chat_id,)
            ).fetchone()
            return int(row[0]) if row is not None else default

    def get_flag(self, name: str, default: bool) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM bot_flags WHERE name = ?", (name,)
            ).fetchone()
            return bool(row[0]) if row else default

    def set_flag(self, name: str, enabled: bool) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO bot_flags (name, value) VALUES (?, ?)
                ON CONFLICT(name) DO UPDATE SET value = excluded.value
                """,
                (name, int(enabled)),
            )
