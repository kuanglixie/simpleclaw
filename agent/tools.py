"""Tool dispatcher for the agent loop.

shell  — general purpose (model already knows bash)
add_todo — the only dedicated tool; handles Inbox formatting + git push atomically
"""

from __future__ import annotations

import time
from pathlib import Path

from .agent_loop import ToolExecutionResult, execute_shell_tool
from .inbox import append_inbox_item
from .output_contract import ToolCallRecord


TOOL_DESCRIPTIONS = (
    "Available tools (use exactly one per step):\n"
    '1. shell — run a bash command\n'
    '   {"type":"shell","command":"<bash command>"}\n'
    '2. add_todo — add an item to the TODO inbox (Cursor picks these up hourly)\n'
    '   {"type":"add_todo","text":"<task description>"}\n'
)


class ToolKit:
    def __init__(self, worklog_dir: Path, shell_timeout: int = 180) -> None:
        self.worklog_dir = worklog_dir
        self.shell_timeout = shell_timeout

    async def run(self, tool_type: str, tool_args: dict) -> ToolExecutionResult:
        if tool_type == "shell":
            return execute_shell_tool(
                tool_args.get("command", ""), self.shell_timeout
            )

        if tool_type == "add_todo":
            return self._add_todo(tool_args)

        return ToolExecutionResult(
            ok=False,
            output=f"Unknown tool type: {tool_type}",
            record=ToolCallRecord(
                name=tool_type, input=str(tool_args),
                output_excerpt=f"Unknown tool type: {tool_type}",
                exit_code=1, duration_ms=0,
            ),
        )

    def _add_todo(self, args: dict) -> ToolExecutionResult:
        text = str(args.get("text", "")).strip()
        if not text:
            return ToolExecutionResult(ok=False, output="add_todo requires 'text'.")
        start = time.time()
        todo_path = self.worklog_dir / "TODO.md"
        result = append_inbox_item(todo_path, text)
        elapsed = int((time.time() - start) * 1000)
        return ToolExecutionResult(
            ok=True, output=result,
            record=ToolCallRecord(
                name="add_todo", input=text,
                output_excerpt=result[:500], exit_code=0, duration_ms=elapsed,
            ),
        )
