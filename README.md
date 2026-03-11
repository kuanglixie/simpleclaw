# Worklog Telegram-Gemini Bot

A personal assistant bot that runs as a Telegram daemon, powered by Google Gemini. It manages work items, monitors Ray training jobs, updates daily logs, and answers technical questions.

## Architecture

- **Telegram bot** receives messages and enqueues tasks in SQLite
- **Orchestrator** processes tasks using Gemini with native function calling
- **Heartbeat** runs periodic check-ins on a configurable interval
- **Cron scheduler** supports one-shot, recurring, and cron-expression jobs
- **Context compaction** summarizes old conversation history to prevent context overflow
- **Memory** persists important facts across sessions via MEMORY.md

## Prerequisites

- Python 3.9+
- Google Gemini API access (or Vertex AI)
- Telegram bot token from [@BotFather](https://t.me/BotFather)

## Setup

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Create config file:

```bash
mkdir -p ~/.config/worklog-agent
cp config.env.example ~/.config/worklog-agent/config.env
```

3. Edit `~/.config/worklog-agent/config.env` and fill in your Telegram bot token and user ID.

## Run

```bash
python -m agent.main
```

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/tasks` | List recent tasks |
| `/cancel` | Cancel the most recent pending task |
| `/todo` | View TODO dashboard |
| `/todo N` | View details for TODO #N |
| `/add_todo <text>` | Add a new TODO item |
| `/new` | Reset conversation session |
| `/cron` | List scheduled cron jobs |
| `/compact` | Trigger conversation compaction |
| *(any text)* | Enqueue as a task for the agent |

## Configuration

All settings are in `config.env`. Key options:

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | *(required)* | Bot token from BotFather |
| `TELEGRAM_ALLOWED_USER_ID` | `0` | Restrict to this Telegram user ID (0 = allow all) |
| `WORKLOG_DIR` | `~/worklog` | Path to your worklog directory |
| `GEMINI_MODEL` | `gemini-2.5-pro` | Gemini model name |
| `GOOGLE_GENAI_USE_VERTEXAI` | `false` | Use Vertex AI instead of free-tier |
| `HEARTBEAT_EVERY_SECONDS` | `1800` | Heartbeat interval (30 min) |
| `MAX_AGENT_STEPS` | `6` | Max tool-use steps per task |

## Data

- SQLite state: `.agent_mailbox/state.db`
- Task output: `.agent_mailbox/outbox/<task-id>.{md,json}`
- Cron jobs: `.agent_mailbox/cron/jobs.json`
