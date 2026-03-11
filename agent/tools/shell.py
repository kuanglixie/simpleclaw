"""Shell command execution with safety denylist."""

from __future__ import annotations

import subprocess
import time

from ..output_contract import ToolCallRecord


_SHELL_DENYLIST = (
    "rm -rf /",
    "rm -rf ~",
    "git reset --hard",
    "git checkout -- .",
    "shutdown",
    "reboot",
    ":(){",
    "mkfs",
    "dd if=",
)


def execute_shell(command: str, timeout_seconds: int = 180) -> dict:
    cleaned = command.strip()
    if not cleaned:
        return {"success": False, "output": "Empty command."}

    lowered = cleaned.lower()
    if any(bad in lowered for bad in _SHELL_DENYLIST):
        return {"success": False, "output": f"Blocked unsafe command: {cleaned}"}

    start = time.time()
    try:
        proc = subprocess.run(
            ["bash", "-lc", cleaned],
            capture_output=True, text=True,
            timeout=timeout_seconds, check=False,
        )
    except subprocess.TimeoutExpired:
        elapsed = int((time.time() - start) * 1000)
        return {
            "success": False,
            "output": f"Command timed out after {timeout_seconds}s",
            "exit_code": 124,
            "duration_ms": elapsed,
        }

    elapsed = int((time.time() - start) * 1000)
    combined = (proc.stdout or "").strip()
    if proc.stderr:
        if combined:
            combined += "\n"
        combined += proc.stderr.strip()

    return {
        "success": proc.returncode == 0,
        "output": combined or "(no output)",
        "exit_code": proc.returncode,
        "duration_ms": elapsed,
    }


def make_tool_record(command: str, result: dict) -> ToolCallRecord:
    return ToolCallRecord(
        name="shell",
        input=command,
        output_excerpt=(result.get("output", ""))[:1500],
        exit_code=result.get("exit_code", 1 if not result.get("success") else 0),
        duration_ms=result.get("duration_ms", 0),
    )
