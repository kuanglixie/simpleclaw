"""Status snapshot: generates a structured STATUS_SNAPSHOT.md.

Called by the heartbeat loop to maintain a machine-generated, timestamped
summary of current project state. This file is injected into every
ContextAssembler build so the model always has ground truth about what's
happening, preventing hallucinated status claims.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from .context import read_todo_dashboard


def _extract_active_jobs_from_todo(dashboard: str) -> list[str]:
    """Pull task lines whose status suggests active/blocked work."""
    active: list[str] = []
    for line in dashboard.splitlines():
        if not line.strip().startswith("|"):
            continue
        lowered = line.lower()
        if any(kw in lowered for kw in ("in progress", "running", "failed", "blocked", "needs")):
            cells = [c.strip() for c in line.split("|")[1:-1]]
            if len(cells) >= 3 and cells[0].strip().replace("~", "").isdigit():
                num = cells[0].strip().replace("~", "")
                name = cells[1].strip()[:80]
                status_raw = cells[2].strip()
                status_short = re.sub(r"\*\*", "", status_raw)[:120]
                active.append(f"- #{num} {name}: {status_short}")
    return active


def _extract_done_items(dashboard: str) -> list[str]:
    """Pull recently completed task lines."""
    done: list[str] = []
    for line in dashboard.splitlines():
        if not line.strip().startswith("|"):
            continue
        lowered = line.lower()
        if "done" in lowered or "complete" in lowered:
            cells = [c.strip() for c in line.split("|")[1:-1]]
            if len(cells) >= 3 and cells[0].strip().replace("~", "").isdigit():
                num = cells[0].strip().replace("~", "")
                name = cells[1].strip()[:80]
                done.append(f"- #{num} {name}")
    return done


def _extract_high_priority(dashboard: str) -> list[str]:
    """Pull high-priority items."""
    high: list[str] = []
    for line in dashboard.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.split("|")[1:-1]]
        if len(cells) >= 4:
            priority = cells[3].strip().lower().replace("*", "")
            if priority in ("high", "critical", "urgent"):
                num = cells[0].strip().replace("~", "")
                if num.isdigit():
                    name = cells[1].strip()[:80]
                    status_raw = re.sub(r"\*\*", "", cells[2].strip())[:100]
                    high.append(f"- #{num} {name}: {status_raw}")
    return high


def _read_recent_dailylog(worklog_dir: Path, lines: int = 30) -> str:
    """Read the most recent dailylog entry header."""
    path = worklog_dir / "dailylog.md"
    if not path.exists():
        return ""
    all_lines = path.read_text(errors="replace").splitlines()
    tail = all_lines[-lines:] if len(all_lines) > lines else all_lines

    for i, line in enumerate(tail):
        if line.startswith("## ") or line.startswith("### "):
            return "\n".join(tail[i:])
    return "\n".join(tail)


def generate_snapshot(worklog_dir: Path) -> str:
    """Generate the STATUS_SNAPSHOT.md content."""
    now = datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat()
    todo_path = worklog_dir / "TODO.md"

    dashboard = read_todo_dashboard(todo_path)
    active_items = _extract_active_jobs_from_todo(dashboard)
    done_items = _extract_done_items(dashboard)
    high_priority = _extract_high_priority(dashboard)
    recent_log = _read_recent_dailylog(worklog_dir, lines=20)

    parts = [
        f"# Status Snapshot",
        f"Auto-generated: {now}",
        "",
    ]

    if high_priority:
        parts.append("## High Priority")
        parts.extend(high_priority)
        parts.append("")

    if active_items:
        parts.append("## Active / Blocked")
        parts.extend(active_items)
        parts.append("")

    if done_items:
        parts.append("## Recently Completed")
        parts.extend(done_items)
        parts.append("")

    if recent_log:
        parts.append("## Latest Dailylog")
        parts.append(recent_log[:800])
        parts.append("")

    if not (active_items or done_items or high_priority):
        parts.append("(no items extracted from TODO dashboard)")
        parts.append("")

    return "\n".join(parts)


def write_snapshot(worklog_dir: Path) -> Path:
    """Generate and write STATUS_SNAPSHOT.md. Returns the path."""
    content = generate_snapshot(worklog_dir)
    path = worklog_dir / "STATUS_SNAPSHOT.md"
    path.write_text(content)
    return path
