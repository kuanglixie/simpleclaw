"""TODO management tools."""

from __future__ import annotations

import re
from pathlib import Path

from ..inbox import append_inbox_item


def add_todo(text: str, worklog_dir: str) -> str:
    if not text.strip():
        return "Error: empty todo text."
    todo_path = Path(worklog_dir) / "TODO.md"
    return append_inbox_item(todo_path, text.strip())


def read_todo(worklog_dir: str, task_number: str = "") -> str:
    todo_path = Path(worklog_dir) / "TODO.md"
    if not todo_path.exists():
        return "TODO.md not found."

    text = todo_path.read_text(errors="replace")

    if task_number:
        return _read_task_detail(text, task_number)
    return _read_dashboard(text)


def _read_dashboard(text: str) -> str:
    lines = text.splitlines()
    end = len(lines)
    for i, line in enumerate(lines):
        if line.strip().startswith("## Details"):
            end = i
            break
    return "\n".join(lines[:end]).strip() or "(no dashboard found)"


def _read_task_detail(text: str, task_num: str) -> str:
    lines = text.splitlines()
    collecting = False
    result: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("### "):
            if collecting:
                break
            before_dash = stripped.split("—")[0] if "—" in stripped else stripped
            if re.search(rf"#\s*{re.escape(task_num)}(?!\d)", before_dash):
                collecting = True
                result = [line]
            continue
        if stripped.startswith("## ") and collecting:
            break
        if collecting:
            result.append(line)
    return "\n".join(result).strip() or f"Task #{task_num} not found."
