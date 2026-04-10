# SimpleClaw Agent

Core Python package: Telegram bot, task queue, PydanticAI agent, and tools.

See the [root README](../README.md) for full setup, commands, and configuration.

## Run

```bash
python -m agent.main
```

## Output locations

- SQLite state: `.agent_mailbox/state.db`
- Human-readable output: `.agent_mailbox/outbox/<task-id>.md`
- Structured output: `.agent_mailbox/outbox/<task-id>.json`

## Optional: Browser extension

Set `BROWSER_SERVER_ENABLED=true` in config to enable the Chrome extension
WebSocket relay for browser automation tools.
