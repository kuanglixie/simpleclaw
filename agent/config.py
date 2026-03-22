from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'").strip('"')
    return values


def _env(env_values: dict[str, str], key: str, default: str) -> str:
    return os.getenv(key, env_values.get(key, default))


@dataclass(frozen=True)
class Settings:
    worklog_dir: Path
    mailbox_dir: Path
    pending_dir: Path
    outbox_dir: Path
    outbox_events_dir: Path
    archive_dir: Path
    data_dir: Path
    cron_dir: Path
    db_path: Path
    gemini_model: str
    gemini_timeout_seconds: int
    gemini_use_vertexai: bool
    gemini_vertex_project: str
    gemini_vertex_location: str
    pydantic_ai_model: str
    max_agent_steps: int
    tool_timeout_seconds: int
    monitor_interval_seconds: int
    queue_poll_interval_seconds: float
    telegram_bot_token: str
    telegram_allowed_user_id: int
    # Heartbeat
    heartbeat_enabled: bool
    heartbeat_every_seconds: int
    heartbeat_active_hours_start: str
    heartbeat_active_hours_end: str
    heartbeat_active_hours_tz: str
    heartbeat_light_context: bool
    # Cron
    cron_enabled: bool
    # Compaction
    compaction_enabled: bool
    compaction_max_session_messages: int
    compaction_keep_recent: int
    compaction_max_context_chars: int
    # Browser extension relay
    browser_server_enabled: bool
    browser_server_host: str
    browser_server_port: int
    browser_server_token: str


def load_settings() -> Settings:
    config_path = Path(
        os.getenv(
            "WORKLOG_AGENT_CONFIG",
            str(Path.home() / ".config/worklog-agent/config.env"),
        )
    )
    file_env = _read_env_file(config_path)

    worklog_dir = Path(_env(file_env, "WORKLOG_DIR", str(Path.home() / "worklog")))
    mailbox_dir = worklog_dir / ".agent_mailbox"

    token = _env(file_env, "TELEGRAM_BOT_TOKEN", "")
    allowed_user_raw = _env(file_env, "TELEGRAM_ALLOWED_USER_ID", "0")
    try:
        allowed_user = int(allowed_user_raw)
    except ValueError:
        allowed_user = 0

    monitor_interval_raw = _env(file_env, "MONITOR_INTERVAL_SECONDS", "1800")
    queue_poll_raw = _env(file_env, "QUEUE_POLL_INTERVAL_SECONDS", "2")
    timeout_raw = _env(file_env, "GEMINI_TIMEOUT_SECONDS", "300")
    max_steps_raw = _env(file_env, "MAX_AGENT_STEPS", "20")
    tool_timeout_raw = _env(file_env, "TOOL_TIMEOUT_SECONDS", "180")
    try:
        monitor_interval = int(monitor_interval_raw)
    except ValueError:
        monitor_interval = 1800
    try:
        queue_poll = float(queue_poll_raw)
    except ValueError:
        queue_poll = 2.0
    try:
        timeout = int(timeout_raw)
    except ValueError:
        timeout = 300
    try:
        max_steps = int(max_steps_raw)
    except ValueError:
        max_steps = 6
    try:
        tool_timeout = int(tool_timeout_raw)
    except ValueError:
        tool_timeout = 180

    use_vertex_raw = _env(file_env, "GOOGLE_GENAI_USE_VERTEXAI", "").lower()
    use_vertexai = use_vertex_raw in ("true", "1", "yes")

    heartbeat_enabled = _env(file_env, "HEARTBEAT_ENABLED", "true").lower() in ("true", "1", "yes")
    heartbeat_every = _safe_int(_env(file_env, "HEARTBEAT_EVERY_SECONDS", "1800"), 1800)
    heartbeat_light = _env(file_env, "HEARTBEAT_LIGHT_CONTEXT", "true").lower() in ("true", "1", "yes")
    cron_enabled = _env(file_env, "CRON_ENABLED", "true").lower() in ("true", "1", "yes")
    compaction_enabled = _env(file_env, "COMPACTION_ENABLED", "true").lower() in ("true", "1", "yes")
    compaction_max_msgs = _safe_int(_env(file_env, "COMPACTION_MAX_SESSION_MESSAGES", "40"), 40)
    compaction_keep = _safe_int(_env(file_env, "COMPACTION_KEEP_RECENT", "12"), 12)
    compaction_max_chars = _safe_int(_env(file_env, "COMPACTION_MAX_CONTEXT_CHARS", "80000"), 80000)

    browser_enabled = _env(file_env, "BROWSER_SERVER_ENABLED", "false").lower() in ("true", "1", "yes")
    browser_host = _env(file_env, "BROWSER_SERVER_HOST", "127.0.0.1")
    browser_port = _safe_int(_env(file_env, "BROWSER_SERVER_PORT", "18790"), 18790)
    browser_token = _env(file_env, "BROWSER_SERVER_TOKEN", "")

    gemini_model = _env(file_env, "GEMINI_MODEL", "gemini-3.1-pro")
    prefix = "google-vertex" if use_vertexai else "google-gla"
    pai_model = _env(file_env, "PYDANTIC_AI_MODEL", f"{prefix}:{gemini_model}")

    return Settings(
        worklog_dir=worklog_dir,
        mailbox_dir=mailbox_dir,
        pending_dir=mailbox_dir / "pending",
        outbox_dir=mailbox_dir / "outbox",
        outbox_events_dir=mailbox_dir / "outbox/events",
        archive_dir=mailbox_dir / "archive",
        data_dir=mailbox_dir / "data",
        cron_dir=mailbox_dir / "cron",
        db_path=mailbox_dir / "state.db",
        gemini_model=gemini_model,
        gemini_timeout_seconds=timeout,
        gemini_use_vertexai=use_vertexai,
        gemini_vertex_project=_env(file_env, "GOOGLE_CLOUD_PROJECT", ""),
        gemini_vertex_location=_env(file_env, "GOOGLE_CLOUD_LOCATION", "us-central1"),
        pydantic_ai_model=pai_model,
        max_agent_steps=max(1, max_steps),
        tool_timeout_seconds=max(10, tool_timeout),
        monitor_interval_seconds=monitor_interval,
        queue_poll_interval_seconds=queue_poll,
        telegram_bot_token=token,
        telegram_allowed_user_id=allowed_user,
        heartbeat_enabled=heartbeat_enabled,
        heartbeat_every_seconds=heartbeat_every,
        heartbeat_active_hours_start=_env(file_env, "HEARTBEAT_ACTIVE_HOURS_START", "08:00"),
        heartbeat_active_hours_end=_env(file_env, "HEARTBEAT_ACTIVE_HOURS_END", "23:00"),
        heartbeat_active_hours_tz=_env(file_env, "HEARTBEAT_ACTIVE_HOURS_TZ", ""),
        heartbeat_light_context=heartbeat_light,
        cron_enabled=cron_enabled,
        compaction_enabled=compaction_enabled,
        compaction_max_session_messages=compaction_max_msgs,
        compaction_keep_recent=compaction_keep,
        compaction_max_context_chars=compaction_max_chars,
        browser_server_enabled=browser_enabled,
        browser_server_host=browser_host,
        browser_server_port=browser_port,
        browser_server_token=browser_token,
    )


def _safe_int(value: str, default: int) -> int:
    try:
        return int(value)
    except ValueError:
        return default

