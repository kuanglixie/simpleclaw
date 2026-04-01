from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.config import Settings, load_settings
from agent.context import ContextAssembler
from agent.cron import CronStore
from agent.notifier import Notifier
from agent.orchestrator import SYSTEM_INSTRUCTION, Orchestrator
from agent.pydantic_agent import create_agent
from agent.queue import TaskQueue
from agent.telegram_bot import WorklogTelegramBot


def _ensure_dirs(settings: Settings) -> None:
    settings.mailbox_dir.mkdir(parents=True, exist_ok=True)
    settings.pending_dir.mkdir(parents=True, exist_ok=True)
    settings.outbox_dir.mkdir(parents=True, exist_ok=True)
    settings.outbox_events_dir.mkdir(parents=True, exist_ok=True)
    settings.archive_dir.mkdir(parents=True, exist_ok=True)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.cron_dir.mkdir(parents=True, exist_ok=True)


def _export_vertex_env(settings: Settings) -> None:
    """Ensure Vertex AI env vars are set for PydanticAI's GoogleVertexModel."""
    if settings.gemini_use_vertexai:
        if settings.gemini_vertex_project:
            os.environ.setdefault("GOOGLE_CLOUD_PROJECT", settings.gemini_vertex_project)
        if settings.gemini_vertex_location:
            os.environ.setdefault("GOOGLE_CLOUD_LOCATION", settings.gemini_vertex_location)


async def run() -> None:
    settings = load_settings()
    _ensure_dirs(settings)
    _export_vertex_env(settings)

    queue = TaskQueue(settings.db_path, poll_interval_seconds=settings.queue_poll_interval_seconds)
    await queue.init()

    cron_store = CronStore(settings.cron_dir / "jobs.json") if settings.cron_enabled else None

    browser_server = None
    include_browser = False

    if settings.browser_server_enabled:
        from agent.browser_server import BrowserServer
        from agent.tools.browser import set_browser_server

        browser_server = BrowserServer(
            host=settings.browser_server_host,
            port=settings.browser_server_port,
            token=settings.browser_server_token,
            queue=queue,
        )
        set_browser_server(browser_server)
        await browser_server.start()
        include_browser = True

    system_instruction = SYSTEM_INSTRUCTION.format(
        worklog_dir=settings.worklog_dir,
    )
    agent = create_agent(
        model=settings.pydantic_ai_model,
        system_instruction=system_instruction,
        include_browser=include_browser,
    )

    context_assembler = ContextAssembler(settings.worklog_dir, settings.data_dir)
    notifier = Notifier(settings.telegram_bot_token)
    orchestrator = Orchestrator(
        settings, queue, agent, context_assembler, notifier,
        cron_store=cron_store,
    )
    telegram_bot = WorklogTelegramBot(
        token=settings.telegram_bot_token,
        allowed_user_id=settings.telegram_allowed_user_id,
        queue=queue,
        worklog_dir=settings.worklog_dir,
        cron_store=cron_store,
    )

    await telegram_bot.start()

    tasks = [
        asyncio.create_task(orchestrator.process_loop(), name="process-loop"),
        asyncio.create_task(orchestrator.heartbeat_loop(), name="heartbeat-loop"),
        asyncio.create_task(orchestrator.self_improve_loop(), name="self-improve-loop"),
    ]
    if cron_store is not None:
        tasks.append(asyncio.create_task(orchestrator.cron_loop(), name="cron-loop"))

    stop_event = asyncio.Event()

    def _handle_stop() -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_stop)

    await stop_event.wait()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await telegram_bot.stop()
    if browser_server is not None:
        await browser_server.stop()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
