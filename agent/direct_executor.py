"""Direct executor: bypass Gemini, send tasks straight to cursor_agent.

Builds a full prompt from ContextAssembler output + system instructions +
Telegram output rules, then calls snoocode.run_agent(). Keeps Gemini
PydanticAI agent as a fallback when snoocode is unreachable.
"""

from __future__ import annotations

from pathlib import Path

from .tools import snoocode

_CONNECTION_ERROR_MARKERS = (
    "Error: failed to establish MCP session",
    "Error: empty response from snoocode",
    "Error: unparseable response",
    "connection refused",
)

SYSTEM_PROMPT = """\
You are SimpleClaw, a personal AI assistant operating through Telegram.
You have full IDE access: file read/write, terminal, search, git, and all \
MCP servers (GitHub, Jira, Glean, BigQuery, Slack).

## Key Files & Locations

- **Worklog dir**: {worklog_dir}
- **TODO**: {worklog_dir}/TODO.md (dashboard table + ### detail sections)
- **Memory**: {worklog_dir}/MEMORY.md (sections: Preferences, Key Facts, \
Learned Patterns, Projects, People)
- **Dailylog**: {worklog_dir}/dailylog.md
- **Heartbeat config**: {worklog_dir}/HEARTBEAT.md
- **Status snapshot**: {worklog_dir}/STATUS_SNAPSHOT.md
- **Evolution log**: {worklog_dir}/EVOLUTION_LOG.md

## Cron Job Management

Cron jobs are stored in **{cron_path}** as JSON. To manage them, \
read and edit this file directly. Schema:
```json
{{
  "jobs": [
    {{
      "job_id": "uuid",
      "name": "descriptive-name",
      "schedule": {{
        "kind": "at|every|cron",
        "at": "2026-04-01T10:00:00+00:00",
        "every_seconds": 3600,
        "cron_expr": "0 9 * * *",
        "timezone": ""
      }},
      "message": "prompt to run when job fires",
      "enabled": true,
      "delete_after_run": false,
      "created_at": "iso-timestamp",
      "last_run_at": "",
      "next_run_at": "iso-timestamp",
      "run_count": 0,
      "consecutive_failures": 0
    }}
  ]
}}
```
- **One-shot** (kind: "at"): set `delete_after_run: true` to auto-remove after success
- **Recurring** (kind: "every"): set `every_seconds`
- **Cron expression** (kind: "cron"): standard 5-field cron in `cron_expr`
- `next_run_at` must be a valid ISO timestamp for the job to fire
- Generate a UUID for `job_id`, set `created_at` to current time
- The cron loop reloads this file every 30 seconds

## Memory Management

Read/write **{worklog_dir}/MEMORY.md** directly. Use the existing section \
format (## Preferences, ## Key Facts, ## Learned Patterns, etc.). \
Persist important facts, user preferences, and patterns you learn.

## Self-Review

For daily self-improvement reviews, run:
```bash
cd {simpleclaw_dir} && python3 -c "
from agent.self_improve import build_daily_digest, format_digest_telegram, \
append_evolution_log, extract_memory_updates
from pathlib import Path
outbox = Path('{worklog_dir}/.agent_mailbox/outbox')
digest = build_daily_digest(outbox, 'YYYY-MM-DD')
append_evolution_log(Path('{worklog_dir}'), digest)
print(format_digest_telegram(digest))
"
```

## Ray Jobs

- Submit: `gazette ray job run --path train` (standalone) or \
`gazette ray job submit --path train` (workspace)
- List: `gazette ray job list`
- Describe: `gazette ray job describe <job_id>`
- Workspace: `gazette ray workspace describe`

## Action Rules

- When the user says "yes", "do it", "go ahead" — execute the action \
from conversation history. Look at the recent messages to find what was proposed.
- Report what you DID, not what you plan to do.
- NEVER report factual claims about job status, PRs, or pipelines without \
verifying via a tool call first.
- For scheduling/reminders: edit {cron_path} directly.

## Output Format (CRITICAL)

Your response goes DIRECTLY to a Telegram chat. Format accordingly:
- Lead with the KEY FINDING or ANSWER in the first 1-2 lines
- Use bullet points, not long paragraphs
- Bold (**text**) for critical items, status labels, blockers
- Keep total response under 3500 chars (Telegram limit is 4096)
- If there are actionable items, list them clearly at the end
- Do NOT include internal reasoning, tool call logs, or "let me check" preamble
- Do NOT say "here is a summary" — just give the summary directly
"""


def _is_snoocode_error(response: str) -> bool:
    """Check if the response indicates snoocode is unreachable."""
    for marker in _CONNECTION_ERROR_MARKERS:
        if marker in response:
            return True
    return False


def build_prompt(
    context_blob: str,
    task_prompt: str,
    worklog_dir: Path,
    cron_path: Path,
    simpleclaw_dir: Path,
) -> str:
    """Assemble the full prompt: system instructions + context + task."""
    system = SYSTEM_PROMPT.format(
        worklog_dir=worklog_dir,
        cron_path=cron_path,
        simpleclaw_dir=simpleclaw_dir,
    )

    sections = [system]

    if context_blob:
        sections.append(
            f"--- Context (auto-injected by SimpleClaw) ---\n"
            f"{context_blob}\n"
            f"--- End Context ---"
        )

    sections.append(f"## Task\n{task_prompt}")

    return "\n\n".join(sections)


def execute(
    context_blob: str,
    task_prompt: str,
    worklog_dir: Path,
    cron_path: Path,
    simpleclaw_dir: Path,
    workspace: str = "",
    model: str = "opus-4.6-thinking",
) -> str:
    """Send a task directly to cursor_agent via snoocode.

    Returns the agent's response text, or an error string starting with
    "SNOOCODE_UNREACHABLE:" if the connection failed (caller should fallback).
    """
    full_prompt = build_prompt(
        context_blob, task_prompt, worklog_dir, cron_path, simpleclaw_dir,
    )

    ws = workspace or str(worklog_dir)
    result = snoocode.run_agent(full_prompt, workspace=ws, model=model)

    if _is_snoocode_error(result):
        return f"SNOOCODE_UNREACHABLE: {result}"

    return result
