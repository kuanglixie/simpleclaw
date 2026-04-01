from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path

from .models import Task


def _tail_lines(path: Path, limit: int) -> str:
    if not path.exists():
        return ""
    lines = path.read_text(errors="replace").splitlines()
    return "\n".join(lines[-limit:])


def _is_ray_prompt(prompt: str) -> bool:
    lowered = prompt.lower()
    keywords = ("ray", "job", "cluster", "training", "pipeline", "eval")
    return any(keyword in lowered for keyword in keywords)


def _is_todo_prompt(prompt: str) -> bool:
    lowered = prompt.lower()
    if any(kw in lowered for kw in ("todo", "task list", "backlog", "checklist")):
        return True
    if re.search(r"(?:task|item)\s*#?\s*\d+", lowered):
        return True
    return False


def _truncate_text(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 3] + "..."


def read_todo_dashboard(path: Path) -> str:
    """Read TODO.md header and dashboard table (everything before ## Details)."""
    if not path.exists():
        return ""
    lines = path.read_text(errors="replace").splitlines()
    end = len(lines)
    for i, line in enumerate(lines):
        if line.strip().startswith("## Details"):
            end = i
            break
    return "\n".join(lines[:end]).strip()


def read_todo_detail(path: Path, task_num: str) -> str:
    """Read a specific task's detail section (### #N — ... through next heading)."""
    if not path.exists():
        return ""
    text = path.read_text(errors="replace")
    lines = text.splitlines()

    collecting = False
    result_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("### "):
            if collecting:
                break
            before_dash = stripped.split("—")[0] if "—" in stripped else stripped
            if re.search(rf"#\s*{re.escape(task_num)}(?!\d)", before_dash):
                collecting = True
                result_lines = [line]
            continue
        if stripped.startswith("## ") and collecting:
            break
        if collecting:
            result_lines.append(line)

    return "\n".join(result_lines).strip()


def _extract_task_nums(prompt: str) -> list[str]:
    """Extract task numbers referenced in prompt (e.g., '#8', 'task 20')."""
    nums = re.findall(r"#(\d+)", prompt)
    nums.extend(re.findall(r"(?:task|item)\s*#?\s*(\d+)", prompt, re.IGNORECASE))
    return list(dict.fromkeys(nums))


def format_todo_for_telegram(path: Path) -> str:
    """Parse TODO.md dashboard table into a compact Telegram-friendly format."""
    if not path.exists():
        return "TODO.md not found."
    lines = path.read_text(errors="replace").splitlines()

    in_table = False
    rows: list[list[str]] = []
    for line in lines:
        stripped = line.strip()
        if "## Dashboard" in stripped:
            in_table = True
            continue
        if stripped.startswith("## ") and in_table:
            break
        if in_table and stripped.startswith("|") and not stripped.startswith("|---"):
            cells = [c.strip() for c in stripped.split("|")[1:-1]]
            if len(cells) >= 2 and cells[0].strip() != "#":
                rows.append(cells)

    if not rows:
        return "No TODO items in dashboard."

    priority_groups: dict[str, list[str]] = {}
    for cells in rows:
        num = cells[0].strip() if len(cells) > 0 else "?"
        task_name = cells[1].strip() if len(cells) > 1 else ""
        status = cells[2].strip().replace("**", "") if len(cells) > 2 else ""
        priority = cells[3].strip().replace("**", "") if len(cells) > 3 else ""
        due = cells[4].strip() if len(cells) > 4 else ""

        prio_key = priority.upper() or "OTHER"
        if prio_key not in priority_groups:
            priority_groups[prio_key] = []

        entry = f"  #{num} {task_name}"
        if status and status != "—":
            entry += f"\n      {status}"
        if due and due != "—":
            entry += f" [due {due}]"
        priority_groups[prio_key].append(entry)

    prio_order = ["CRITICAL", "URGENT", "HIGH", "MEDIUM", "LOW", "OTHER"]
    parts = [f"TODO ({len(rows)} open)\n"]
    for prio in prio_order:
        if prio in priority_groups:
            parts.append(f"[{prio}]")
            parts.extend(priority_groups[prio])
            parts.append("")

    return "\n".join(parts).strip()


def _read_file_safe(path: Path, max_chars: int = 0) -> str:
    """Read a file, returning empty string if missing or unreadable."""
    if not path.exists():
        return ""
    text = path.read_text(errors="replace")
    if max_chars and len(text) > max_chars:
        return text[:max_chars] + "\n... (truncated)"
    return text


class ContextAssembler:
    def __init__(self, worklog_dir: Path, data_dir: Path) -> None:
        self.worklog_dir = worklog_dir
        self.data_dir = data_dir

    async def build(
        self, task: Task, recent_messages: list[tuple[str, str]] | None = None
    ) -> str:
        return await asyncio.to_thread(self._build_sync, task, recent_messages or [])

    def _build_sync(
        self, task: Task, recent_messages: list[tuple[str, str]]
    ) -> str:
        now = datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat()
        ray_prompt = _is_ray_prompt(task.prompt)
        todo_prompt = _is_todo_prompt(task.prompt)
        todo_path = self.worklog_dir / "TODO.md"
        dailylog_tail = _tail_lines(self.worklog_dir / "dailylog.md", 40)

        soul_text = _read_file_safe(self.worklog_dir / "SOUL.md", max_chars=4000)
        memory_text = _read_file_safe(self.worklog_dir / "MEMORY.md", max_chars=12000)

        latest_monitor = ""
        if self.data_dir.exists():
            pattern = "ray-status-*.md" if ray_prompt else "*.md"
            report_files = sorted(
                [p for p in self.data_dir.glob(pattern) if p.is_file()],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if report_files:
                latest_monitor = _truncate_text(_tail_lines(report_files[0], 20), 1800)

        parts = [
            f"Current UTC time: {now}",
            f"Task source: {task.source}",
            f"Session key: {task.session_key or 'n/a'}",
        ]

        if soul_text:
            parts.extend(["", "--- SOUL (identity) ---", soul_text])
        if memory_text:
            parts.extend(["", "--- MEMORY (long-term knowledge) ---", memory_text])

        if recent_messages:
            parts.extend(["", "Recent conversation (most recent last):"])
            n = len(recent_messages)
            for i, (role, content) in enumerate(recent_messages):
                label = "User" if role.lower() == "user" else "Assistant"
                is_recent = i >= n - 6
                limit = 1200 if is_recent else 400
                parts.append(f"- {label}: {_truncate_text(content, limit)}")
            parts.append(
                "\nIMPORTANT: The user's new message may be a SHORT follow-up "
                "(e.g. 'yes', 'do it', 'fix this', 'try again'). In that case "
                "you MUST resolve what they mean from the conversation above. "
                "Specifically: if you previously offered to do something and the "
                "user says 'yes' or 'go ahead', EXECUTE that action now."
            )

        snapshot_text = _read_file_safe(
            self.worklog_dir / "STATUS_SNAPSHOT.md", max_chars=3000,
        )
        if snapshot_text:
            parts.extend([
                "",
                "--- STATUS SNAPSHOT (auto-generated, see timestamp for freshness) ---",
                snapshot_text,
            ])

        dashboard = read_todo_dashboard(todo_path)

        if ray_prompt:
            parts.extend(
                [
                    "",
                    "Ray-focused context:",
                    latest_monitor or "(no recent ray monitor report found)",
                    "",
                    "TODO Dashboard:",
                    _truncate_text(dashboard, 4000) if dashboard else "(empty)",
                ]
            )
        elif todo_prompt:
            parts.extend(
                [
                    "",
                    f"TODO file: {todo_path}",
                    "",
                    "TODO Dashboard:",
                    dashboard or "(empty)",
                ]
            )
            task_nums = _extract_task_nums(task.prompt)
            for num in task_nums[:3]:
                detail = read_todo_detail(todo_path, num)
                if detail:
                    parts.extend(["", f"TODO Detail (#{num}):", detail])
            parts.extend(
                [
                    "",
                    "To update TODO items, edit the markdown file directly.",
                    "The file has a dashboard table and ### detail sections.",
                ]
            )
        else:
            parts.extend(
                [
                    "",
                    "Recent dailylog tail:",
                    dailylog_tail or "(empty)",
                    "",
                    "TODO Dashboard:",
                    _truncate_text(dashboard, 4000) if dashboard else "(empty)",
                ]
            )
            if latest_monitor:
                parts.extend(
                    [
                        "",
                        "Latest monitor report excerpt:",
                        latest_monitor,
                    ]
                )

        return "\n".join(parts).strip()

