from __future__ import annotations

import asyncio
import functools
from pathlib import Path
from typing import Optional

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .context import format_todo_for_telegram, read_todo_detail
from .cron import CronJob, CronSchedule, CronStore, format_job_list
from .inbox import append_inbox_item
from .models import Task, TaskStatus, new_task_id
from .queue import TaskQueue

_TELEGRAM_MSG_LIMIT = 4096
_QUOTE_MAX_CHARS = 2000


def _extract_quoted_context(update: Update) -> str:
    """Extract text from a quoted/replied-to Telegram message."""
    msg = update.message
    if msg is None:
        return ""

    # Telegram "quote" field: the user highlighted a specific portion
    if hasattr(msg, "quote") and msg.quote and hasattr(msg.quote, "text") and msg.quote.text:
        return msg.quote.text[:_QUOTE_MAX_CHARS]

    # Standard reply-to: the full message being replied to
    reply = msg.reply_to_message
    if reply is None:
        return ""

    text = (reply.text or reply.caption or "").strip()
    if not text:
        return ""

    if len(text) > _QUOTE_MAX_CHARS:
        text = text[:_QUOTE_MAX_CHARS] + "..."
    return text


def _authorized(func):
    """Skip handler if the sender is not the allowed user."""
    @functools.wraps(func)
    async def wrapper(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_allowed(update):
            return
        return await func(self, update, context)
    return wrapper


def _clamp(text: str, limit: int = _TELEGRAM_MSG_LIMIT - 50) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "\n..."


class WorklogTelegramBot:
    def __init__(
        self,
        token: str,
        allowed_user_id: int,
        queue: TaskQueue,
        worklog_dir: Path | None = None,
        cron_store: CronStore | None = None,
    ) -> None:
        self.token = token
        self.allowed_user_id = allowed_user_id
        self.queue = queue
        self.worklog_dir = worklog_dir or Path.home() / "worklog"
        self.cron_store = cron_store
        self._app: Optional[Application] = None

    async def start(self) -> None:
        if not self.token:
            return
        app = ApplicationBuilder().token(self.token).build()
        app.add_handler(CommandHandler("tasks", self._tasks))
        app.add_handler(CommandHandler("cancel", self._cancel))
        app.add_handler(CommandHandler("todo", self._todo))
        app.add_handler(CommandHandler("add_todo", self._add_todo))
        app.add_handler(CommandHandler("new", self._new_session))
        app.add_handler(CommandHandler("cron", self._cron))
        app.add_handler(CommandHandler("compact", self._compact))
        app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
        )
        self._app = app
        await app.initialize()
        await app.start()
        await app.updater.start_polling()

    async def stop(self) -> None:
        if not self._app:
            return
        await self._app.updater.stop()
        await self._app.stop()
        await self._app.shutdown()

    def _is_allowed(self, update: Update) -> bool:
        user = update.effective_user
        if user is None:
            return False
        if self.allowed_user_id <= 0:
            return True
        return user.id == self.allowed_user_id

    @_authorized
    async def _tasks(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        tasks = await self.queue.list_recent(limit=10)
        if not tasks:
            await update.message.reply_text("No recent tasks.")
            return
        lines = [f"{t.id[:8]} {t.status.value} ({t.source})" for t in tasks]
        await update.message.reply_text("Recent tasks:\n" + "\n".join(lines))

    @_authorized
    async def _cancel(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        tasks = await self.queue.list_recent(limit=10)
        pending = [t for t in tasks if t.status in {TaskStatus.PENDING, TaskStatus.QUEUED}]
        if not pending:
            await update.message.reply_text("Nothing to cancel.")
            return
        target = pending[0]
        await self.queue.cancel_task(target.id)
        prompt_preview = target.prompt[:60] + ("..." if len(target.prompt) > 60 else "")
        await update.message.reply_text(f"Cancelled: {prompt_preview}")

    @_authorized
    async def _todo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        todo_path = self.worklog_dir / "TODO.md"
        task_num = context.args[0].strip() if context.args else ""
        if task_num.startswith("#"):
            task_num = task_num[1:]

        if task_num and task_num.isdigit():
            detail = await asyncio.to_thread(read_todo_detail, todo_path, task_num)
            text = f"TODO #{task_num}:\n\n{detail}" if detail else f"No detail section found for #{task_num}."
            text = text.replace("**", "").replace("~~", "")
        else:
            text = await asyncio.to_thread(format_todo_for_telegram, todo_path)

        await update.message.reply_text(_clamp(text))

    @_authorized
    async def _add_todo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = " ".join(context.args).strip() if context.args else ""
        if not text:
            await update.message.reply_text("Usage: /add_todo <task description>")
            return
        todo_path = self.worklog_dir / "TODO.md"
        result = await asyncio.to_thread(append_inbox_item, todo_path, text)
        await update.message.reply_text(result)

    @_authorized
    async def _new_session(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_chat:
            return
        session_key = f"telegram:{update.effective_chat.id}"
        cleared = await self.queue.clear_session_messages(session_key)
        await update.message.reply_text(f"Session reset. Cleared {cleared} turns.")

    @_authorized
    async def _cron(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.cron_store is None:
            await update.message.reply_text("Cron is not enabled.")
            return
        args = context.args or []
        if not args:
            jobs = self.cron_store.list_all()
            await update.message.reply_text(_clamp(format_job_list(jobs)))
            return
        sub = args[0].lower()
        if sub == "list":
            jobs = self.cron_store.list_all()
            await update.message.reply_text(_clamp(format_job_list(jobs)))
        elif sub == "remove" and len(args) > 1:
            job_id = args[1]
            for j in self.cron_store.list_all():
                if j.job_id.startswith(job_id):
                    self.cron_store.remove(j.job_id)
                    await update.message.reply_text(f"Removed: {j.name} ({j.job_id[:8]})")
                    return
            await update.message.reply_text(f"No job matching '{job_id}'.")
        else:
            await update.message.reply_text(
                "Usage:\n/cron — list jobs\n/cron remove <id> — remove a job\n"
                "Use the agent to create jobs via natural language."
            )

    @_authorized
    async def _compact(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_chat:
            return
        session_key = f"telegram:{update.effective_chat.id}"
        messages = await self.queue.get_recent_session_messages(session_key, limit=200)
        if len(messages) < 8:
            await update.message.reply_text(f"Session has only {len(messages)} messages, no compaction needed.")
            return
        # Enqueue a compaction request as a task
        task = Task(
            id=new_task_id(),
            source="compact",
            prompt=f"Compact the session history ({len(messages)} messages). Summarize the older messages.",
            status=TaskStatus.PENDING,
            telegram_chat_id=str(update.effective_chat.id),
            session_key=session_key,
        )
        await self.queue.enqueue(task)
        await update.message.reply_text(f"Compaction queued for {len(messages)} messages.")

    @_authorized
    async def _handle_message(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.effective_chat:
            return
        prompt = (update.message.text or "").strip()
        if not prompt:
            await update.message.reply_text("Please send a non-empty task prompt.")
            return

        quoted = _extract_quoted_context(update)
        if quoted:
            prompt = f"[Quoted message]\n{quoted}\n[End quote]\n\n{prompt}"

        task = Task(
            id=new_task_id(),
            source="telegram",
            prompt=prompt,
            status=TaskStatus.PENDING,
            telegram_chat_id=str(update.effective_chat.id),
            session_key=f"telegram:{update.effective_chat.id}",
        )
        await self.queue.enqueue(task)
        await self.queue.add_session_message(task.session_key or "", "user", prompt, task.id)
        await update.message.reply_text(f"Task queued: `{task.id}`", parse_mode="Markdown")
