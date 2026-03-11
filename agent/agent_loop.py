from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional

from .output_contract import ToolCallRecord


@dataclass
class LoopAction:
    status: str
    final_answer: str = ""
    summary: str = ""
    next_action: str = ""
    tool_type: str = ""
    tool_args: dict = field(default_factory=dict)


@dataclass
class ToolExecutionResult:
    ok: bool
    output: str
    record: Optional[ToolCallRecord] = None


def extract_action(raw_text: str, allow_plain_text: bool = False) -> Optional[LoopAction]:
    payload = _extract_json_obj(raw_text)
    if payload is not None:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            pass
        else:
            status = str(data.get("status", "")).strip().lower()
            if status in {"continue", "done"}:
                tool = data.get("tool") if isinstance(data.get("tool"), dict) else {}
                tool_type = ""
                tool_args: dict = {}
                if isinstance(tool, dict):
                    tool_type = str(tool.get("type", "")).strip()
                    tool_args = {k: v for k, v in tool.items() if k != "type"}
                return LoopAction(
                    status=status,
                    final_answer=str(data.get("final_answer", "")).strip(),
                    summary=str(data.get("summary", "")).strip(),
                    next_action=str(data.get("next_action", "")).strip(),
                    tool_type=tool_type,
                    tool_args=tool_args,
                )

    if allow_plain_text:
        text = raw_text.strip()
        if len(text) > 20 and not text.startswith("{"):
            first_line = text.split("\n")[0].strip()
            return LoopAction(
                status="done",
                final_answer=text,
                summary=first_line[:300],
            )

    return None


def _extract_json_obj(text: str) -> Optional[str]:
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped

    fence_match = re.search(r"```json\s*(\{[\s\S]*?\})\s*```", text, re.IGNORECASE)
    if fence_match:
        return fence_match.group(1)

    generic_fence = re.search(r"```\s*(\{[\s\S]*?\})\s*```", text)
    if generic_fence:
        return generic_fence.group(1)
    return None


_SHELL_DENYLIST = (
    "rm -rf",
    "git reset --hard",
    "git checkout --",
    "shutdown",
    "reboot",
    ":(){",
)


def execute_shell_tool(command: str, timeout_seconds: int = 180) -> ToolExecutionResult:
    cleaned = command.strip()
    if not cleaned:
        return ToolExecutionResult(ok=False, output="Empty tool command.")
    lowered = cleaned.lower()
    if any(bad in lowered for bad in _SHELL_DENYLIST):
        return ToolExecutionResult(ok=False, output=f"Blocked unsafe command: {cleaned}")

    start = time.time()
    proc = subprocess.run(
        ["bash", "-lc", cleaned],
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    elapsed = int((time.time() - start) * 1000)
    combined = (proc.stdout or "").strip()
    if proc.stderr:
        if combined:
            combined += "\n"
        combined += proc.stderr.strip()
    output_excerpt = combined[:1500]
    record = ToolCallRecord(
        name="shell",
        input=cleaned,
        output_excerpt=output_excerpt,
        exit_code=proc.returncode,
        duration_ms=elapsed,
    )
    return ToolExecutionResult(ok=proc.returncode == 0, output=combined, record=record)
