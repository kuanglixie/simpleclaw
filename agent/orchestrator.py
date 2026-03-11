"""Orchestrator: agent loop using native Gemini function calling.

Includes heartbeat (periodic check-ins), cron job scheduling,
and context compaction for long sessions.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from google.genai import types

from .compaction import CompactionConfig, compact_session, needs_compaction
from .config import Settings
from .context import ContextAssembler
from .cron import CronStore, get_due_jobs
from .gemini import GeminiExecutor
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
from .output_contract import OutputContract, write_contract
from .queue import TaskQueue
from .tools.executor import ToolExecutor


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat()


def _summary_from_text(text: str) -> str:
    for line in text.splitlines():
        clean = line.strip()
        if clean:
            return clean[:300]
    return "(empty response)"


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

Guidelines:
- Use read_file, edit_file, grep_search, glob_files instead of shell when \
  doing file operations. Reserve shell for git, kubectl, gazette, and other \
  system commands.
- For TODO operations, use add_todo and read_todo instead of raw file editing.
- For Ray job monitoring, use check_ray_jobs and ray_job_describe.
- For scheduling, use cron_add, cron_list, cron_remove to manage recurring tasks.
- Use memory_write to persist important facts, preferences, or patterns you \
  learn. Use memory_read to recall stored knowledge. Your memory survives \
  across sessions and compaction — use it proactively.
- Be concise in your final answers — they go to Telegram.
- When you have enough information to answer, just answer. Don't over-tool.
"""


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        queue: TaskQueue,
        gemini: GeminiExecutor,
        context_assembler: ContextAssembler,
        notifier: Notifier,
        cron_store: CronStore | None = None,
    ) -> None:
        self.settings = settings
        self.queue = queue
        self.gemini = gemini
        self.context_assembler = context_assembler
        self.notifier = notifier
        self.cron_store = cron_store
        self.tool_executor = ToolExecutor(
            worklog_dir=settings.worklog_dir,
            shell_timeout=settings.tool_timeout_seconds,
            cron_store=cron_store,
        )
        self._system_instruction = SYSTEM_INSTRUCTION.format(
            worklog_dir=settings.worklog_dir,
        )
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

        # --- Context compaction before building context ---
        if session_key and self._compaction_config.enabled:
            await self._maybe_compact(session_key)

        recent_messages = await self.queue.get_recent_session_messages(
            session_key, limit=self._compaction_config.keep_recent + 10,
        )
        context_blob = await self.context_assembler.build(
            task, recent_messages=recent_messages,
        )
        await self.queue.update_task(task.id, context=context_blob)

        errors: list[str] = []
        tool_records = []
        final_answer = ""
        summary = ""
        status = TaskStatus.FAILED
        is_heartbeat = task.source == "heartbeat"

        user_prompt = f"{context_blob}\n\n---\nTask: {task.prompt}"
        contents: list = [
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=user_prompt)],
            ),
        ]

        for step in range(1, self.settings.max_agent_steps + 1):
            gemini_result = await self.gemini.generate_with_tools(
                contents, system_instruction=self._system_instruction,
            )

            if gemini_result.return_code != 0:
                err_msg = gemini_result.stderr.strip() or "Gemini call failed"
                errors.append(err_msg)
                final_answer = gemini_result.text.strip() or err_msg
                summary = _summary_from_text(final_answer)
                status = TaskStatus.FAILED
                break

            if gemini_result.function_calls:
                if gemini_result.raw_content:
                    contents.append(gemini_result.raw_content)

                fn_response_parts = []
                for fc in gemini_result.function_calls:
                    tool_result = await self.tool_executor.execute(fc.name, fc.args)
                    tool_records.append(tool_result.record)

                    if not tool_result.success:
                        errors.append(_summary_from_text(tool_result.output))

                    fn_response_parts.append(
                        types.Part.from_function_response(
                            name=fc.name,
                            response={
                                "result": tool_result.output[:4000],
                                "success": tool_result.success,
                            },
                        ),
                    )

                contents.append(types.Content(parts=fn_response_parts))
            else:
                final_answer = gemini_result.text.strip()
                summary = _summary_from_text(final_answer)
                status = TaskStatus.COMPLETED
                break
        else:
            status = TaskStatus.FAILED
            errors.append(
                f"Agent loop exceeded max steps ({self.settings.max_agent_steps}).",
            )
            final_answer = final_answer or "Task did not converge within step limit."
            summary = summary or "Task stopped at max agent steps."

        # --- Heartbeat: suppress HEARTBEAT_OK responses ---
        if is_heartbeat and status == TaskStatus.COMPLETED:
            if is_heartbeat_ok(final_answer, self._heartbeat_config.ack_max_chars):
                await self.queue.update_task(
                    task.id, status=TaskStatus.COMPLETED, result="HEARTBEAT_OK",
                )
                return  # silently drop

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
            model=self.settings.gemini_model,
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
            self.gemini, self.queue,
            worklog_dir=self.settings.worklog_dir,
        )

    # --- Heartbeat loop (replaces old scheduler_loop) ---

    async def heartbeat_loop(self) -> None:
        """Periodic heartbeat check-ins using HEARTBEAT.md."""
        if not self._heartbeat_config.enabled:
            return
        while True:
            await asyncio.sleep(self._heartbeat_config.every_seconds)
            try:
                if not is_within_active_hours(self._heartbeat_config):
                    continue

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
                    telegram_chat_id=None,
                    session_key="heartbeat:main",
                )
                await self.queue.enqueue(task)
            except Exception:  # noqa: BLE001
                pass

    # --- Cron job loop ---

    async def cron_loop(self) -> None:
        """Check for due cron jobs and enqueue them."""
        if not self.settings.cron_enabled or self.cron_store is None:
            return
        while True:
            await asyncio.sleep(30)  # check every 30s
            try:
                due = await asyncio.to_thread(get_due_jobs, self.cron_store)
                for job in due:
                    task = Task(
                        id=new_task_id(),
                        source=f"cron:{job.name}",
                        prompt=job.message,
                        status=TaskStatus.PENDING,
                        telegram_chat_id=None,
                        session_key=f"cron:{job.job_id}",
                    )
                    await self.queue.enqueue(task)
                    await asyncio.to_thread(
                        self.cron_store.mark_run, job.job_id, True,
                    )
            except Exception:  # noqa: BLE001
                pass
