from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Optional

from .models import Task, TaskStatus, utc_now_iso


class TaskQueue:
    def __init__(self, db_path: Path, poll_interval_seconds: float = 2.0) -> None:
        self.db_path = db_path
        self.poll_interval_seconds = poll_interval_seconds
        self._lock = asyncio.Lock()

    async def init(self) -> None:
        await asyncio.to_thread(self._init_sync)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_sync(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    context TEXT,
                    result TEXT,
                    error TEXT,
                    retry_count INTEGER DEFAULT 0,
                    max_retries INTEGER DEFAULT 2,
                    telegram_chat_id TEXT,
                    session_key TEXT,
                    output_md_path TEXT,
                    output_json_path TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS session_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_key TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    task_id TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_session_messages_key_time ON session_messages(session_key, created_at)"
            )
            conn.commit()

    async def enqueue(self, task: Task) -> None:
        async with self._lock:
            await asyncio.to_thread(self._enqueue_sync, task)

    def _enqueue_sync(self, task: Task) -> None:
        now = utc_now_iso()
        created = task.created_at or now
        updated = task.updated_at or now
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tasks (
                    id, created_at, updated_at, source, status, prompt, context, result,
                    error, retry_count, max_retries, telegram_chat_id, session_key,
                    output_md_path, output_json_path
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.id,
                    created,
                    updated,
                    task.source,
                    task.status.value,
                    task.prompt,
                    task.context,
                    task.result,
                    task.error,
                    task.retry_count,
                    task.max_retries,
                    task.telegram_chat_id,
                    task.session_key,
                    task.output_md_path,
                    task.output_json_path,
                ),
            )
            conn.commit()

    async def dequeue_next(self) -> Task:
        while True:
            task = await asyncio.to_thread(self._dequeue_next_sync)
            if task is not None:
                return task
            await asyncio.sleep(self.poll_interval_seconds)

    def _dequeue_next_sync(self) -> Optional[Task]:
        now = utc_now_iso()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM tasks
                WHERE status IN (?, ?)
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (TaskStatus.PENDING.value, TaskStatus.QUEUED.value),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (TaskStatus.RUNNING.value, now, row["id"]),
            )
            conn.commit()
            return self._row_to_task(dict(row), override_status=TaskStatus.RUNNING)

    async def update_task(
        self,
        task_id: str,
        *,
        status: Optional[TaskStatus] = None,
        context: Optional[str] = None,
        result: Optional[str] = None,
        error: Optional[str] = None,
        output_md_path: Optional[str] = None,
        output_json_path: Optional[str] = None,
    ) -> None:
        await asyncio.to_thread(
            self._update_task_sync,
            task_id,
            status,
            context,
            result,
            error,
            output_md_path,
            output_json_path,
        )

    def _update_task_sync(
        self,
        task_id: str,
        status: Optional[TaskStatus],
        context: Optional[str],
        result: Optional[str],
        error: Optional[str],
        output_md_path: Optional[str],
        output_json_path: Optional[str],
    ) -> None:
        now = utc_now_iso()
        updates: dict[str, str] = {"updated_at": now}
        if status is not None:
            updates["status"] = status.value
        if context is not None:
            updates["context"] = context
        if result is not None:
            updates["result"] = result
        if error is not None:
            updates["error"] = error
        if output_md_path is not None:
            updates["output_md_path"] = output_md_path
        if output_json_path is not None:
            updates["output_json_path"] = output_json_path

        if not updates:
            return

        set_clause = ", ".join(f"{key} = ?" for key in updates)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE tasks SET {set_clause} WHERE id = ?",
                (*updates.values(), task_id),
            )
            conn.commit()

    async def get_task(self, task_id: str) -> Optional[Task]:
        return await asyncio.to_thread(self._get_task_sync, task_id)

    def _get_task_sync(self, task_id: str) -> Optional[Task]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                return None
            return self._row_to_task(dict(row))

    async def cancel_task(self, task_id: str) -> bool:
        return await asyncio.to_thread(self._cancel_task_sync, task_id)

    def _cancel_task_sync(self, task_id: str) -> bool:
        now = utc_now_iso()
        with self._connect() as conn:
            result = conn.execute(
                """
                UPDATE tasks
                SET status = ?, updated_at = ?
                WHERE id = ? AND status IN (?, ?)
                """,
                (
                    TaskStatus.CANCELLED.value,
                    now,
                    task_id,
                    TaskStatus.PENDING.value,
                    TaskStatus.QUEUED.value,
                ),
            )
            conn.commit()
            return result.rowcount > 0

    async def list_recent(self, limit: int = 5) -> list[Task]:
        return await asyncio.to_thread(self._list_recent_sync, limit)

    def _list_recent_sync(self, limit: int) -> list[Task]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._row_to_task(dict(row)) for row in rows]

    async def add_session_message(
        self,
        session_key: str,
        role: str,
        content: str,
        task_id: Optional[str] = None,
    ) -> None:
        await asyncio.to_thread(
            self._add_session_message_sync, session_key, role, content, task_id
        )

    def _add_session_message_sync(
        self,
        session_key: str,
        role: str,
        content: str,
        task_id: Optional[str],
    ) -> None:
        if not session_key or not content.strip():
            return
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO session_messages (session_key, role, content, created_at, task_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_key, role, content.strip(), utc_now_iso(), task_id),
            )
            conn.commit()

    async def get_recent_session_messages(
        self, session_key: str, limit: int = 8
    ) -> list[tuple[str, str]]:
        return await asyncio.to_thread(
            self._get_recent_session_messages_sync, session_key, limit
        )

    def _get_recent_session_messages_sync(
        self, session_key: str, limit: int
    ) -> list[tuple[str, str]]:
        if not session_key:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT role, content
                FROM session_messages
                WHERE session_key = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (session_key, limit),
            ).fetchall()
            return [(row["role"], row["content"]) for row in reversed(rows)]

    async def clear_session_messages(self, session_key: str) -> int:
        return await asyncio.to_thread(self._clear_session_messages_sync, session_key)

    def _clear_session_messages_sync(self, session_key: str) -> int:
        if not session_key:
            return 0
        with self._connect() as conn:
            result = conn.execute(
                "DELETE FROM session_messages WHERE session_key = ?",
                (session_key,),
            )
            conn.commit()
            return result.rowcount

    @staticmethod
    def _row_to_task(row: dict, override_status: Optional[TaskStatus] = None) -> Task:
        status = override_status or TaskStatus(row["status"])
        return Task(
            id=row["id"],
            source=row["source"],
            prompt=row["prompt"],
            status=status,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            context=row.get("context") or "",
            result=row.get("result") or "",
            error=row.get("error") or "",
            retry_count=row.get("retry_count") or 0,
            max_retries=row.get("max_retries") or 2,
            telegram_chat_id=row.get("telegram_chat_id"),
            session_key=row.get("session_key"),
            output_md_path=row.get("output_md_path"),
            output_json_path=row.get("output_json_path"),
        )

