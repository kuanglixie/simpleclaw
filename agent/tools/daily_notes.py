"""Daily notes: append-only daily memory logs (OpenClaw-style).

Each day gets a `memory/YYYY-MM-DD.md` file for raw facts, decisions, and
context captured during conversations. These are the "raw journal" layer —
messy on purpose. Nightly consolidation promotes lasting facts to MEMORY.md.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

MEMORY_DIR_NAME = "memory"


def _memory_dir(worklog_dir: str | Path) -> Path:
    d = Path(worklog_dir) / MEMORY_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _today_path(worklog_dir: str | Path) -> Path:
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    return _memory_dir(worklog_dir) / f"{today}.md"


def _yesterday_path(worklog_dir: str | Path) -> Path:
    yesterday = (datetime.now(tz=timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    return _memory_dir(worklog_dir) / f"{yesterday}.md"


def daily_note_append(worklog_dir: str | Path, content: str) -> str:
    """Append content to today's daily note."""
    path = _today_path(worklog_dir)
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d (%A)")
    now = datetime.now(tz=timezone.utc).strftime("%H:%M UTC")

    if not path.exists():
        path.write_text(f"# {today}\n\n")

    with open(path, "a") as f:
        f.write(f"\n## {now}\n\n{content.strip()}\n")

    return f"Appended to {path.name}"


def daily_note_read(worklog_dir: str | Path, date: str = "") -> str:
    """Read a daily note. Empty date = today."""
    if date:
        path = _memory_dir(worklog_dir) / f"{date}.md"
    else:
        path = _today_path(worklog_dir)

    if not path.exists():
        return f"No daily note for {path.stem}."
    return path.read_text(errors="replace")


def load_recent_daily_notes(worklog_dir: str | Path, max_chars: int = 4000) -> str:
    """Load today's + yesterday's daily notes for context injection."""
    parts: list[str] = []
    total = 0

    for path in (_today_path(worklog_dir), _yesterday_path(worklog_dir)):
        if not path.exists():
            continue
        text = path.read_text(errors="replace")
        if total + len(text) > max_chars:
            remaining = max_chars - total
            if remaining > 200:
                text = text[:remaining] + "\n... (truncated)"
            else:
                break
        parts.append(text)
        total += len(text)

    if not parts:
        return ""
    return "\n\n---\n\n".join(parts)


def archive_old_notes(worklog_dir: str | Path, keep_days: int = 30) -> int:
    """Move daily notes older than keep_days to memory/archive/."""
    mem_dir = _memory_dir(worklog_dir)
    archive_dir = mem_dir / "archive"
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=keep_days)
    archived = 0

    for path in sorted(mem_dir.glob("????-??-??.md")):
        try:
            file_date = datetime.strptime(path.stem, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if file_date < cutoff:
            archive_dir.mkdir(parents=True, exist_ok=True)
            path.rename(archive_dir / path.name)
            archived += 1

    return archived
