"""Snoocode MCP bridge — call snoocode_tools from SimpleClaw.

Snoocode runs as a local HTTP MCP server at 127.0.0.1:3846 and provides
dev workflow tools: GitHub PRs, Drone CI, ticket management, CI watching,
phased workflow orchestration, and Cursor agent delegation.

Uses MCP Streamable HTTP transport: session init + SSE responses.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from typing import Any

SNOOCODE_URL = "http://127.0.0.1:3846/mcp"
_TIMEOUT = 120


def _http_timeout_seconds(env_name: str, default: int, min_s: int, max_s: int) -> int:
    """Parse env for HTTP max-time; clamp to [min_s, max_s]."""
    try:
        v = int(os.getenv(env_name, str(default)))
    except ValueError:
        v = default
    return max(min_s, min(max_s, v))


# run_agent can run many tool steps (heartbeat: kubectl, gazette, PRs). The default
# 120s curl limit caused false "cursor_agent timed out" when the IDE agent was still working.
# Default 3600s (1h); override with SNOOCODE_RUN_AGENT_TIMEOUT (clamped 120–7200).
RUN_AGENT_HTTP_TIMEOUT = _http_timeout_seconds(
    "SNOOCODE_RUN_AGENT_TIMEOUT", default=3600, min_s=120, max_s=7200,
)
_ACCEPT = "application/json, text/event-stream"

_lock = threading.Lock()
_session_id: str | None = None
_request_id = 0


def _next_id() -> int:
    global _request_id
    _request_id += 1
    return _request_id


def _curl_post(
    payload: dict,
    session_id: str | None = None,
    timeout: int = _TIMEOUT,
    include_headers: bool = False,
) -> tuple[str, dict[str, str]]:
    """POST JSON to snoocode, return (body, response_headers)."""
    cmd = [
        "curl", "-s", "-X", "POST", SNOOCODE_URL,
        "-H", "Content-Type: application/json",
        "-H", f"Accept: {_ACCEPT}",
        "--max-time", str(timeout),
        "-d", json.dumps(payload),
    ]
    if session_id:
        cmd.extend(["-H", f"mcp-session-id: {session_id}"])
    if include_headers:
        cmd.append("-D-")

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout + 10, check=False,
        )
    except subprocess.TimeoutExpired:
        return "", {}

    if proc.returncode != 0:
        return "", {}

    output = proc.stdout
    headers: dict[str, str] = {}

    if include_headers and "\r\n\r\n" in output:
        raw_headers, body = output.split("\r\n\r\n", 1)
        for line in raw_headers.splitlines():
            if ":" in line and not line.startswith("HTTP/"):
                key, val = line.split(":", 1)
                headers[key.strip().lower()] = val.strip()
        return body, headers

    return output, headers


def _parse_sse_result(body: str) -> dict | None:
    """Extract JSON-RPC result from an SSE event stream."""
    for line in body.splitlines():
        if line.startswith("data: "):
            data_str = line[6:].strip()
            if not data_str:
                continue
            try:
                return json.loads(data_str)
            except json.JSONDecodeError:
                continue
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def _ensure_session() -> str | None:
    """Initialize an MCP session if we don't have one."""
    global _session_id
    if _session_id is not None:
        return _session_id

    with _lock:
        if _session_id is not None:
            return _session_id

        init_payload = {
            "jsonrpc": "2.0",
            "id": _next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "simpleclaw", "version": "1.0"},
            },
        }
        body, headers = _curl_post(init_payload, include_headers=True, timeout=15)
        sid = headers.get("mcp-session-id")
        if not sid:
            sid_match = re.search(r"mcp-session-id:\s*(\S+)", body, re.IGNORECASE)
            if sid_match:
                sid = sid_match.group(1)
        if not sid:
            return None

        _session_id = sid

        notify_payload = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        }
        _curl_post(notify_payload, session_id=sid, timeout=5)

        return _session_id


def _reset_session() -> None:
    global _session_id
    with _lock:
        _session_id = None


def call_tool(
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    *,
    timeout: int | None = None,
) -> str:
    """Call any snoocode MCP tool via HTTP JSON-RPC with session management.

    Args:
        tool_name: MCP tool name.
        arguments: Tool arguments.
        timeout: Seconds for curl --max-time (default 120). Use
            RUN_AGENT_HTTP_TIMEOUT for long-running tools like run_agent.
    """
    sid = _ensure_session()
    if not sid:
        return f"Error: failed to establish MCP session with snoocode"

    payload = {
        "jsonrpc": "2.0",
        "id": _next_id(),
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments or {},
        },
    }

    t = timeout if timeout is not None else _TIMEOUT
    body, _ = _curl_post(payload, session_id=sid, timeout=t)
    if not body:
        _reset_session()
        return f"Error: empty response from snoocode for {tool_name}"

    resp = _parse_sse_result(body)
    if resp is None:
        _reset_session()
        return f"Error: unparseable response from snoocode: {body[:500]}"

    if "error" in resp:
        err = resp["error"]
        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        if "session" in msg.lower():
            _reset_session()
        return f"Error: {msg}"

    result = resp.get("result", {})
    is_error = result.get("isError", False) if isinstance(result, dict) else False
    content_parts = result.get("content", []) if isinstance(result, dict) else []
    if content_parts:
        texts = []
        for part in content_parts:
            if isinstance(part, dict) and part.get("text"):
                texts.append(part["text"])
            elif isinstance(part, str):
                texts.append(part)
        if texts:
            combined = "\n".join(texts)
            if is_error:
                return f"Error: {combined}"
            return combined

    if isinstance(result, str):
        return result

    return json.dumps(result, indent=2)[:8000]


# -------------------------------------------------------------------
# Convenience wrappers
# -------------------------------------------------------------------

def ping() -> str:
    return call_tool("snoocode_ping")


def list_prs(owner: str, repo: str, state: str = "open") -> str:
    return call_tool("list_prs", {"owner": owner, "repo": repo, "state": state})


def get_pr(owner: str, repo: str, pull_number: int) -> str:
    return call_tool("get_pr", {"owner": owner, "repo": repo, "pull_number": pull_number})


def get_pr_diff(owner: str, repo: str, pull_number: int) -> str:
    return call_tool("get_pr_diff", {"owner": owner, "repo": repo, "pull_number": pull_number})


def create_draft_pr(
    owner: str, repo: str, title: str, body: str,
    head: str, base: str = "master",
) -> str:
    return call_tool("create_draft_pr", {
        "owner": owner, "repo": repo, "title": title, "body": body,
        "head": head, "base": base,
    })


def ci_watcher(
    owner: str, repo: str, pull_number: int,
    poll_interval: int = 30, timeout: int = 1800,
) -> str:
    return call_tool("ci_watcher", {
        "owner": owner, "repo": repo, "pull_number": pull_number,
        "poll_interval": poll_interval, "timeout": timeout,
    })


def get_build_status(owner: str, repo: str, ref: str) -> str:
    return call_tool("get_build_status", {"owner": owner, "repo": repo, "ref": ref})


def get_build(owner: str, repo: str, build_number: int) -> str:
    return call_tool("get_build", {"owner": owner, "repo": repo, "build_number": build_number})


def get_build_logs(owner: str, repo: str, build_number: int, stage: int, step: int) -> str:
    return call_tool("get_build_logs", {
        "owner": owner, "repo": repo, "build_number": build_number,
        "stage": stage, "step": step,
    })


def list_builds(owner: str, repo: str, page: int = 1) -> str:
    return call_tool("list_builds", {"owner": owner, "repo": repo, "page": page})


def get_ticket(ticket_key: str) -> str:
    return call_tool("get_ticket", {"ticket_key": ticket_key})


def search_tickets(query: str) -> str:
    return call_tool("search_tickets", {"query": query})


def create_ticket(
    project: str, summary: str, description: str = "",
    issue_type: str = "Task",
) -> str:
    return call_tool("create_ticket", {
        "project": project, "summary": summary,
        "description": description, "issue_type": issue_type,
    })


def add_comment(ticket_key: str, body: str) -> str:
    return call_tool("add_comment", {"ticket_key": ticket_key, "body": body})


def transition_ticket(ticket_key: str, transition: str) -> str:
    return call_tool("transition_ticket", {"ticket_key": ticket_key, "transition": transition})


def workflow_list() -> str:
    return call_tool("workflow_list")


def run_agent(prompt: str, workspace: str = "", model: str = "") -> str:
    args: dict[str, Any] = {"prompt": prompt}
    if workspace:
        args["workspace"] = workspace
    if model:
        args["model"] = model
    raw = call_tool("run_agent", args, timeout=RUN_AGENT_HTTP_TIMEOUT)

    if raw.startswith("Error:"):
        return raw

    try:
        envelope = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw

    if isinstance(envelope, dict) and "result" in envelope:
        result_text = envelope["result"]
        if envelope.get("status") != "success":
            return f"Cursor agent exited with code 1: {result_text}"
        return str(result_text).strip() if result_text else ""

    return raw
