from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.config import augment_process_path_for_cli_tools, bootstrap_config_env

bootstrap_config_env()
augment_process_path_for_cli_tools()

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

    import logging
    _log = logging.getLogger(__name__)

    loop_factories: dict[str, object] = {
        "process-loop": orchestrator.process_loop,
        "heartbeat-loop": orchestrator.heartbeat_loop,
        "self-improve-loop": orchestrator.self_improve_loop,
    }
    if cron_store is not None:
        loop_factories["cron-loop"] = orchestrator.cron_loop
    if settings.alerts_enabled:
        loop_factories["alert-loop"] = orchestrator.alert_loop
    if settings.quality_watchdog_enabled:
        loop_factories["quality-watchdog-loop"] = (
            orchestrator.quality_watchdog_loop
        )

    tasks: dict[str, asyncio.Task] = {}
    for name, factory in loop_factories.items():
        tasks[name] = asyncio.create_task(factory(), name=name)

    stop_event = asyncio.Event()

    def _handle_stop() -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_stop)

    must_run = {"process-loop", "cron-loop"}

    async def _watchdog() -> None:
        """Restart background tasks that die unexpectedly."""
        while not stop_event.is_set():
            await asyncio.sleep(10)
            for name, t in list(tasks.items()):
                if t.done() and not t.cancelled():
                    try:
                        exc = t.exception()
                    except asyncio.CancelledError:
                        continue
                    if exc is not None:
                        _log.error("Background task %r died: %s — restarting", name, exc)
                    elif name in must_run:
                        _log.warning("Critical task %r exited cleanly — restarting", name)
                    else:
                        continue
                    factory = loop_factories[name]
                    tasks[name] = asyncio.create_task(factory(), name=name)

    watchdog_task = asyncio.create_task(_watchdog(), name="watchdog")

    await stop_event.wait()
    watchdog_task.cancel()
    for t in tasks.values():
        t.cancel()
    await asyncio.gather(watchdog_task, *tasks.values(), return_exceptions=True)
    await telegram_bot.stop()
    if browser_server is not None:
        await browser_server.stop()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
