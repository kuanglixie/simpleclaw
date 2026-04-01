"""Orchestrator: task processing using PydanticAI agent with native tool calling.

Includes heartbeat (periodic check-ins), cron job scheduling,
and context compaction for long sessions.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from pydantic_ai import Agent
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import ToolCallPart, ToolReturnPart
from pydantic_ai.usage import UsageLimits

from .compaction import CompactionConfig, compact_session, needs_compaction
from .config import Settings
from .context import ContextAssembler
from .direct_executor import execute as direct_execute
from .status_snapshot import write_snapshot
from .cron import CronStore, get_due_jobs
from .heartbeat import (
    ActiveHours,
    HeartbeatConfig,
    is_heartbeat_ok,
    is_within_active_hours,
    read_heartbeat_md,
    strip_heartbeat_token,
)
from .models import Task, TaskStatus, new_task_id
from .notifier import Notifier
from .output_contract import OutputContract, ToolCallRecord, write_contract
from .pydantic_agent import AgentDeps
from .queue import TaskQueue


SYSTEM_INSTRUCTION = """\
You are a personal assistant agent running as a Telegram bot for a machine \
learning engineer. You help manage work items, monitor Ray training jobs, \
update daily logs, and answer technical questions.

Your workspace is at {worklog_dir} which contains:
- SOUL.md: Your identity and behavioral guidelines (loaded into context)
- MEMORY.md: Your long-term memory — accumulated facts, preferences, patterns
- TODO.md: Task tracking with dashboard and detail sections
- dailylog.md: Daily work notes and activity logs
- HEARTBEAT.md: Heartbeat check-in checklist
- scripts/: Utility scripts including Ray job management
- knowledge-base/: Reference docs and experiment notes
- cursor-conversations/: Cursor IDE chat history as .jsonl files. \
  index.md maps UUIDs to first-line summaries. Each .jsonl has one JSON \
  object per line with "role" and "message" fields.

CURSOR AGENT — YOUR PRIMARY TOOL:
You have access to cursor_agent(), which delegates tasks to a full Cursor IDE \
agent. The Cursor agent has file read/write, terminal, search, git, and ALL \
MCP servers (GitHub, Jira, Glean, BigQuery, Playwright, etc.).

USE cursor_agent() for MOST tasks:
- Code changes, edits, fixes, refactors in any repo
- Exploring or understanding codebases
- Multi-step investigations needing file/code access
- GitHub PR operations (create, review, check CI)
- Jira ticket operations (fetch, comment, transition)
- Running shell commands in a specific repo context
- Daily log updates, experiment audits, research scans
- STATUS CHECKS: "what's happening with X", "summarize my experiments", \
  "how's the PR doing", "what did I work on today" — these need deep \
  investigation across files, jobs, and PRs. cursor_agent can search \
  across repos, read logs, check CI, and cross-reference TODO items. \
  Don't try to answer these with shallow check_ray_jobs alone.
- Anything that benefits from IDE-level tool access

When calling cursor_agent(), write a DETAILED prompt. Include:
- What repo/workspace to work in (give the full path)
- What specific outcome you want
- Any context from the user's message or conversation history

Model selection for cursor_agent:
- Always uses claude-4.6-opus by default
- Override only if explicitly requested by the user

Handle DIRECTLY (without cursor_agent) only:
- Simple conversational replies ("thanks", "ok", greetings)
- Quick TODO reads: read_todo
- SIMPLE Ray job checks: "is job X still running?" -> check_ray_jobs + ray_job_describe
- Ray job submission: ray_job_submit
- Cron management: cron_add, cron_list, cron_remove
- Memory reads/writes: memory_read, memory_write
- Quick file reads when you know the exact path: read_file

IMPORTANT: If you answer directly but the result feels shallow or incomplete, \
it probably is. Re-do it via cursor_agent instead. For example: \
"summarize experiment status" should NOT be a single check_ray_jobs call — \
delegate to cursor_agent so it can cross-reference TODO items, read experiment \
configs, check multiple job statuses, and produce a real summary.

Other guidelines:
- For scheduling, use cron_add, cron_list, cron_remove to manage recurring tasks.
- For Ray job monitoring, use check_ray_jobs and ray_job_describe.
- For submitting Ray jobs, use ray_job_submit. NEVER try to run adhoc_job.py \
  locally. Always submit via ray_job_submit(job_name=..., mode=...).
- Use memory_write to persist important facts, preferences, or patterns you \
  learn. Use memory_read to recall stored knowledge. Your memory survives \
  across sessions and compaction — use it proactively.
- Be concise in your final answers — they go to Telegram (4096 char limit).
- When cursor_agent returns a result, pass it through as your answer. \
  Do NOT re-summarize or add preamble like "Here's what I found". The \
  cursor_agent output is already formatted for Telegram. Just return it.
- When you have enough information to answer, just answer. Don't over-tool.
- NEVER report factual claims about task status, PR merges, job progress, \
  pipeline states, or data backfills without first verifying via a tool call. \
  If your tools fail, say "I could not verify" rather than guessing.

Action execution rules (CRITICAL):
- When the user asks you to FIX, UPDATE, or CHANGE something, delegate to \
  cursor_agent() with a clear prompt. It can edit files, run commands, etc.
- When the user says "yes", "do it", "go ahead", or similar confirmations, \
  treat it as approval to execute the action you most recently proposed. \
  Look at the conversation history to find what you offered to do.
- Report what you actually DID, not what you plan to do.

Multi-item messages:
- When the user references multiple task numbers (e.g. "42: please do. 44: do it"), \
  handle each one sequentially. For each item, first read_todo to get context, \
  then take the action (typically ray_job_submit for experiments). \
  Prefer finishing fewer items well over starting many and running out of steps.
- If a TODO item says "submit" or "resubmit" an experiment, use ray_job_submit. \
  Read the TODO to find the job name (e.g. 'train-and-evaluate-rfe-v2-best-of').
"""


class _SnoocodeFallback(Exception):
    """Raised when snoocode is unreachable, triggering Gemini fallback."""


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat()


def _summary_from_text(text: str) -> str:
    for line in text.splitlines():
        clean = line.strip()
        if clean:
            return clean[:300]
    return "(empty response)"


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        queue: TaskQueue,
        agent: Agent[AgentDeps],
        context_assembler: ContextAssembler,
        notifier: Notifier,
        cron_store: CronStore | None = None,
    ) -> None:
        self.settings = settings
        self.queue = queue
        self.agent = agent
        self.context_assembler = context_assembler
        self.notifier = notifier
        self.cron_store = cron_store
        self._heartbeat_config = HeartbeatConfig(
            enabled=settings.heartbeat_enabled,
            every_seconds=settings.heartbeat_every_seconds,
            light_context=settings.heartbeat_light_context,
            active_hours=ActiveHours(
                start=settings.heartbeat_active_hours_start,
                end=settings.heartbeat_active_hours_end,
                timezone=settings.heartbeat_active_hours_tz,
            ),
        )
        self._compaction_config = CompactionConfig(
            enabled=settings.compaction_enabled,
            max_session_messages=settings.compaction_max_session_messages,
            keep_recent=settings.compaction_keep_recent,
            max_context_chars=settings.compaction_max_context_chars,
        )
        self._pending_cron_tasks: dict[str, str] = {}  # task_id -> cron job_id

    # --- Task processing loop ---

    async def process_loop(self) -> None:
        while True:
            task = await self.queue.dequeue_next()
            started_at = _now_iso()
            try:
                await self._execute_task(task, started_at)
            except Exception as exc:  # noqa: BLE001
                await self._handle_task_error(task, exc)

    async def _execute_task(self, task: Task, started_at: str) -> None:
        session_key = task.session_key or ""

        if session_key and self._compaction_config.enabled:
            await self._maybe_compact(session_key)

        try:
            await asyncio.to_thread(write_snapshot, self.settings.worklog_dir)
        except Exception:  # noqa: BLE001
            pass

        recent_messages = await self.queue.get_recent_session_messages(
            session_key, limit=self._compaction_config.keep_recent + 10,
        )
        context_blob = await self.context_assembler.build(
            task, recent_messages=recent_messages,
        )
        await self.queue.update_task(task.id, context=context_blob)

        errors: list[str] = []
        tool_records: list[ToolCallRecord] = []
        final_answer = ""
        summary = ""
        status = TaskStatus.FAILED
        is_heartbeat = task.source == "heartbeat"
        model_used = "cursor_agent"

        # --- Primary path: direct to cursor_agent via snoocode ---
        try:
            cron_path = self.settings.cron_dir / "jobs.json"
            simpleclaw_dir = self.settings.worklog_dir.parent / "sideproject" / "simpleclaw"
            if not simpleclaw_dir.exists():
                from pathlib import Path as _P
                simpleclaw_dir = _P(__file__).resolve().parent.parent

            raw = await asyncio.to_thread(
                direct_execute,
                context_blob,
                task.prompt,
                self.settings.worklog_dir,
                cron_path,
                simpleclaw_dir,
            )

            if raw.startswith("SNOOCODE_UNREACHABLE:"):
                raise _SnoocodeFallback(raw)

            final_answer = raw.strip()
            summary = _summary_from_text(final_answer)
            status = TaskStatus.COMPLETED

        except _SnoocodeFallback:
            # --- Fallback: Gemini via PydanticAI ---
            model_used = self.settings.gemini_model
            user_prompt = f"{context_blob}\n\n---\nTask: {task.prompt}"
            deps = AgentDeps(
                worklog_dir=self.settings.worklog_dir,
                shell_timeout=self.settings.tool_timeout_seconds,
                cron_store=self.cron_store,
            )
            try:
                result = await self.agent.run(
                    user_prompt,
                    deps=deps,
                    usage_limits=UsageLimits(
                        request_limit=self.settings.max_agent_steps,
                    ),
                )
                final_answer = str(result.output).strip()
                summary = _summary_from_text(final_answer)
                tool_records = _extract_tool_records(result)
                status = TaskStatus.COMPLETED

            except UsageLimitExceeded:
                status = TaskStatus.FAILED
                errors.append(
                    f"Agent loop exceeded max steps ({self.settings.max_agent_steps}).",
                )
                final_answer = "Task did not converge within step limit."
                summary = "Task stopped at max agent steps."

            except (UnexpectedModelBehavior, Exception) as exc:  # noqa: BLE001
                status = TaskStatus.FAILED
                err_msg = str(exc).strip() or "Agent execution failed"
                errors.append(err_msg)
                final_answer = err_msg
                summary = _summary_from_text(err_msg)

        except Exception as exc:  # noqa: BLE001
            status = TaskStatus.FAILED
            err_msg = str(exc).strip() or "Direct executor failed"
            errors.append(err_msg)
            final_answer = err_msg
            summary = _summary_from_text(err_msg)

        # --- Heartbeat: suppress HEARTBEAT_OK responses ---
        if is_heartbeat and status == TaskStatus.COMPLETED:
            if is_heartbeat_ok(final_answer, self._heartbeat_config.ack_max_chars):
                await self.queue.update_task(
                    task.id, status=TaskStatus.COMPLETED, result="HEARTBEAT_OK",
                )
                return

            final_answer = strip_heartbeat_token(final_answer)

        finished_at = _now_iso()
        contract = OutputContract(
            task_id=task.id,
            run_id=str(uuid4()),
            attempt=1,
            source=task.source,
            session_key=session_key,
            status=status.value,
            started_at=started_at,
            finished_at=finished_at,
            model=model_used,
            final_answer=final_answer,
            summary=summary or _summary_from_text(final_answer),
            tool_calls=tool_records,
            errors=errors,
            next_actions=["Review output in Cursor."],
        )
        md_path, json_path = write_contract(contract, self.settings.outbox_dir)

        await self.queue.update_task(
            task.id,
            status=status,
            result=contract.final_answer,
            error="\n".join(errors),
            output_md_path=str(md_path),
            output_json_path=str(json_path),
        )

        cron_job_id = self._pending_cron_tasks.pop(task.id, None)
        if cron_job_id and self.cron_store is not None:
            success = status == TaskStatus.COMPLETED
            await asyncio.to_thread(self.cron_store.mark_run, cron_job_id, success)

        latest = await self.queue.get_task(task.id)
        if session_key:
            if status == TaskStatus.FAILED:
                assistant_msg = (
                    f"[assistant_error] "
                    f"{(errors[0] if errors else contract.summary)[:1200]}"
                )
            else:
                assistant_msg = contract.final_answer or contract.summary
            await self.queue.add_session_message(
                session_key, "assistant", assistant_msg, task.id,
            )
        if latest is not None:
            await self.notifier.notify_task_result(latest, json_path)

    async def _handle_task_error(self, task: Task, exc: Exception) -> None:
        await self.queue.update_task(
            task.id, status=TaskStatus.FAILED, error=str(exc),
        )
        cron_job_id = self._pending_cron_tasks.pop(task.id, None)
        if cron_job_id and self.cron_store is not None:
            await asyncio.to_thread(self.cron_store.mark_run, cron_job_id, False)
        if task.session_key:
            await self.queue.add_session_message(
                task.session_key,
                "assistant",
                f"[assistant_error] task execution exception: {exc}",
                task.id,
            )
        latest = await self.queue.get_task(task.id)
        if latest is not None and latest.telegram_chat_id:
            await self.notifier.send_text(
                latest.telegram_chat_id, f"Task {latest.id} failed: {exc}",
            )

    # --- Context compaction ---

    async def _maybe_compact(self, session_key: str) -> None:
        messages = await self.queue.get_recent_session_messages(
            session_key, limit=200,
        )
        if not needs_compaction(messages, self._compaction_config):
            return
        await compact_session(
            session_key, messages, self._compaction_config,
            model=self.settings.pydantic_ai_model,
            queue=self.queue,
            worklog_dir=self.settings.worklog_dir,
        )

    # --- Heartbeat loop ---

    def _user_chat_id(self) -> str | None:
        """Derive Telegram chat ID from the allowed user ID.

        For Telegram DMs, chat_id == user_id.
        """
        uid = self.settings.telegram_allowed_user_id
        return str(uid) if uid and uid > 0 else None

    async def heartbeat_loop(self) -> None:
        if not self._heartbeat_config.enabled:
            return
        while True:
            await asyncio.sleep(self._heartbeat_config.every_seconds)
            try:
                if not is_within_active_hours(self._heartbeat_config):
                    continue

                await asyncio.to_thread(
                    write_snapshot, self.settings.worklog_dir,
                )

                hb_content = await asyncio.to_thread(
                    read_heartbeat_md, self.settings.worklog_dir,
                )
                if not hb_content:
                    continue

                prompt = self._heartbeat_config.prompt
                if hb_content:
                    prompt = f"HEARTBEAT.md content:\n{hb_content}\n\n{prompt}"

                task = Task(
                    id=new_task_id(),
                    source="heartbeat",
                    prompt=prompt,
                    status=TaskStatus.PENDING,
                    telegram_chat_id=self._user_chat_id(),
                    session_key="heartbeat:main",
                )
                await self.queue.enqueue(task)
            except Exception:  # noqa: BLE001
                pass

    # --- Self-improvement loop ---

    async def self_improve_loop(self) -> None:
        """Daily self-improvement: runs at 11pm local, reviews the day's tasks."""
        from .self_improve import SELF_IMPROVE_PROMPT
        while True:
            now = datetime.now()
            target = now.replace(hour=23, minute=0, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            wait_seconds = (target - now).total_seconds()
            await asyncio.sleep(wait_seconds)

            try:
                task = Task(
                    id=new_task_id(),
                    source="self-improve",
                    prompt=SELF_IMPROVE_PROMPT,
                    status=TaskStatus.PENDING,
                    telegram_chat_id=self._user_chat_id(),
                    session_key="self-improve:daily",
                )
                await self.queue.enqueue(task)
            except Exception:  # noqa: BLE001
                pass

    # --- Cron job loop ---

    async def cron_loop(self) -> None:
        if not self.settings.cron_enabled or self.cron_store is None:
            return
        chat_id = self._user_chat_id()
        while True:
            await asyncio.sleep(30)
            try:
                await asyncio.to_thread(self.cron_store.reload)
                due = await asyncio.to_thread(get_due_jobs, self.cron_store)
                for job in due:
                    task = Task(
                        id=new_task_id(),
                        source=f"cron:{job.name}",
                        prompt=job.message,
                        status=TaskStatus.PENDING,
                        telegram_chat_id=chat_id,
                        session_key=f"cron:{job.job_id}",
                    )
                    await self.queue.enqueue(task)
                    self._pending_cron_tasks[task.id] = job.job_id
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_tool_records(result) -> list[ToolCallRecord]:
    """Build ToolCallRecords from PydanticAI's message history."""
    calls: dict[str, tuple[str, dict]] = {}
    records: list[ToolCallRecord] = []

    for msg in result.all_messages():
        for part in msg.parts:
            if isinstance(part, ToolCallPart):
                calls[part.tool_call_id] = (
                    part.tool_name,
                    dict(part.args) if part.args else {},
                )
            elif isinstance(part, ToolReturnPart):
                call_info = calls.get(part.tool_call_id)
                name = call_info[0] if call_info else part.tool_name
                args = call_info[1] if call_info else {}
                output = str(part.content)[:1500]
                is_error = output.startswith("Error:") or output.startswith(
                    "Tool error",
                )
                records.append(
                    ToolCallRecord(
                        name=name,
                        input=_format_tool_input(name, args),
                        output_excerpt=output,
                        exit_code=1 if is_error else 0,
                        duration_ms=0,
                    ),
                )

    return records


def _format_tool_input(name: str, args: dict) -> str:
    if name == "run_shell":
        return args.get("command", "")
    if name == "read_file":
        return args.get("path", "")
    if name == "grep_search":
        return f"{args.get('pattern', '')} in {args.get('path', '.')}"
    parts = [f"{k}={v!r}" for k, v in args.items()]
    return ", ".join(parts)[:300]
