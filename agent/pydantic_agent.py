"""PydanticAI agent factory with native tool registration.

Replaces the old declarations.py + executor.py + gemini.py stack.
Tools are registered as typed Python functions; PydanticAI auto-generates
JSON schemas from signatures and docstrings and handles dispatch.

Includes optional browser tools for Chrome extension integration.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from pydantic_ai import Agent, RunContext
from pydantic_ai.settings import ModelSettings

from .cron import CronJob, CronSchedule, CronStore, format_job_list
from .self_improve import (
    build_daily_digest,
    format_digest_telegram,
    append_evolution_log,
    extract_memory_updates,
)
from .tools import file_ops, memory_ops, ray_ops, search, shell, snoocode, todo, web

_CURSOR_CONTEXT_MAX = 6000


def _read_safe(path: Path, max_chars: int = 0) -> str:
    if not path.exists():
        return ""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return ""
    if max_chars and len(text) > max_chars:
        return text[:max_chars] + "\n...(truncated)"
    return text


_OUTPUT_INSTRUCTIONS = """\
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


def _build_cursor_agent_prompt(task_prompt: str, worklog_dir: Path) -> str:
    """Prepend essential context so the Cursor agent isn't flying blind."""
    parts: list[str] = []

    memory = _read_safe(worklog_dir / "MEMORY.md", max_chars=3000)
    if memory:
        parts.append(f"## Agent Memory (from {worklog_dir}/MEMORY.md)\n{memory}")

    todo_path = worklog_dir / "TODO.md"
    if todo_path.exists():
        lines = todo_path.read_text(errors="replace").splitlines()
        dashboard_end = len(lines)
        for i, line in enumerate(lines):
            if line.strip().startswith("## Details"):
                dashboard_end = i
                break
        dashboard = "\n".join(lines[:dashboard_end]).strip()
        if dashboard:
            parts.append(f"## TODO Dashboard\n{dashboard[:2000]}")

    snapshot = _read_safe(worklog_dir / "STATUS_SNAPSHOT.md", max_chars=1500)
    if snapshot:
        parts.append(f"## Status Snapshot\n{snapshot}")

    if not parts:
        context_block = ""
    else:
        context_block = "\n\n".join(parts)
        if len(context_block) > _CURSOR_CONTEXT_MAX:
            context_block = context_block[:_CURSOR_CONTEXT_MAX] + "\n...(truncated)"

    sections = []
    if context_block:
        sections.append(
            f"--- Context (auto-injected by SimpleClaw) ---\n"
            f"{context_block}\n"
            f"--- End Context ---"
        )
    sections.append(_OUTPUT_INSTRUCTIONS)
    sections.append(f"## Task\n{task_prompt}")

    return "\n\n".join(sections)


@dataclass
class AgentDeps:
    worklog_dir: Path
    shell_timeout: int = 180
    cron_store: Optional[CronStore] = None


def create_agent(
    model: str,
    system_instruction: str = "",
    include_browser: bool = False,
) -> Agent[AgentDeps]:
    """Create a PydanticAI agent with all tools registered."""

    agent: Agent[AgentDeps] = Agent(
        model,
        deps_type=AgentDeps,
        instructions=system_instruction,
        model_settings=ModelSettings(temperature=0.2),
    )

    # ------------------------------------------------------------------
    # Shell
    # ------------------------------------------------------------------

    @agent.tool
    def run_shell(ctx: RunContext[AgentDeps], command: str) -> str:
        """Run a bash shell command. Use for git, kubectl, gazette CLI, or system commands.
        Prefer read_file/grep_search/glob_files for file operations.

        Args:
            command: The bash command to execute.
        """
        result = shell.execute_shell(command, ctx.deps.shell_timeout)
        return result["output"]

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    @agent.tool_plain
    def read_file(path: str, offset: int = 0, limit: int = 0) -> str:
        """Read a file's contents. Use offset/limit for large files.

        Args:
            path: Absolute path to the file.
            offset: Starting line number (1-indexed). 0 means from the beginning.
            limit: Number of lines to read. 0 means read all.
        """
        return file_ops.read_file(path, offset=offset, limit=limit)

    @agent.tool_plain
    def write_file(path: str, contents: str) -> str:
        """Write contents to a file, creating parent directories as needed. Overwrites existing content.

        Args:
            path: Absolute path to the file.
            contents: The full contents to write.
        """
        return file_ops.write_file(path, contents)

    @agent.tool_plain
    def edit_file(path: str, old_string: str, new_string: str) -> str:
        """Replace an exact string match in a file. The old_string must appear exactly once.

        Args:
            path: Absolute path to the file.
            old_string: The exact text to find and replace (must be unique in the file).
            new_string: The replacement text.
        """
        return file_ops.edit_file(path, old_string, new_string)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    @agent.tool
    def glob_files(ctx: RunContext[AgentDeps], pattern: str, directory: str = "") -> str:
        """Find files matching a glob pattern, sorted by modification time (newest first).
        Patterns without leading **/ are auto-prefixed for recursive search.

        Args:
            pattern: Glob pattern, e.g. '*.py', '**/*.yaml', 'test_*.py'.
            directory: Directory to search in. Defaults to worklog directory.
        """
        d = directory or str(ctx.deps.worklog_dir)
        return search.glob_files(pattern, directory=d)

    @agent.tool
    def grep_search(
        ctx: RunContext[AgentDeps],
        pattern: str,
        path: str = "",
        glob_filter: str = "",
    ) -> str:
        """Search file contents using ripgrep. Supports regex patterns.

        Args:
            pattern: Regex pattern to search for.
            path: File or directory to search in. Defaults to worklog directory.
            glob_filter: Optional glob to filter files, e.g. '*.py', '*.md'.
        """
        p = path or str(ctx.deps.worklog_dir)
        return search.grep_search(pattern, path=p, glob_filter=glob_filter)

    @agent.tool
    def search_conversations(ctx: RunContext[AgentDeps], query: str) -> str:
        """Search past Cursor IDE conversations by keyword. Returns matching
        conversation UUIDs with summaries and a snippet of the matching content.

        Args:
            query: Keyword or phrase to search for in conversation transcripts.
        """
        return search.search_conversations(
            query, worklog_dir=str(ctx.deps.worklog_dir),
        )

    # ------------------------------------------------------------------
    # Web
    # ------------------------------------------------------------------

    @agent.tool_plain
    def web_search(query: str, max_results: int = 8) -> str:
        """Search the web for real-time information. Use for current events, documentation, error messages, etc.

        Args:
            query: Search query.
            max_results: Maximum number of results (default 8).
        """
        return web.web_search(query, max_results=max_results)

    @agent.tool_plain
    def web_fetch(url: str) -> str:
        """Fetch content from a URL and return it as readable text. Works for HTML pages, JSON APIs, and plain text.

        Args:
            url: The URL to fetch.
        """
        return web.web_fetch(url)

    # ------------------------------------------------------------------
    # TODO
    # ------------------------------------------------------------------

    @agent.tool
    def add_todo(ctx: RunContext[AgentDeps], text: str) -> str:
        """Add a new item to the TODO.md inbox. Timestamped and auto-committed via git.

        Args:
            text: The task description to add.
        """
        return todo.add_todo(text, str(ctx.deps.worklog_dir))

    @agent.tool
    def read_todo(ctx: RunContext[AgentDeps], task_number: str = "") -> str:
        """Read the TODO dashboard or a specific task's details from TODO.md.

        Args:
            task_number: Optional task number (e.g. '8') to read details. Empty for dashboard overview.
        """
        return todo.read_todo(str(ctx.deps.worklog_dir), task_number)

    # ------------------------------------------------------------------
    # Ray jobs
    # ------------------------------------------------------------------

    @agent.tool
    def check_ray_jobs(
        ctx: RunContext[AgentDeps], mode: str = "standalone", top: int = 10,
    ) -> str:
        """Check the status of Ray training/evaluation jobs.

        Args:
            mode: Job mode: 'standalone' or 'workspace'. Default 'standalone'.
            top: Number of recent jobs to show (default 10).
        """
        return ray_ops.check_ray_jobs(
            str(ctx.deps.worklog_dir), mode=mode, top=top,
        )

    @agent.tool_plain
    def ray_job_describe(job_id: str) -> str:
        """Get detailed information about a specific Ray job by its ID.

        Args:
            job_id: The Ray job ID to describe.
        """
        return ray_ops.ray_job_describe(job_id)

    @agent.tool
    def ray_job_submit(
        ctx: RunContext[AgentDeps],
        job_name: str,
        mode: str = "standalone",
        path: str = "train",
        worktree: str = "rfe-v2-features",
    ) -> str:
        """Submit a Ray training or evaluation job. This is the ONLY correct way
        to run ML jobs -- never try to run adhoc_job.py locally.

        Args:
            job_name: The adhoc job name, e.g. 'train-and-evaluate-rfe-v2-best-of'.
            mode: 'standalone' (gazette ray job run) or 'workspace' (gazette ray job submit). Default standalone.
            path: Job path, usually 'train'. Default 'train'.
            worktree: Git worktree name containing the experiment code. Default 'rfe-v2-features'.
        """
        return ray_ops.ray_job_submit(
            str(ctx.deps.worklog_dir),
            job_name=job_name,
            mode=mode,
            path=path,
            worktree=worktree,
        )

    # ------------------------------------------------------------------
    # Cron
    # ------------------------------------------------------------------

    @agent.tool
    def cron_add(
        ctx: RunContext[AgentDeps],
        name: str,
        message: str,
        schedule_kind: str,
        schedule_at: str = "",
        schedule_every_seconds: int = 0,
        schedule_cron_expr: str = "",
        schedule_timezone: str = "",
        delete_after_run: bool = True,
    ) -> str:
        """Add a scheduled cron job. Supports one-shot (at), recurring (every), or cron-expression schedules.

        Args:
            name: Human-readable name for the job.
            message: The prompt/task to execute when the job fires.
            schedule_kind: Schedule type: 'at' (one-shot ISO timestamp), 'every' (interval), or 'cron' (cron expression).
            schedule_at: ISO 8601 timestamp for one-shot jobs (schedule_kind='at').
            schedule_every_seconds: Interval in seconds for recurring jobs (schedule_kind='every').
            schedule_cron_expr: 5-field cron expression (schedule_kind='cron'), e.g. '0 7 * * *'.
            schedule_timezone: IANA timezone for cron expressions, e.g. 'America/Los_Angeles'.
            delete_after_run: Delete one-shot jobs after successful run (default true for 'at' jobs).
        """
        store = ctx.deps.cron_store
        if store is None:
            return "Error: cron not enabled."

        schedule = CronSchedule(
            kind=schedule_kind,
            at=schedule_at,
            every_seconds=schedule_every_seconds,
            cron_expr=schedule_cron_expr,
            timezone=schedule_timezone,
        )
        if schedule_kind == "at" and not schedule_at:
            return "Error: schedule_at is required for 'at' jobs."
        if schedule_kind == "every" and schedule_every_seconds <= 0:
            return "Error: schedule_every_seconds must be > 0 for 'every' jobs."
        if schedule_kind == "cron" and not schedule_cron_expr:
            return "Error: schedule_cron_expr is required for 'cron' jobs."

        job = CronJob(
            job_id="",
            name=name,
            schedule=schedule,
            message=message,
            delete_after_run=delete_after_run if schedule_kind == "at" else False,
        )
        created = store.add(job)
        return (
            f"Cron job created: {created.job_id[:8]} ({name})\n"
            f"Next run: {created.next_run_at}"
        )

    @agent.tool
    def cron_list(ctx: RunContext[AgentDeps]) -> str:
        """List all scheduled cron jobs with their status, schedule, and run count."""
        store = ctx.deps.cron_store
        if store is None:
            return "Error: cron not enabled."
        return format_job_list(store.list_all())

    @agent.tool
    def cron_remove(ctx: RunContext[AgentDeps], job_id: str) -> str:
        """Remove a cron job by its ID.

        Args:
            job_id: The job ID to remove (first 8 chars is sufficient).
        """
        store = ctx.deps.cron_store
        if store is None:
            return "Error: cron not enabled."
        for job in store.list_all():
            if job.job_id == job_id or job.job_id.startswith(job_id):
                if store.remove(job.job_id):
                    return f"Removed cron job: {job.job_id[:8]}"
                return f"Error: failed to remove job '{job_id}'."
        return f"Error: no job found matching '{job_id}'."

    # ------------------------------------------------------------------
    # Memory
    # ------------------------------------------------------------------

    @agent.tool
    def memory_read(ctx: RunContext[AgentDeps], section: str = "") -> str:
        """Read the agent's persistent MEMORY.md file. Contains accumulated knowledge
        across sessions: preferences, key facts, learned patterns, projects, people.

        Args:
            section: Optional section name to read (e.g. 'Key Facts', 'Preferences'). Empty reads entire file.
        """
        return memory_ops.memory_read(str(ctx.deps.worklog_dir), section=section)

    @agent.tool
    def memory_write(
        ctx: RunContext[AgentDeps],
        section: str,
        content: str,
        mode: str = "append",
    ) -> str:
        """Write to a section of MEMORY.md. Use to persist important facts or patterns.

        Args:
            section: Section name: 'Preferences', 'Key Facts', 'Learned Patterns', 'Projects', or 'People'.
            content: The content to write (markdown format, typically a bullet point).
            mode: Write mode: 'append' (add to section, default) or 'replace' (overwrite section).
        """
        return memory_ops.memory_write(
            str(ctx.deps.worklog_dir),
            section=section,
            content=content,
            mode=mode,
        )

    # ------------------------------------------------------------------
    # Snoocode (dev workflow tools via local MCP server)
    # ------------------------------------------------------------------

    @agent.tool_plain
    def snoocode_list_prs(owner: str, repo: str, state: str = "open") -> str:
        """List pull requests for a GitHub repository.

        Args:
            owner: Repository owner/org (e.g. 'reddit').
            repo: Repository name.
            state: PR state: 'open', 'closed', or 'all'. Default 'open'.
        """
        return snoocode.list_prs(owner, repo, state=state)

    @agent.tool_plain
    def snoocode_get_pr(owner: str, repo: str, pull_number: int) -> str:
        """Get details of a specific pull request.

        Args:
            owner: Repository owner/org.
            repo: Repository name.
            pull_number: The PR number.
        """
        return snoocode.get_pr(owner, repo, pull_number)

    @agent.tool_plain
    def snoocode_create_draft_pr(
        owner: str, repo: str, title: str, body: str,
        head: str, base: str = "master",
    ) -> str:
        """Create a draft pull request.

        Args:
            owner: Repository owner/org.
            repo: Repository name.
            title: PR title.
            body: PR description (markdown).
            head: Source branch name.
            base: Target branch (default 'master').
        """
        return snoocode.create_draft_pr(owner, repo, title, body, head, base)

    @agent.tool_plain
    def snoocode_ci_watcher(
        owner: str, repo: str, pull_number: int,
        poll_interval: int = 30, timeout: int = 1800,
    ) -> str:
        """Watch a PR's CI/CD builds until they pass or fail. Automatically
        investigates Drone CI failures and extracts error logs.

        Args:
            owner: Repository owner/org.
            repo: Repository name.
            pull_number: The PR number to watch.
            poll_interval: Seconds between status checks (default 30).
            timeout: Max seconds to watch (default 1800).
        """
        return snoocode.ci_watcher(owner, repo, pull_number, poll_interval, timeout)

    @agent.tool_plain
    def snoocode_get_build_status(owner: str, repo: str, ref: str) -> str:
        """Get Drone CI build status for a git ref (branch or SHA).

        Args:
            owner: Repository owner/org.
            repo: Repository name.
            ref: Git ref (branch name or commit SHA).
        """
        return snoocode.get_build_status(owner, repo, ref)

    @agent.tool_plain
    def snoocode_get_build_logs(
        owner: str, repo: str, build_number: int, stage: int, step: int,
    ) -> str:
        """Get logs for a specific Drone CI build stage/step.

        Args:
            owner: Repository owner/org.
            repo: Repository name.
            build_number: The Drone build number.
            stage: Stage number (1-indexed).
            step: Step number (1-indexed).
        """
        return snoocode.get_build_logs(owner, repo, build_number, stage, step)

    @agent.tool_plain
    def snoocode_get_ticket(ticket_key: str) -> str:
        """Get details of a Jira ticket.

        Args:
            ticket_key: The ticket key (e.g. 'DISCO-1234').
        """
        return snoocode.get_ticket(ticket_key)

    @agent.tool_plain
    def snoocode_search_tickets(query: str) -> str:
        """Search Jira tickets by JQL or keyword.

        Args:
            query: JQL query or keyword search string.
        """
        return snoocode.search_tickets(query)

    @agent.tool_plain
    def snoocode_add_ticket_comment(ticket_key: str, body: str) -> str:
        """Add a comment to a Jira ticket.

        Args:
            ticket_key: The ticket key (e.g. 'DISCO-1234').
            body: Comment text (markdown supported).
        """
        return snoocode.add_comment(ticket_key, body)

    @agent.tool
    def cursor_agent(
        ctx: RunContext[AgentDeps],
        prompt: str,
        workspace: str = "/Users/jing.lu/git_repos",
        model: str = "opus-4.6-thinking",
    ) -> str:
        """Delegate a task to the Cursor IDE agent. This is the most powerful
        tool available — the Cursor agent has full IDE access including file
        read/write, terminal, search, git, and ALL MCP servers (GitHub, Jira,
        Glean, BigQuery, etc.).

        USE THIS for:
        - Any code changes, edits, refactors, or fixes
        - Exploring or understanding codebases
        - Multi-step investigations that need file/code access
        - Running shell commands in a repo context
        - GitHub PR operations, CI debugging, Jira updates
        - Any task that benefits from IDE-level tool access

        Keep handling DIRECTLY (without cursor_agent) only:
        - Simple conversational responses
        - Quick TODO/memory reads that use existing tools
        - Ray job status checks via check_ray_jobs

        Args:
            prompt: Detailed task description for the Cursor agent. Be specific
                about what files, repos, or actions are needed.
            workspace: Working directory for the agent. Default is ~/git_repos.
                Use the specific repo path when possible (e.g.
                '/Users/jing.lu/git_repos/ray-jobs').
            model: Model to use. Default is opus-4.6-thinking.
        """
        full_prompt = _build_cursor_agent_prompt(prompt, ctx.deps.worklog_dir)
        return snoocode.run_agent(full_prompt, workspace=workspace, model=model)

    @agent.tool
    def self_review_today(ctx: RunContext[AgentDeps], date: str = "") -> str:
        """Run a self-improvement review for today (or a specific date).

        Analyzes completed/failed tasks, extracts failure patterns and tool usage,
        generates improvement ideas, and logs to EVOLUTION_LOG.md.
        Also updates MEMORY.md with new learned patterns.

        Call this during the daily self-improvement cron job, or manually to
        review a specific day's performance.

        Args:
            date: Date to review in YYYY-MM-DD format. Empty = today.
        """
        from datetime import datetime as _dt, timezone as _tz
        if not date:
            date = _dt.now(tz=_tz.utc).strftime("%Y-%m-%d")

        outbox = ctx.deps.worklog_dir / ".agent_mailbox" / "outbox"
        digest = build_daily_digest(outbox, date)

        append_evolution_log(ctx.deps.worklog_dir, digest)

        memory_entries = extract_memory_updates(digest)
        if memory_entries:
            combined = "\n".join(f"- {e}" for e in memory_entries)
            memory_ops.memory_write(
                str(ctx.deps.worklog_dir), "Learned Patterns", combined,
            )

        return format_digest_telegram(digest)

    @agent.tool_plain
    def snoocode_call(tool_name: str, arguments: str = "{}") -> str:
        """Generic fallback: call any snoocode tool by name. Use the specific
        snoocode_* tools or cursor_agent when possible. This is for less common
        operations like workflow_save, restart_build, update_pr, etc.

        Args:
            tool_name: The snoocode tool name (e.g. 'workflow_list', 'restart_build').
            arguments: JSON string of tool arguments.
        """
        import json as _json
        try:
            args = _json.loads(arguments) if arguments else {}
        except _json.JSONDecodeError:
            return f"Error: invalid JSON arguments: {arguments}"
        return snoocode.call_tool(tool_name, args)

    # ------------------------------------------------------------------
    # Browser (optional — Chrome extension relay)
    # ------------------------------------------------------------------

    if include_browser:
        _register_browser_tools(agent)

    return agent


def _register_browser_tools(agent: Agent[AgentDeps]) -> None:
    """Register browser tools that talk to the Chrome extension via WebSocket.

    These are async because they await the extension's response.
    Requires set_browser_server() to be called before the agent runs.
    """
    from .tools import browser

    @agent.tool_plain
    async def browser_list_tabs() -> str:
        """List all pinned browser tabs from the Chrome extension."""
        return await browser.browser_list_tabs()

    @agent.tool_plain
    async def browser_read_page(tab_id: int) -> str:
        """Read the current page state of a pinned tab (DOM snapshot with element IDs).

        Args:
            tab_id: The tab ID to read.
        """
        return await browser.browser_read_page(tab_id)

    @agent.tool_plain
    async def browser_click(tab_id: int, element_id: int) -> str:
        """Click an interactive element on a page. Returns updated page state.

        Args:
            tab_id: The tab ID.
            element_id: The element ID from the page state.
        """
        return await browser.browser_click(tab_id, element_id)

    @agent.tool_plain
    async def browser_type(tab_id: int, element_id: int, text: str) -> str:
        """Type text into an input element.

        Args:
            tab_id: The tab ID.
            element_id: The input element ID.
            text: The text to type.
        """
        return await browser.browser_type(tab_id, element_id, text)

    @agent.tool_plain
    async def browser_select(tab_id: int, element_id: int, value: str) -> str:
        """Select an option from a dropdown element.

        Args:
            tab_id: The tab ID.
            element_id: The select element ID.
            value: The value to select.
        """
        return await browser.browser_select(tab_id, element_id, value)

    @agent.tool_plain
    async def browser_scroll(tab_id: int, direction: str = "down") -> str:
        """Scroll the page up or down. Returns updated page state.

        Args:
            tab_id: The tab ID.
            direction: Scroll direction: 'up' or 'down'.
        """
        return await browser.browser_scroll(tab_id, direction)

    @agent.tool_plain
    async def browser_refresh(tab_id: int) -> str:
        """Refresh a page and return the new page state.

        Args:
            tab_id: The tab ID.
        """
        return await browser.browser_refresh(tab_id)

    @agent.tool_plain
    async def browser_navigate(tab_id: int, url: str) -> str:
        """Navigate a tab to a URL. Returns page state after load.

        Args:
            tab_id: The tab ID.
            url: The URL to navigate to.
        """
        return await browser.browser_navigate(tab_id, url)

    @agent.tool_plain
    async def browser_eval(tab_id: int, code: str) -> str:
        """Execute JavaScript in a tab and return the result.

        Args:
            tab_id: The tab ID.
            code: JavaScript code to execute.
        """
        return await browser.browser_eval(tab_id, code)

    @agent.tool_plain
    async def browser_click_text(tab_id: int, text: str) -> str:
        """Click an element matching the given visible text.

        Args:
            tab_id: The tab ID.
            text: The visible text to match and click.
        """
        return await browser.browser_click_text(tab_id, text)

    @agent.tool_plain
    async def browser_keyboard(
        tab_id: int,
        key: str,
        metaKey: bool = False,
        ctrlKey: bool = False,
        shiftKey: bool = False,
        altKey: bool = False,
    ) -> str:
        """Send a keyboard event to a tab.

        Args:
            tab_id: The tab ID.
            key: Key name (e.g. 'Enter', 'Tab', 'Escape', 'a').
            metaKey: Hold Cmd/Meta key.
            ctrlKey: Hold Ctrl key.
            shiftKey: Hold Shift key.
            altKey: Hold Alt key.
        """
        return await browser.browser_keyboard(
            tab_id, key,
            metaKey=metaKey, ctrlKey=ctrlKey,
            shiftKey=shiftKey, altKey=altKey,
        )

    @agent.tool_plain
    async def browser_wait(seconds: float) -> str:
        """Wait for a number of seconds (max 10) for page content to settle.

        Args:
            seconds: Seconds to wait (max 10).
        """
        return await browser.browser_wait(seconds)

    @agent.tool_plain
    async def browser_insert_text(tab_id: int, text: str) -> str:
        """Insert text at current cursor via CDP Input.insertText.
        More reliable on React/Slack apps than browser_type.

        Args:
            tab_id: The tab ID.
            text: Text to insert.
        """
        return await browser.browser_insert_text(tab_id, text)

    @agent.tool_plain
    async def browser_cdp_click(tab_id: int, element_id: int) -> str:
        """Click using CDP trusted mouse events. More reliable on React/Slack
        where synthetic events are ignored.

        Args:
            tab_id: The tab ID.
            element_id: The element ID.
        """
        return await browser.browser_cdp_click(tab_id, element_id)

    @agent.tool_plain
    async def browser_cdp_type(tab_id: int, element_id: int, text: str) -> str:
        """Focus element via CDP click then type via Input.insertText.
        Most reliable way to type into Slack/React inputs.

        Args:
            tab_id: The tab ID.
            element_id: The element ID.
            text: Text to type.
        """
        return await browser.browser_cdp_type(tab_id, element_id, text)

    @agent.tool_plain
    async def browser_slack_messages(tab_id: int) -> str:
        """Extract structured messages from the current Slack view.
        Returns sender, text, time, replies, and mention flags.

        Args:
            tab_id: The tab ID showing Slack.
        """
        return await browser.browser_slack_messages(tab_id)

    @agent.tool_plain
    async def browser_slack_unreads(tab_id: int) -> str:
        """Extract unread DMs and channels from the Slack sidebar.

        Args:
            tab_id: The tab ID showing Slack.
        """
        return await browser.browser_slack_unreads(tab_id)
