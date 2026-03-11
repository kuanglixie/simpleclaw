from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from uuid import uuid4


class TaskStatus(str, Enum):
    QUEUED = "queued"
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Task:
    id: str
    source: str
    prompt: str
    status: TaskStatus = TaskStatus.PENDING
    created_at: str = ""
    updated_at: str = ""
    context: str = ""
    result: str = ""
    error: str = ""
    retry_count: int = 0
    max_retries: int = 2
    telegram_chat_id: Optional[str] = None
    session_key: Optional[str] = None
    output_md_path: Optional[str] = None
    output_json_path: Optional[str] = None


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat()


def new_task_id() -> str:
    return str(uuid4())

