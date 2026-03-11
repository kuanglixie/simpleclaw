"""Append items to the ## Inbox section of TODO.md and commit/push."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

_INBOX_HEADER = "## Inbox"
_DASHBOARD_HEADER = "## Dashboard"


def append_inbox_item(todo_path: Path, text: str) -> str:
    """Append a bullet to ## Inbox in TODO.md.  Creates the section if absent.

    Returns a user-facing confirmation or error string.
    """
    if not todo_path.exists():
        return "TODO.md not found."

    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    bullet = f"- [ ] {text} *(added via Telegram, {ts})*"

    lines = todo_path.read_text(errors="replace").splitlines()
    inbox_idx = _find_section(lines, _INBOX_HEADER)

    if inbox_idx is not None:
        insert_at = _section_end(lines, inbox_idx)
        lines.insert(insert_at, bullet)
    else:
        dashboard_idx = _find_section(lines, _DASHBOARD_HEADER)
        insert_at = dashboard_idx if dashboard_idx is not None else len(lines)
        lines.insert(insert_at, "")
        lines.insert(insert_at + 1, _INBOX_HEADER)
        lines.insert(insert_at + 2, "")
        lines.insert(insert_at + 3, bullet)
        lines.insert(insert_at + 4, "")

    todo_path.write_text("\n".join(lines) + "\n")

    ok = _git_commit_push(todo_path)
    short = text[:80] + ("..." if len(text) > 80 else "")
    if ok:
        return f"Added to Inbox: {short}\n(committed + pushed)"
    return f"Added to Inbox: {short}\n(written locally, git push failed — Cursor will still pick it up)"


def _find_section(lines: list[str], header: str) -> int | None:
    normalized = header.lower()
    for i, line in enumerate(lines):
        if line.strip().lower().startswith(normalized):
            return i
    return None


def _section_end(lines: list[str], header_idx: int) -> int:
    """Return the index of the first blank line or next ## heading after header_idx."""
    for i in range(header_idx + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped.startswith("## "):
            return i
    return len(lines)


def _git_commit_push(todo_path: Path) -> bool:
    repo = todo_path.parent
    try:
        subprocess.run(
            ["git", "add", str(todo_path.name)],
            cwd=repo, capture_output=True, check=True, timeout=10,
        )
        subprocess.run(
            ["git", "commit", "-m", "inbox: add TODO via Telegram"],
            cwd=repo, capture_output=True, check=True, timeout=10,
        )
        subprocess.run(
            ["git", "push"],
            cwd=repo, capture_output=True, check=True, timeout=30,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
