"""Function declarations for Gemini native function calling.

Each declaration maps to a tool implementation. The Gemini SDK uses these
to let the model decide which tool to call and with what arguments.
"""

from __future__ import annotations


TOOL_DECLARATIONS = [
    {
        "name": "shell",
        "description": (
            "Run a bash shell command. Use for git operations, kubectl, "
            "gazette CLI, pip, or any system command not covered by other tools. "
            "Prefer specific tools (read_file, grep_search) over shell when possible."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The bash command to execute.",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read the contents of a file. For large files, use offset and limit "
            "to read specific line ranges."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the file.",
                },
                "offset": {
                    "type": "integer",
                    "description": "Starting line number (1-indexed). 0 means start from beginning.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of lines to read. 0 means read all.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write contents to a file, creating parent directories as needed. Overwrites existing content.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the file.",
                },
                "contents": {
                    "type": "string",
                    "description": "The full contents to write.",
                },
            },
            "required": ["path", "contents"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "Make a precise edit to a file by replacing an exact string match. "
            "The old_string must appear exactly once in the file."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the file.",
                },
                "old_string": {
                    "type": "string",
                    "description": "The exact text to find and replace (must be unique in the file).",
                },
                "new_string": {
                    "type": "string",
                    "description": "The replacement text.",
                },
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    {
        "name": "glob_files",
        "description": (
            "Find files matching a glob pattern. Results sorted by modification time (newest first). "
            "Patterns without leading **/ are auto-prefixed for recursive search."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern, e.g. '*.py', '**/*.yaml', 'test_*.py'.",
                },
                "directory": {
                    "type": "string",
                    "description": "Directory to search in. Defaults to worklog_dir.",
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "grep_search",
        "description": (
            "Search file contents using ripgrep. Supports regex patterns. "
            "Use for finding code, config values, or text across files."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regex pattern to search for.",
                },
                "path": {
                    "type": "string",
                    "description": "File or directory to search in. Defaults to worklog_dir.",
                },
                "glob_filter": {
                    "type": "string",
                    "description": "Optional glob to filter files, e.g. '*.py', '*.md'.",
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "web_search",
        "description": "Search the web for real-time information. Use for current events, documentation, error messages, etc.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results (default 8).",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "web_fetch",
        "description": "Fetch content from a URL and return it as readable text. Works for HTML pages, JSON APIs, and plain text.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to fetch.",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "add_todo",
        "description": "Add a new item to the TODO.md inbox. The item is timestamped and auto-committed via git.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The task description to add.",
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "read_todo",
        "description": "Read the TODO dashboard or a specific task's details from TODO.md.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_number": {
                    "type": "string",
                    "description": "Optional task number (e.g. '8') to read details. Empty for dashboard overview.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "check_ray_jobs",
        "description": "Check the status of Ray training/evaluation jobs. Shows recent jobs with their status, errors, and runtime.",
        "parameters": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "description": "Job mode: 'standalone' or 'workspace'. Default 'standalone'.",
                },
                "top": {
                    "type": "integer",
                    "description": "Number of recent jobs to show (default 10).",
                },
            },
            "required": [],
        },
    },
    {
        "name": "ray_job_describe",
        "description": "Get detailed information about a specific Ray job by its ID.",
        "parameters": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "The Ray job ID to describe.",
                },
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "cron_add",
        "description": (
            "Add a scheduled cron job. Supports one-shot (at), recurring (every), "
            "or cron-expression schedules. The job will run the given message as an agent task."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Human-readable name for the job.",
                },
                "message": {
                    "type": "string",
                    "description": "The prompt/task to execute when the job fires.",
                },
                "schedule_kind": {
                    "type": "string",
                    "description": "Schedule type: 'at' (one-shot ISO timestamp), 'every' (interval), or 'cron' (cron expression).",
                },
                "schedule_at": {
                    "type": "string",
                    "description": "ISO 8601 timestamp for one-shot jobs (schedule_kind='at').",
                },
                "schedule_every_seconds": {
                    "type": "integer",
                    "description": "Interval in seconds for recurring jobs (schedule_kind='every').",
                },
                "schedule_cron_expr": {
                    "type": "string",
                    "description": "5-field cron expression (schedule_kind='cron'), e.g. '0 7 * * *'.",
                },
                "schedule_timezone": {
                    "type": "string",
                    "description": "IANA timezone for cron expressions, e.g. 'America/Los_Angeles'.",
                },
                "delete_after_run": {
                    "type": "boolean",
                    "description": "Delete one-shot jobs after successful run (default true for 'at' jobs).",
                },
            },
            "required": ["name", "message", "schedule_kind"],
        },
    },
    {
        "name": "cron_list",
        "description": "List all scheduled cron jobs with their status, schedule, and run count.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "cron_remove",
        "description": "Remove a cron job by its ID.",
        "parameters": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "The job ID to remove (first 8 chars is sufficient).",
                },
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "memory_read",
        "description": (
            "Read the agent's persistent MEMORY.md file. Contains accumulated knowledge "
            "across sessions: preferences, key facts, learned patterns, projects, people. "
            "Optionally filter to a specific section."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "section": {
                    "type": "string",
                    "description": (
                        "Optional section name to read (e.g. 'Key Facts', 'Preferences'). "
                        "Empty reads the entire file."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "memory_write",
        "description": (
            "Write to a section of the agent's persistent MEMORY.md file. "
            "Use to record important facts, preferences, or patterns that should "
            "survive across sessions and compaction cycles."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "section": {
                    "type": "string",
                    "description": "Section name: 'Preferences', 'Key Facts', 'Learned Patterns', 'Projects', or 'People'.",
                },
                "content": {
                    "type": "string",
                    "description": "The content to write (markdown format, typically a bullet point).",
                },
                "mode": {
                    "type": "string",
                    "description": "Write mode: 'append' (add to section, default) or 'replace' (overwrite section).",
                },
            },
            "required": ["section", "content"],
        },
    },
]


def get_all_declarations() -> list[dict]:
    return TOOL_DECLARATIONS.copy()
