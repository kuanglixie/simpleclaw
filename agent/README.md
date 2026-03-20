# Personal Agent Daemon

Telegram + PydanticAI personal assistant with tool-calling.

## Architecture

- Telegram messages enqueue tasks in SQLite.
- A periodic heartbeat scheduler enqueues monitor tasks.
- PydanticAI `Agent.run()` handles the full agentic loop with automatic tool dispatch.
- Results are written to `.agent_mailbox/outbox/<task-id>.md` and `.json`.
- Telegram receives completion/failure notifications.

## Prerequisites

- Python 3.10+
- Telegram bot token from BotFather

## Setup

1. Install dependencies:

```bash
python3 -m pip install -r requirements.txt
```

2. Create config file:

```bash
mkdir -p ~/.config/worklog-agent
cp config.env.example ~/.config/worklog-agent/config.env
```

3. Edit `~/.config/worklog-agent/config.env` and fill in Telegram values.

## Run

```bash
python3 main.py
```

## Telegram usage

- Send any text message to enqueue a task.
- `/status` to list active tasks.
- `/history` to view recent completed/failed tasks.
- `/cancel <task_id>` to cancel a pending task.

## Output locations

- SQLite state: `.agent_mailbox/state.db`
- Human-readable output: `.agent_mailbox/outbox/<task-id>.md`
- Structured output: `.agent_mailbox/outbox/<task-id>.json`

## Optional: Browser extension

Set `BROWSER_SERVER_ENABLED=true` in config to enable the Chrome extension
WebSocket relay for browser automation tools.
