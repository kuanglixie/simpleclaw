"""Tests for direct_executor: prompt building, snoocode routing, fallback detection."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from agent.direct_executor import (
    SYSTEM_PROMPT,
    _is_snoocode_error,
    build_prompt,
    execute,
)


# ---------------------------------------------------------------------------
# _is_snoocode_error
# ---------------------------------------------------------------------------

class TestIsSnooCodeError:
    def test_session_error(self):
        assert _is_snoocode_error("Error: failed to establish MCP session with snoocode") is True

    def test_empty_response(self):
        assert _is_snoocode_error("Error: empty response from snoocode for test_tool") is True

    def test_unparseable(self):
        assert _is_snoocode_error("Error: unparseable response from snoocode: ...") is True

    def test_connection_refused(self):
        assert _is_snoocode_error("connection refused") is True

    def test_normal_response(self):
        assert _is_snoocode_error("Here are your Ray job results...") is False

    def test_normal_error_not_snoocode(self):
        assert _is_snoocode_error("Error: tool not found") is False

    def test_empty_string(self):
        assert _is_snoocode_error("") is False


# ---------------------------------------------------------------------------
# build_prompt
# ---------------------------------------------------------------------------

class TestBuildPrompt:
    def test_includes_system_prompt_with_paths(self, tmp_path):
        cron_path = tmp_path / "cron" / "jobs.json"
        result = build_prompt(
            context_blob="some context",
            task_prompt="check status",
            worklog_dir=tmp_path,
            cron_path=cron_path,
            simpleclaw_dir=tmp_path / "simpleclaw",
        )
        assert str(tmp_path) in result
        assert str(cron_path) in result
        assert "SimpleClaw" in result

    def test_includes_context_block(self, tmp_path):
        result = build_prompt(
            context_blob="MEMORY: important fact\nTODO: item 1",
            task_prompt="do something",
            worklog_dir=tmp_path,
            cron_path=tmp_path / "jobs.json",
            simpleclaw_dir=tmp_path,
        )
        assert "--- Context (auto-injected by SimpleClaw) ---" in result
        assert "important fact" in result
        assert "--- End Context ---" in result

    def test_empty_context_omits_block(self, tmp_path):
        result = build_prompt(
            context_blob="",
            task_prompt="hello",
            worklog_dir=tmp_path,
            cron_path=tmp_path / "jobs.json",
            simpleclaw_dir=tmp_path,
        )
        assert "--- Context" not in result

    def test_includes_task(self, tmp_path):
        result = build_prompt(
            context_blob="ctx",
            task_prompt="fix the bug in utils.py",
            worklog_dir=tmp_path,
            cron_path=tmp_path / "jobs.json",
            simpleclaw_dir=tmp_path,
        )
        assert "## Task\nfix the bug in utils.py" in result

    def test_includes_cron_schema(self, tmp_path):
        result = build_prompt(
            context_blob="",
            task_prompt="task",
            worklog_dir=tmp_path,
            cron_path=tmp_path / "jobs.json",
            simpleclaw_dir=tmp_path,
        )
        assert "job_id" in result
        assert "every_seconds" in result
        assert "delete_after_run" in result

    def test_includes_output_format(self, tmp_path):
        result = build_prompt(
            context_blob="",
            task_prompt="task",
            worklog_dir=tmp_path,
            cron_path=tmp_path / "jobs.json",
            simpleclaw_dir=tmp_path,
        )
        assert "Telegram" in result
        assert "3500" in result

    def test_includes_ray_commands(self, tmp_path):
        result = build_prompt(
            context_blob="",
            task_prompt="task",
            worklog_dir=tmp_path,
            cron_path=tmp_path / "jobs.json",
            simpleclaw_dir=tmp_path,
        )
        assert "gazette ray" in result

    def test_includes_memory_instructions(self, tmp_path):
        result = build_prompt(
            context_blob="",
            task_prompt="task",
            worklog_dir=tmp_path,
            cron_path=tmp_path / "jobs.json",
            simpleclaw_dir=tmp_path,
        )
        assert "MEMORY.md" in result
        assert "Learned Patterns" in result


# ---------------------------------------------------------------------------
# execute — snoocode success
# ---------------------------------------------------------------------------

class TestExecuteSuccess:
    @mock.patch("agent.direct_executor.snoocode.run_agent", return_value="Job completed successfully.")
    def test_returns_agent_response(self, mock_run, tmp_path):
        result = execute(
            context_blob="ctx",
            task_prompt="check jobs",
            worklog_dir=tmp_path,
            cron_path=tmp_path / "jobs.json",
            simpleclaw_dir=tmp_path,
        )
        assert result == "Job completed successfully."
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert "check jobs" in call_args[0][0]

    @mock.patch("agent.direct_executor.snoocode.run_agent", return_value="done")
    def test_uses_worklog_as_default_workspace(self, mock_run, tmp_path):
        execute(
            context_blob="",
            task_prompt="task",
            worklog_dir=tmp_path,
            cron_path=tmp_path / "jobs.json",
            simpleclaw_dir=tmp_path,
        )
        _, kwargs = mock_run.call_args
        assert kwargs.get("workspace") == str(tmp_path)

    @mock.patch("agent.direct_executor.snoocode.run_agent", return_value="done")
    def test_custom_workspace(self, mock_run, tmp_path):
        execute(
            context_blob="",
            task_prompt="task",
            worklog_dir=tmp_path,
            cron_path=tmp_path / "jobs.json",
            simpleclaw_dir=tmp_path,
            workspace="/custom/repo",
        )
        _, kwargs = mock_run.call_args
        assert kwargs.get("workspace") == "/custom/repo"

    @mock.patch("agent.direct_executor.snoocode.run_agent", return_value="done")
    def test_passes_model(self, mock_run, tmp_path):
        execute(
            context_blob="",
            task_prompt="task",
            worklog_dir=tmp_path,
            cron_path=tmp_path / "jobs.json",
            simpleclaw_dir=tmp_path,
            model="fast",
        )
        _, kwargs = mock_run.call_args
        assert kwargs.get("model") == "fast"


# ---------------------------------------------------------------------------
# execute — snoocode unreachable
# ---------------------------------------------------------------------------

class TestExecuteFallback:
    @mock.patch(
        "agent.direct_executor.snoocode.run_agent",
        return_value="Error: failed to establish MCP session with snoocode",
    )
    def test_returns_unreachable_marker(self, mock_run, tmp_path):
        result = execute(
            context_blob="",
            task_prompt="task",
            worklog_dir=tmp_path,
            cron_path=tmp_path / "jobs.json",
            simpleclaw_dir=tmp_path,
        )
        assert result.startswith("SNOOCODE_UNREACHABLE:")

    @mock.patch(
        "agent.direct_executor.snoocode.run_agent",
        return_value="Error: empty response from snoocode for run_agent",
    )
    def test_empty_response_triggers_fallback(self, mock_run, tmp_path):
        result = execute(
            context_blob="",
            task_prompt="task",
            worklog_dir=tmp_path,
            cron_path=tmp_path / "jobs.json",
            simpleclaw_dir=tmp_path,
        )
        assert result.startswith("SNOOCODE_UNREACHABLE:")

    @mock.patch(
        "agent.direct_executor.snoocode.run_agent",
        return_value="Error: tool not found",
    )
    def test_tool_error_not_treated_as_unreachable(self, mock_run, tmp_path):
        result = execute(
            context_blob="",
            task_prompt="task",
            worklog_dir=tmp_path,
            cron_path=tmp_path / "jobs.json",
            simpleclaw_dir=tmp_path,
        )
        assert not result.startswith("SNOOCODE_UNREACHABLE:")
        assert "Error: tool not found" in result


# ---------------------------------------------------------------------------
# SYSTEM_PROMPT content verification
# ---------------------------------------------------------------------------

class TestSystemPrompt:
    def test_mentions_cron_json(self):
        assert "jobs.json" in SYSTEM_PROMPT or "cron_path" in SYSTEM_PROMPT

    def test_mentions_memory(self):
        assert "MEMORY.md" in SYSTEM_PROMPT

    def test_mentions_telegram_output(self):
        assert "Telegram" in SYSTEM_PROMPT

    def test_has_format_placeholders(self):
        assert "{worklog_dir}" in SYSTEM_PROMPT
        assert "{cron_path}" in SYSTEM_PROMPT

    def test_mentions_action_rules(self):
        assert "yes" in SYSTEM_PROMPT.lower()
        assert "do it" in SYSTEM_PROMPT.lower()
