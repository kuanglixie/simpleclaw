"""Tool executor: dispatches function calls to implementations."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from ..cron import CronStore
from ..output_contract import ToolCallRecord
from . import file_ops, search, web, shell, todo, ray_ops, cron_ops, memory_ops


class ToolExecutor:
    def __init__(
        self,
        worklog_dir: Path,
        shell_timeout: int = 180,
        cron_store: CronStore | None = None,
    ) -> None:
        self.worklog_dir = worklog_dir
        self.shell_timeout = shell_timeout
        if cron_store is not None:
            cron_ops.set_cron_store(cron_store)

    async def execute(self, name: str, args: dict) -> ToolResult:
        start = time.time()
        try:
            output = await asyncio.to_thread(self._dispatch, name, args)
        except Exception as exc:
            output = f"Tool error ({name}): {exc}"
        elapsed = int((time.time() - start) * 1000)

        success = not output.startswith("Error:") and not output.startswith("Tool error")
        record = ToolCallRecord(
            name=name,
            input=_format_input(name, args),
            output_excerpt=output[:1500],
            exit_code=0 if success else 1,
            duration_ms=elapsed,
        )
        return ToolResult(output=output, success=success, record=record)

    def _dispatch(self, name: str, args: dict) -> str:
        wdir = str(self.worklog_dir)

        if name == "shell":
            result = shell.execute_shell(args.get("command", ""), self.shell_timeout)
            return result["output"]

        if name == "read_file":
            return file_ops.read_file(
                args["path"],
                offset=int(args.get("offset", 0)),
                limit=int(args.get("limit", 0)),
            )

        if name == "write_file":
            return file_ops.write_file(args["path"], args["contents"])

        if name == "edit_file":
            return file_ops.edit_file(
                args["path"], args["old_string"], args["new_string"],
            )

        if name == "glob_files":
            return search.glob_files(
                args["pattern"],
                directory=args.get("directory", wdir),
            )

        if name == "grep_search":
            return search.grep_search(
                args["pattern"],
                path=args.get("path", wdir),
                glob_filter=args.get("glob_filter", ""),
            )

        if name == "web_search":
            return web.web_search(
                args["query"],
                max_results=int(args.get("max_results", 8)),
            )

        if name == "web_fetch":
            return web.web_fetch(args["url"])

        if name == "add_todo":
            return todo.add_todo(args["text"], wdir)

        if name == "read_todo":
            return todo.read_todo(wdir, args.get("task_number", ""))

        if name == "check_ray_jobs":
            return ray_ops.check_ray_jobs(
                wdir,
                mode=args.get("mode", "standalone"),
                top=int(args.get("top", 10)),
            )

        if name == "ray_job_describe":
            return ray_ops.ray_job_describe(args["job_id"])

        if name == "cron_add":
            return cron_ops.cron_add(
                name=args.get("name", ""),
                message=args.get("message", ""),
                schedule_kind=args.get("schedule_kind", "at"),
                schedule_at=args.get("schedule_at", ""),
                schedule_every_seconds=int(args.get("schedule_every_seconds", 0)),
                schedule_cron_expr=args.get("schedule_cron_expr", ""),
                schedule_timezone=args.get("schedule_timezone", ""),
                delete_after_run=bool(args.get("delete_after_run", True)),
            )

        if name == "cron_list":
            return cron_ops.cron_list()

        if name == "cron_remove":
            return cron_ops.cron_remove(args.get("job_id", ""))

        if name == "memory_read":
            return memory_ops.memory_read(wdir, section=args.get("section", ""))

        if name == "memory_write":
            return memory_ops.memory_write(
                wdir,
                section=args.get("section", ""),
                content=args.get("content", ""),
                mode=args.get("mode", "append"),
            )

        return f"Error: unknown tool '{name}'"


class ToolResult:
    __slots__ = ("output", "success", "record")

    def __init__(self, output: str, success: bool, record: ToolCallRecord) -> None:
        self.output = output
        self.success = success
        self.record = record


def _format_input(name: str, args: dict) -> str:
    if name == "shell":
        return args.get("command", "")
    if name == "read_file":
        return args.get("path", "")
    if name == "grep_search":
        return f"{args.get('pattern', '')} in {args.get('path', '.')}"
    parts = [f"{k}={v!r}" for k, v in args.items()]
    return ", ".join(parts)[:300]
