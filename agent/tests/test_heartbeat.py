"""Tests for heartbeat: active hours, HEARTBEAT_OK detection, prompt content."""

from pathlib import Path
from unittest import mock

import pytest

from agent.heartbeat import (
    DEFAULT_PROMPT,
    HEARTBEAT_TOKEN,
    ActiveHours,
    HeartbeatConfig,
    is_heartbeat_ok,
    is_within_active_hours,
    read_heartbeat_md,
    strip_heartbeat_token,
)


# ---------------------------------------------------------------------------
# is_heartbeat_ok
# ---------------------------------------------------------------------------

class TestIsHeartbeatOk:
    def test_exact_token(self):
        assert is_heartbeat_ok("HEARTBEAT_OK") is True

    def test_token_with_whitespace(self):
        assert is_heartbeat_ok("  HEARTBEAT_OK  ") is True

    def test_token_at_start_with_short_suffix(self):
        assert is_heartbeat_ok("HEARTBEAT_OK — nothing new.") is True

    def test_token_at_end_with_short_prefix(self):
        assert is_heartbeat_ok("All quiet. HEARTBEAT_OK") is True

    def test_token_with_long_content_is_not_ok(self):
        long_text = "x" * 500
        assert is_heartbeat_ok(f"HEARTBEAT_OK {long_text}") is False

    def test_no_token_is_not_ok(self):
        assert is_heartbeat_ok("There are 3 failed jobs to investigate") is False

    def test_empty_string_is_not_ok(self):
        assert is_heartbeat_ok("") is False

    def test_custom_ack_max_chars(self):
        msg = "HEARTBEAT_OK " + "x" * 50
        assert is_heartbeat_ok(msg, ack_max_chars=100) is True
        assert is_heartbeat_ok(msg, ack_max_chars=10) is False


# ---------------------------------------------------------------------------
# strip_heartbeat_token
# ---------------------------------------------------------------------------

class TestStripHeartbeatToken:
    def test_strips_token_at_start(self):
        assert strip_heartbeat_token("HEARTBEAT_OK nothing new") == "nothing new"

    def test_strips_token_at_end(self):
        assert strip_heartbeat_token("All good HEARTBEAT_OK") == "All good"

    def test_no_token_returns_original(self):
        assert strip_heartbeat_token("real status update") == "real status update"

    def test_only_token_returns_empty(self):
        assert strip_heartbeat_token("HEARTBEAT_OK") == ""


# ---------------------------------------------------------------------------
# read_heartbeat_md
# ---------------------------------------------------------------------------

class TestReadHeartbeatMd:
    def test_reads_content(self, tmp_path):
        (tmp_path / "HEARTBEAT.md").write_text("# Checklist\n- Check Ray jobs\n- Check TODOs")
        result = read_heartbeat_md(tmp_path)
        assert "Check Ray jobs" in result

    def test_missing_file_returns_empty(self, tmp_path):
        assert read_heartbeat_md(tmp_path) == ""

    def test_headers_only_returns_empty(self, tmp_path):
        (tmp_path / "HEARTBEAT.md").write_text("# Heartbeat\n## Section\n")
        assert read_heartbeat_md(tmp_path) == ""

    def test_empty_file_returns_empty(self, tmp_path):
        (tmp_path / "HEARTBEAT.md").write_text("")
        assert read_heartbeat_md(tmp_path) == ""


# ---------------------------------------------------------------------------
# is_within_active_hours
# ---------------------------------------------------------------------------

class TestIsWithinActiveHours:
    def test_no_active_hours_always_true(self):
        config = HeartbeatConfig(active_hours=None)
        assert is_within_active_hours(config) is True

    def test_within_range(self):
        config = HeartbeatConfig(active_hours=ActiveHours(start="00:00", end="23:59"))
        assert is_within_active_hours(config) is True

    def test_outside_range(self):
        with mock.patch("agent.heartbeat.datetime") as mock_dt:
            mock_dt.now.return_value = mock.MagicMock(strftime=lambda fmt: "03:00")
            config = HeartbeatConfig(
                active_hours=ActiveHours(start="08:00", end="22:00")
            )
            assert is_within_active_hours(config) is False

    def test_midnight_wrap(self):
        with mock.patch("agent.heartbeat.datetime") as mock_dt:
            mock_dt.now.return_value = mock.MagicMock(strftime=lambda fmt: "23:30")
            mock_dt.now.return_value.strftime = lambda fmt: "23:30"
            config = HeartbeatConfig(
                active_hours=ActiveHours(start="22:00", end="06:00")
            )
            assert is_within_active_hours(config) is True


# ---------------------------------------------------------------------------
# DEFAULT_PROMPT content verification
# ---------------------------------------------------------------------------

class TestDefaultPrompt:
    def test_mentions_cursor_agent(self):
        assert "cursor_agent" in DEFAULT_PROMPT

    def test_mentions_ray_jobs(self):
        assert "Ray job" in DEFAULT_PROMPT

    def test_mentions_todo(self):
        assert "TODO" in DEFAULT_PROMPT

    def test_mentions_experiment_tracker(self):
        assert "experiment" in DEFAULT_PROMPT.lower()

    def test_mentions_heartbeat_ok_fallback(self):
        assert HEARTBEAT_TOKEN in DEFAULT_PROMPT

    def test_mentions_workspace_path(self):
        assert "/Users/jing.lu/git_repos" in DEFAULT_PROMPT
