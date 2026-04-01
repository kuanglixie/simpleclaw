"""Tests for orchestrator fixes: chat_id on heartbeat/cron, deferred mark_run."""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import pytest

from agent.config import Settings
from agent.cron import CronJob, CronSchedule, CronStore
from agent.models import Task, TaskStatus, new_task_id


# ---------------------------------------------------------------------------
# Helpers — build minimal Settings without touching real config files
# ---------------------------------------------------------------------------

def _make_settings(**overrides) -> Settings:
    defaults = dict(
        worklog_dir=Path("/tmp/test-worklog"),
        mailbox_dir=Path("/tmp/test-worklog/.agent_mailbox"),
        pending_dir=Path("/tmp/test-worklog/.agent_mailbox/pending"),
        outbox_dir=Path("/tmp/test-worklog/.agent_mailbox/outbox"),
        outbox_events_dir=Path("/tmp/test-worklog/.agent_mailbox/outbox/events"),
        archive_dir=Path("/tmp/test-worklog/.agent_mailbox/archive"),
        data_dir=Path("/tmp/test-worklog/.agent_mailbox/data"),
        cron_dir=Path("/tmp/test-worklog/.agent_mailbox/cron"),
        db_path=Path("/tmp/test-worklog/.agent_mailbox/state.db"),
        gemini_model="gemini-2.5-pro",
        gemini_timeout_seconds=300,
        gemini_use_vertexai=False,
        gemini_vertex_project="",
        gemini_vertex_location="us-central1",
        pydantic_ai_model="google-gla:gemini-2.5-pro",
        max_agent_steps=20,
        tool_timeout_seconds=180,
        monitor_interval_seconds=1800,
        queue_poll_interval_seconds=2.0,
        telegram_bot_token="fake-token",
        telegram_allowed_user_id=123456789,
        heartbeat_enabled=True,
        heartbeat_every_seconds=1800,
        heartbeat_active_hours_start="00:00",
        heartbeat_active_hours_end="23:59",
        heartbeat_active_hours_tz="",
        heartbeat_light_context=True,
        cron_enabled=True,
        compaction_enabled=False,
        compaction_max_session_messages=40,
        compaction_keep_recent=12,
        compaction_max_context_chars=80000,
        browser_server_enabled=False,
        browser_server_host="127.0.0.1",
        browser_server_port=18790,
        browser_server_token="",
    )
    defaults.update(overrides)
    return Settings(**defaults)


# ---------------------------------------------------------------------------
# _user_chat_id
# ---------------------------------------------------------------------------

class TestUserChatId:
    def test_derives_chat_id_from_allowed_user_id(self):
        from agent.orchestrator import Orchestrator
        settings = _make_settings(telegram_allowed_user_id=123456789)
        orch = Orchestrator(
            settings=settings,
            queue=mock.MagicMock(),
            agent=mock.MagicMock(),
            context_assembler=mock.MagicMock(),
            notifier=mock.MagicMock(),
        )
        assert orch._user_chat_id() == "123456789"

    def test_returns_none_when_user_id_zero(self):
        from agent.orchestrator import Orchestrator
        settings = _make_settings(telegram_allowed_user_id=0)
        orch = Orchestrator(
            settings=settings,
            queue=mock.MagicMock(),
            agent=mock.MagicMock(),
            context_assembler=mock.MagicMock(),
            notifier=mock.MagicMock(),
        )
        assert orch._user_chat_id() is None

    def test_returns_none_when_user_id_negative(self):
        from agent.orchestrator import Orchestrator
        settings = _make_settings(telegram_allowed_user_id=-1)
        orch = Orchestrator(
            settings=settings,
            queue=mock.MagicMock(),
            agent=mock.MagicMock(),
            context_assembler=mock.MagicMock(),
            notifier=mock.MagicMock(),
        )
        assert orch._user_chat_id() is None


# ---------------------------------------------------------------------------
# Pending cron task tracking
# ---------------------------------------------------------------------------

class TestPendingCronTasks:
    def test_pending_cron_tasks_dict_initialized(self):
        from agent.orchestrator import Orchestrator
        settings = _make_settings()
        orch = Orchestrator(
            settings=settings,
            queue=mock.MagicMock(),
            agent=mock.MagicMock(),
            context_assembler=mock.MagicMock(),
            notifier=mock.MagicMock(),
        )
        assert orch._pending_cron_tasks == {}

    def test_cron_task_tracking_lifecycle(self):
        """Simulate: cron enqueues task -> task completes -> mark_run called."""
        from agent.orchestrator import Orchestrator
        settings = _make_settings()
        orch = Orchestrator(
            settings=settings,
            queue=mock.MagicMock(),
            agent=mock.MagicMock(),
            context_assembler=mock.MagicMock(),
            notifier=mock.MagicMock(),
        )

        task_id = "task-abc"
        cron_job_id = "cron-xyz"
        orch._pending_cron_tasks[task_id] = cron_job_id
        assert task_id in orch._pending_cron_tasks

        popped = orch._pending_cron_tasks.pop(task_id, None)
        assert popped == cron_job_id
        assert task_id not in orch._pending_cron_tasks

    def test_pop_missing_task_returns_none(self):
        from agent.orchestrator import Orchestrator
        settings = _make_settings()
        orch = Orchestrator(
            settings=settings,
            queue=mock.MagicMock(),
            agent=mock.MagicMock(),
            context_assembler=mock.MagicMock(),
            notifier=mock.MagicMock(),
        )
        assert orch._pending_cron_tasks.pop("nonexistent", None) is None


# ---------------------------------------------------------------------------
# CronStore.reload — picks up external file edits
# ---------------------------------------------------------------------------

class TestCronStoreReload:
    def test_reload_picks_up_new_jobs(self, tmp_path):
        store = CronStore(tmp_path / "jobs.json")
        job = CronJob(
            job_id="",
            name="original",
            schedule=CronSchedule(kind="every", every_seconds=3600),
            message="check",
        )
        store.add(job)
        assert len(store.list_all()) == 1

        import json
        data = json.loads((tmp_path / "jobs.json").read_text())
        data["jobs"].append({
            "job_id": "external-edit-123",
            "name": "added-externally",
            "schedule": {"kind": "every", "every_seconds": 1800},
            "message": "external job",
            "enabled": True,
            "delete_after_run": False,
            "created_at": "2026-04-01T00:00:00+00:00",
            "last_run_at": "",
            "next_run_at": "2026-04-01T01:00:00+00:00",
            "run_count": 0,
            "consecutive_failures": 0,
        })
        (tmp_path / "jobs.json").write_text(json.dumps(data, indent=2))

        assert len(store.list_all()) == 1  # still cached
        store.reload()
        assert len(store.list_all()) == 2
        assert store.get("external-edit-123") is not None
        assert store.get("external-edit-123").name == "added-externally"

    def test_reload_picks_up_removed_jobs(self, tmp_path):
        store = CronStore(tmp_path / "jobs.json")
        job = CronJob(
            job_id="",
            name="will-be-removed",
            schedule=CronSchedule(kind="every", every_seconds=3600),
            message="bye",
        )
        created = store.add(job)
        assert len(store.list_all()) == 1

        (tmp_path / "jobs.json").write_text('{"jobs": []}')
        store.reload()
        assert len(store.list_all()) == 0

    def test_reload_survives_corrupt_file(self, tmp_path):
        store = CronStore(tmp_path / "jobs.json")
        store.add(CronJob(
            job_id="", name="ok",
            schedule=CronSchedule(kind="every", every_seconds=3600),
            message="test",
        ))

        (tmp_path / "jobs.json").write_text("not valid json {{{")
        store.reload()
        assert len(store.list_all()) == 0  # cleared, but no crash

    def test_reload_on_missing_file(self, tmp_path):
        store = CronStore(tmp_path / "jobs.json")
        store.add(CronJob(
            job_id="", name="x",
            schedule=CronSchedule(kind="every", every_seconds=60),
            message="m",
        ))
        (tmp_path / "jobs.json").unlink()
        store.reload()
        assert len(store.list_all()) == 0


# ---------------------------------------------------------------------------
# CronStore.mark_run behavior for one-shot jobs
# ---------------------------------------------------------------------------

class TestCronMarkRun:
    def test_success_deletes_one_shot_job(self, tmp_path):
        store = CronStore(tmp_path / "jobs.json")
        job = CronJob(
            job_id="",
            name="remind-me",
            schedule=CronSchedule(kind="at", at="2026-04-01T10:00:00Z"),
            message="check results",
            delete_after_run=True,
        )
        created = store.add(job)
        assert store.get(created.job_id) is not None

        store.mark_run(created.job_id, success=True)
        assert store.get(created.job_id) is None

    def test_failure_keeps_one_shot_job(self, tmp_path):
        store = CronStore(tmp_path / "jobs.json")
        job = CronJob(
            job_id="",
            name="remind-me",
            schedule=CronSchedule(kind="at", at="2026-04-01T10:00:00Z"),
            message="check results",
            delete_after_run=True,
        )
        created = store.add(job)

        store.mark_run(created.job_id, success=False)
        kept = store.get(created.job_id)
        assert kept is not None
        assert kept.consecutive_failures == 1
        assert kept.run_count == 1

    def test_recurring_job_not_deleted_on_success(self, tmp_path):
        store = CronStore(tmp_path / "jobs.json")
        job = CronJob(
            job_id="",
            name="hourly-check",
            schedule=CronSchedule(kind="every", every_seconds=3600),
            message="status check",
        )
        created = store.add(job)

        store.mark_run(created.job_id, success=True)
        assert store.get(created.job_id) is not None
        assert store.get(created.job_id).run_count == 1
