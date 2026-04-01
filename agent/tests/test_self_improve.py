"""Tests for self-improvement module: daily digest, evolution log, memory updates."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.self_improve import (
    SELF_IMPROVE_PROMPT,
    DailyDigest,
    TaskSignal,
    append_evolution_log,
    build_daily_digest,
    collect_day_signals,
    extract_memory_updates,
    format_digest_markdown,
    format_digest_telegram,
    _duration_seconds,
    _extract_signal,
    _find_failure_patterns,
    _count_tools,
    _generate_improvement_ideas,
    _normalize_error,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_task_json(outbox_dir: Path, task_id: str, **overrides) -> Path:
    """Create a task JSON file in the outbox."""
    data = {
        "task_id": task_id,
        "run_id": "run-1",
        "attempt": 1,
        "source": "telegram",
        "session_key": "test",
        "status": "completed",
        "started_at": "2026-03-31T10:00:00+00:00",
        "finished_at": "2026-03-31T10:01:30+00:00",
        "model": "gemini-2.5-pro",
        "final_answer": "Done.",
        "summary": "Task completed.",
        "tool_calls": [],
        "artifacts": [],
        "errors": [],
        "next_actions": [],
    }
    data.update(overrides)
    path = outbox_dir / f"{task_id}.json"
    path.write_text(json.dumps(data))
    return path


# ---------------------------------------------------------------------------
# _duration_seconds
# ---------------------------------------------------------------------------

class TestDurationSeconds:
    def test_valid_iso_timestamps(self):
        dur = _duration_seconds(
            "2026-03-31T10:00:00+00:00",
            "2026-03-31T10:02:30+00:00",
        )
        assert dur == 150.0

    def test_empty_strings(self):
        assert _duration_seconds("", "") == 0.0

    def test_invalid_format(self):
        assert _duration_seconds("not-a-date", "also-not") == 0.0

    def test_same_timestamp(self):
        ts = "2026-03-31T10:00:00+00:00"
        assert _duration_seconds(ts, ts) == 0.0


# ---------------------------------------------------------------------------
# _extract_signal
# ---------------------------------------------------------------------------

class TestExtractSignal:
    def test_basic_extraction(self):
        data = {
            "task_id": "t1",
            "source": "telegram",
            "status": "completed",
            "started_at": "2026-03-31T10:00:00+00:00",
            "finished_at": "2026-03-31T10:01:00+00:00",
            "tool_calls": [
                {"name": "run_shell", "input": "ls", "output_excerpt": "", "exit_code": 0, "duration_ms": 100},
                {"name": "read_file", "input": "/tmp/x", "output_excerpt": "", "exit_code": 0, "duration_ms": 50},
            ],
            "errors": ["TimeoutError: tool timed out"],
            "summary": "Checked files",
        }
        sig = _extract_signal(data)
        assert sig.task_id == "t1"
        assert sig.tool_names == ["run_shell", "read_file"]
        assert sig.error_count == 1
        assert sig.duration_seconds == 60.0

    def test_missing_fields_handled(self):
        sig = _extract_signal({})
        assert sig.task_id == ""
        assert sig.tool_names == []
        assert sig.error_count == 0


# ---------------------------------------------------------------------------
# _normalize_error
# ---------------------------------------------------------------------------

class TestNormalizeError:
    def test_extracts_error_type(self):
        result = _normalize_error("TimeoutError: agent tool timed out after 180s")
        assert "TimeoutError" in result
        assert "timed out" in result

    def test_long_message_truncated(self):
        msg = "Something went wrong: " + "x" * 200
        result = _normalize_error(msg)
        assert len(result) <= 80

    def test_short_message_returns_empty(self):
        assert _normalize_error("err") == ""

    def test_empty_returns_empty(self):
        assert _normalize_error("") == ""


# ---------------------------------------------------------------------------
# collect_day_signals
# ---------------------------------------------------------------------------

class TestCollectDaySignals:
    def test_collects_matching_date(self, tmp_path):
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        _write_task_json(outbox, "t1", started_at="2026-03-31T10:00:00+00:00")
        _write_task_json(outbox, "t2",
                         started_at="2026-03-30T10:00:00+00:00",
                         finished_at="2026-03-30T10:01:30+00:00")
        _write_task_json(outbox, "t3", started_at="2026-03-31T23:00:00+00:00")

        signals = collect_day_signals(outbox, "2026-03-31")
        task_ids = {s.task_id for s in signals}
        assert "t1" in task_ids
        assert "t3" in task_ids
        assert "t2" not in task_ids

    def test_empty_outbox(self, tmp_path):
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        assert collect_day_signals(outbox, "2026-03-31") == []

    def test_nonexistent_outbox(self, tmp_path):
        assert collect_day_signals(tmp_path / "nope", "2026-03-31") == []

    def test_corrupt_json_skipped(self, tmp_path):
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        (outbox / "bad.json").write_text("not valid json {{{")
        _write_task_json(outbox, "good", started_at="2026-03-31T12:00:00+00:00")

        signals = collect_day_signals(outbox, "2026-03-31")
        assert len(signals) == 1
        assert signals[0].task_id == "good"

    def test_matches_on_finished_at_too(self, tmp_path):
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        _write_task_json(
            outbox, "t1",
            started_at="2026-03-30T23:59:00+00:00",
            finished_at="2026-03-31T00:01:00+00:00",
        )
        signals = collect_day_signals(outbox, "2026-03-31")
        assert len(signals) == 1


# ---------------------------------------------------------------------------
# _find_failure_patterns
# ---------------------------------------------------------------------------

class TestFindFailurePatterns:
    def test_groups_similar_errors(self):
        signals = [
            TaskSignal("t1", "cron:x", "failed", "", "", 0, [], 1,
                        ["TimeoutError: tool timed out after 180s"], ""),
            TaskSignal("t2", "cron:y", "failed", "", "", 0, [], 1,
                        ["TimeoutError: tool timed out after 180s"], ""),
            TaskSignal("t3", "cron:z", "completed", "", "", 0, [], 0, [], ""),
        ]
        patterns = _find_failure_patterns(signals)
        assert len(patterns) == 1
        assert "(x2)" in patterns[0]

    def test_no_failures_empty(self):
        signals = [
            TaskSignal("t1", "telegram", "completed", "", "", 0, [], 0, [], ""),
        ]
        assert _find_failure_patterns(signals) == []


# ---------------------------------------------------------------------------
# _count_tools
# ---------------------------------------------------------------------------

class TestCountTools:
    def test_counts_tools(self):
        signals = [
            TaskSignal("t1", "", "", "", "", 0, ["run_shell", "read_file", "run_shell"], 0, [], ""),
            TaskSignal("t2", "", "", "", "", 0, ["run_shell", "cursor_agent"], 0, [], ""),
        ]
        counts = _count_tools(signals)
        assert counts[0] == ("run_shell", 3)

    def test_empty_signals(self):
        assert _count_tools([]) == []


# ---------------------------------------------------------------------------
# _generate_improvement_ideas
# ---------------------------------------------------------------------------

class TestGenerateImprovementIdeas:
    def test_high_failure_rate(self):
        signals = [
            TaskSignal(f"t{i}", "", "failed", "", "", 0, [], 1, ["err"], "")
            for i in range(5)
        ]
        ideas = _generate_improvement_ideas(signals, [], [])
        assert any("failure rate" in idea.lower() for idea in ideas)

    def test_slow_tasks(self):
        signals = [
            TaskSignal("t1", "heavy-task", "completed", "", "", 200, [], 0, [], ""),
        ]
        ideas = _generate_improvement_ideas(signals, [], [])
        assert any(">2 min" in idea for idea in ideas)

    def test_no_cursor_agent_hint(self):
        signals = [
            TaskSignal(f"t{i}", "", "completed", "", "", 30, [], 0, [], "")
            for i in range(10)
        ]
        ideas = _generate_improvement_ideas(signals, [], [("run_shell", 5)])
        assert any("cursor_agent" in idea for idea in ideas)

    def test_clean_day(self):
        signals = [
            TaskSignal("t1", "", "completed", "", "", 30,
                        ["cursor_agent"], 0, [], ""),
        ]
        ideas = _generate_improvement_ideas(signals, [], [("cursor_agent", 1)])
        assert any("normally" in idea.lower() for idea in ideas)

    def test_timeout_pattern_flagged(self):
        ideas = _generate_improvement_ideas(
            [], ["TimeoutError: tool timed out (x3)"], [],
        )
        assert any("timeout" in idea.lower() for idea in ideas)


# ---------------------------------------------------------------------------
# build_daily_digest
# ---------------------------------------------------------------------------

class TestBuildDailyDigest:
    def test_full_digest(self, tmp_path):
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        _write_task_json(outbox, "t1", status="completed",
                         tool_calls=[{"name": "run_shell"}])
        _write_task_json(outbox, "t2", status="failed",
                         errors=["ValueError: bad input"],
                         started_at="2026-03-31T10:00:00+00:00",
                         finished_at="2026-03-31T10:05:00+00:00")

        digest = build_daily_digest(outbox, "2026-03-31")
        assert digest.total_tasks == 2
        assert digest.completed == 1
        assert digest.failed == 1
        assert digest.date == "2026-03-31"
        assert len(digest.improvement_ideas) > 0

    def test_empty_day(self, tmp_path):
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        digest = build_daily_digest(outbox, "2026-03-31")
        assert digest.total_tasks == 0
        assert digest.completed == 0


# ---------------------------------------------------------------------------
# format_digest_markdown
# ---------------------------------------------------------------------------

class TestFormatDigestMarkdown:
    def test_contains_date_header(self):
        digest = DailyDigest(
            date="2026-03-31", total_tasks=10, completed=8, failed=2,
            avg_duration_seconds=45.0, slowest_task=None,
            failure_patterns=["TimeoutError (x2)"],
            most_used_tools=[("run_shell", 5)],
            improvement_ideas=["Add retries"],
        )
        md = format_digest_markdown(digest)
        assert "## 2026-03-31" in md
        assert "10 total" in md
        assert "Failure Patterns" in md
        assert "Improvement Ideas" in md

    def test_with_slowest_task(self):
        slow = TaskSignal("t1", "heavy-cron", "completed", "", "", 300.0, [], 0, [], "")
        digest = DailyDigest(
            date="2026-03-31", total_tasks=5, completed=5, failed=0,
            avg_duration_seconds=60.0, slowest_task=slow,
            failure_patterns=[], most_used_tools=[],
            improvement_ideas=["All good"],
        )
        md = format_digest_markdown(digest)
        assert "heavy-cron" in md
        assert "300s" in md


# ---------------------------------------------------------------------------
# format_digest_telegram
# ---------------------------------------------------------------------------

class TestFormatDigestTelegram:
    def test_compact_format(self):
        digest = DailyDigest(
            date="2026-03-31", total_tasks=10, completed=8, failed=2,
            avg_duration_seconds=45.0, slowest_task=None,
            failure_patterns=["timeout (x2)"],
            most_used_tools=[],
            improvement_ideas=["Add retries"],
        )
        text = format_digest_telegram(digest)
        assert "Daily Self-Review" in text
        assert "8/10" in text
        assert "2 failed" in text

    def test_clean_day_no_ideas_section(self):
        digest = DailyDigest(
            date="2026-03-31", total_tasks=5, completed=5, failed=0,
            avg_duration_seconds=30.0, slowest_task=None,
            failure_patterns=[], most_used_tools=[],
            improvement_ideas=["No significant issues detected. System operating normally."],
        )
        text = format_digest_telegram(digest)
        assert "Ideas" not in text


# ---------------------------------------------------------------------------
# append_evolution_log
# ---------------------------------------------------------------------------

class TestAppendEvolutionLog:
    def test_creates_new_log(self, tmp_path):
        digest = DailyDigest(
            date="2026-03-31", total_tasks=5, completed=5, failed=0,
            avg_duration_seconds=30.0, slowest_task=None,
            failure_patterns=[], most_used_tools=[],
            improvement_ideas=["All good"],
        )
        path = append_evolution_log(tmp_path, digest)
        assert path.exists()
        content = path.read_text()
        assert "Evolution Log" in content
        assert "2026-03-31" in content

    def test_appends_to_existing(self, tmp_path):
        log_path = tmp_path / "EVOLUTION_LOG.md"
        log_path.write_text("# SimpleClaw Evolution Log\n\n## 2026-03-30\nOld entry\n")

        digest = DailyDigest(
            date="2026-03-31", total_tasks=3, completed=3, failed=0,
            avg_duration_seconds=20.0, slowest_task=None,
            failure_patterns=[], most_used_tools=[],
            improvement_ideas=["Fine"],
        )
        append_evolution_log(tmp_path, digest)
        content = log_path.read_text()
        assert "2026-03-30" in content
        assert "2026-03-31" in content

    def test_skips_duplicate_date(self, tmp_path):
        log_path = tmp_path / "EVOLUTION_LOG.md"
        log_path.write_text("# Log\n\n## 2026-03-31\nAlready here\n")

        digest = DailyDigest(
            date="2026-03-31", total_tasks=1, completed=1, failed=0,
            avg_duration_seconds=10.0, slowest_task=None,
            failure_patterns=[], most_used_tools=[],
            improvement_ideas=["Duplicate"],
        )
        append_evolution_log(tmp_path, digest)
        content = log_path.read_text()
        assert content.count("2026-03-31") == 1  # not duplicated


# ---------------------------------------------------------------------------
# extract_memory_updates
# ---------------------------------------------------------------------------

class TestExtractMemoryUpdates:
    def test_high_failure_rate_generates_entry(self):
        digest = DailyDigest(
            date="2026-03-31", total_tasks=10, completed=5, failed=5,
            avg_duration_seconds=30.0, slowest_task=None,
            failure_patterns=["TimeoutError (x3)"],
            most_used_tools=[],
            improvement_ideas=["Fix timeouts"],
        )
        entries = extract_memory_updates(digest)
        assert len(entries) > 0
        assert any("failure rate" in e.lower() for e in entries)

    def test_clean_day_no_entries(self):
        digest = DailyDigest(
            date="2026-03-31", total_tasks=10, completed=10, failed=0,
            avg_duration_seconds=30.0, slowest_task=None,
            failure_patterns=[], most_used_tools=[],
            improvement_ideas=["No significant issues detected. System operating normally."],
        )
        entries = extract_memory_updates(digest)
        assert entries == []

    def test_timeout_idea_generates_entry(self):
        digest = DailyDigest(
            date="2026-03-31", total_tasks=5, completed=4, failed=1,
            avg_duration_seconds=30.0, slowest_task=None,
            failure_patterns=[],
            most_used_tools=[],
            improvement_ideas=["Recurring timeout: TimeoutError (x3). Increase timeout or simplify."],
        )
        entries = extract_memory_updates(digest)
        assert any("timeout" in e.lower() for e in entries)

    def test_high_avg_duration_generates_entry(self):
        digest = DailyDigest(
            date="2026-03-31", total_tasks=10, completed=10, failed=0,
            avg_duration_seconds=120.0, slowest_task=None,
            failure_patterns=[], most_used_tools=[],
            improvement_ideas=[],
        )
        entries = extract_memory_updates(digest)
        assert any("duration" in e.lower() for e in entries)


# ---------------------------------------------------------------------------
# SELF_IMPROVE_PROMPT content
# ---------------------------------------------------------------------------

class TestSelfImprovePrompt:
    def test_mentions_evolution_log(self):
        assert "EVOLUTION_LOG" in SELF_IMPROVE_PROMPT

    def test_mentions_memory(self):
        assert "MEMORY" in SELF_IMPROVE_PROMPT

    def test_mentions_cursor_agent(self):
        assert "cursor_agent" in SELF_IMPROVE_PROMPT

    def test_no_auto_code_changes(self):
        assert "NOT apply code changes automatically" in SELF_IMPROVE_PROMPT
