"""Tests for pydantic_agent: context injection, output instructions, cursor_agent."""

from pathlib import Path
from unittest import mock

import pytest

from agent.pydantic_agent import (
    _build_cursor_agent_prompt,
    _CURSOR_CONTEXT_MAX,
    _OUTPUT_INSTRUCTIONS,
    _read_safe,
)


# ---------------------------------------------------------------------------
# _read_safe
# ---------------------------------------------------------------------------

class TestReadSafe:
    def test_reads_existing_file(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("hello world")
        assert _read_safe(f) == "hello world"

    def test_nonexistent_file_returns_empty(self, tmp_path):
        assert _read_safe(tmp_path / "nope.md") == ""

    def test_truncates_at_max_chars(self, tmp_path):
        f = tmp_path / "big.md"
        f.write_text("x" * 500)
        result = _read_safe(f, max_chars=100)
        assert len(result) < 500
        assert result.endswith("...(truncated)")

    def test_no_truncation_when_under_limit(self, tmp_path):
        f = tmp_path / "small.md"
        f.write_text("short")
        assert _read_safe(f, max_chars=1000) == "short"

    def test_zero_max_chars_means_no_limit(self, tmp_path):
        f = tmp_path / "full.md"
        content = "x" * 10000
        f.write_text(content)
        assert _read_safe(f) == content


# ---------------------------------------------------------------------------
# _build_cursor_agent_prompt
# ---------------------------------------------------------------------------

class TestBuildCursorAgentPrompt:
    def test_includes_task_prompt(self, tmp_path):
        result = _build_cursor_agent_prompt("fix the bug", tmp_path)
        assert "## Task\nfix the bug" in result

    def test_includes_output_instructions(self, tmp_path):
        result = _build_cursor_agent_prompt("do something", tmp_path)
        assert "Output Format (CRITICAL)" in result
        assert "Telegram" in result

    def test_includes_memory_when_present(self, tmp_path):
        (tmp_path / "MEMORY.md").write_text("key fact: use v2 model")
        result = _build_cursor_agent_prompt("task", tmp_path)
        assert "Agent Memory" in result
        assert "key fact: use v2 model" in result

    def test_includes_todo_dashboard(self, tmp_path):
        todo_content = "# TODO Dashboard\n- [x] task1\n- [ ] task2\n\n## Details\nblah blah"
        (tmp_path / "TODO.md").write_text(todo_content)
        result = _build_cursor_agent_prompt("task", tmp_path)
        assert "TODO Dashboard" in result
        assert "task2" in result
        assert "blah blah" not in result  # details section excluded

    def test_includes_status_snapshot(self, tmp_path):
        (tmp_path / "STATUS_SNAPSHOT.md").write_text("## Snapshot\nAll systems go")
        result = _build_cursor_agent_prompt("task", tmp_path)
        assert "Status Snapshot" in result
        assert "All systems go" in result

    def test_no_context_still_works(self, tmp_path):
        result = _build_cursor_agent_prompt("bare task", tmp_path)
        assert "## Task\nbare task" in result
        assert "--- Context" not in result

    def test_context_block_present_when_files_exist(self, tmp_path):
        (tmp_path / "MEMORY.md").write_text("remember this")
        result = _build_cursor_agent_prompt("task", tmp_path)
        assert "--- Context (auto-injected by SimpleClaw) ---" in result
        assert "--- End Context ---" in result

    def test_total_context_truncated_when_too_large(self, tmp_path):
        (tmp_path / "MEMORY.md").write_text("m" * 5000)
        (tmp_path / "STATUS_SNAPSHOT.md").write_text("s" * 3000)
        result = _build_cursor_agent_prompt("task", tmp_path)
        context_start = result.find("--- Context")
        context_end = result.find("--- End Context ---")
        context_section = result[context_start:context_end]
        assert len(context_section) <= _CURSOR_CONTEXT_MAX + 200  # some overhead

    def test_todo_details_excluded(self, tmp_path):
        todo = (
            "# Dashboard\n"
            "- item1\n"
            "- item2\n"
            "## Details\n"
            "### item1\n"
            "Long description that should be excluded\n"
        )
        (tmp_path / "TODO.md").write_text(todo)
        result = _build_cursor_agent_prompt("task", tmp_path)
        assert "Long description that should be excluded" not in result
        assert "item1" in result


# ---------------------------------------------------------------------------
# _OUTPUT_INSTRUCTIONS content
# ---------------------------------------------------------------------------

class TestOutputInstructions:
    def test_mentions_telegram(self):
        assert "Telegram" in _OUTPUT_INSTRUCTIONS

    def test_mentions_char_limit(self):
        assert "3500" in _OUTPUT_INSTRUCTIONS or "4096" in _OUTPUT_INSTRUCTIONS

    def test_mentions_bullet_points(self):
        assert "bullet" in _OUTPUT_INSTRUCTIONS.lower()

    def test_mentions_bold_formatting(self):
        assert "Bold" in _OUTPUT_INSTRUCTIONS or "**" in _OUTPUT_INSTRUCTIONS
