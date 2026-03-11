"""Heartbeat: periodic agent check-ins inspired by OpenClaw.

Runs a lightweight agent turn on a configurable interval. The model reads
HEARTBEAT.md and either surfaces urgent items or replies HEARTBEAT_OK.
HEARTBEAT_OK responses are silently dropped to avoid notification spam.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


HEARTBEAT_TOKEN = "HEARTBEAT_OK"

DEFAULT_PROMPT = (
    "Read HEARTBEAT.md if it exists (workspace context). Follow it strictly. "
    "Do not infer or repeat old tasks from prior chats. "
    "If nothing needs attention, reply HEARTBEAT_OK."
)


@dataclass
class ActiveHours:
    start: str = "08:00"
    end: str = "23:00"
    timezone: str = ""  # empty = local


@dataclass
class HeartbeatConfig:
    enabled: bool = True
    every_seconds: int = 1800  # 30m
    prompt: str = DEFAULT_PROMPT
    light_context: bool = True
    active_hours: Optional[ActiveHours] = None
    ack_max_chars: int = 300


def is_within_active_hours(config: HeartbeatConfig) -> bool:
    if config.active_hours is None:
        return True

    now = datetime.now(tz=timezone.utc)
    if config.active_hours.timezone:
        try:
            import zoneinfo
            tz = zoneinfo.ZoneInfo(config.active_hours.timezone)
            now = datetime.now(tz=tz)
        except Exception:
            pass
    else:
        now = datetime.now()

    current_time = now.strftime("%H:%M")
    start = config.active_hours.start
    end = config.active_hours.end

    if end == "24:00":
        end = "23:59"

    if start <= end:
        return start <= current_time <= end
    # wraps midnight
    return current_time >= start or current_time <= end


def read_heartbeat_md(worklog_dir: Path) -> str:
    hb_path = worklog_dir / "HEARTBEAT.md"
    if not hb_path.exists():
        return ""
    text = hb_path.read_text(errors="replace").strip()
    # If the file is effectively empty (only blank lines and headers), skip
    stripped = re.sub(r"^#+\s.*$", "", text, flags=re.MULTILINE).strip()
    if not stripped:
        return ""
    return text


def is_heartbeat_ok(response: str, ack_max_chars: int = 300) -> bool:
    """Check if a response is a quiet HEARTBEAT_OK acknowledgment."""
    text = response.strip()
    if text == HEARTBEAT_TOKEN:
        return True
    # Token at start or end with minimal surrounding text
    if text.startswith(HEARTBEAT_TOKEN):
        remaining = text[len(HEARTBEAT_TOKEN):].strip()
        return len(remaining) <= ack_max_chars
    if text.endswith(HEARTBEAT_TOKEN):
        remaining = text[:-len(HEARTBEAT_TOKEN)].strip()
        return len(remaining) <= ack_max_chars
    return False


def strip_heartbeat_token(response: str) -> str:
    text = response.strip()
    if text.startswith(HEARTBEAT_TOKEN):
        text = text[len(HEARTBEAT_TOKEN):].strip()
    if text.endswith(HEARTBEAT_TOKEN):
        text = text[:-len(HEARTBEAT_TOKEN)].strip()
    return text
