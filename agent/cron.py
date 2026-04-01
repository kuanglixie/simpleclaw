"""Cron job scheduler inspired by OpenClaw.

Persistent job store (JSON) with support for:
- One-shot (at): run once at a specific ISO timestamp
- Recurring (every): fixed interval in seconds
- Cron expression: standard 5-field cron expressions

Jobs are stored at .agent_mailbox/cron/jobs.json and survive restarts.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4


@dataclass
class CronSchedule:
    kind: str  # "at", "every", "cron"
    at: str = ""  # ISO 8601 for one-shot
    every_seconds: int = 0  # interval for recurring
    cron_expr: str = ""  # 5-field cron for cron-based
    timezone: str = ""  # IANA timezone for cron


@dataclass
class CronJob:
    job_id: str
    name: str
    schedule: CronSchedule
    message: str  # prompt to send to the agent
    enabled: bool = True
    delete_after_run: bool = False
    created_at: str = ""
    last_run_at: str = ""
    next_run_at: str = ""
    run_count: int = 0
    consecutive_failures: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @staticmethod
    def from_dict(d: dict) -> CronJob:
        sched = d.get("schedule", {})
        return CronJob(
            job_id=d.get("job_id", str(uuid4())),
            name=d.get("name", ""),
            schedule=CronSchedule(
                kind=sched.get("kind", "at"),
                at=sched.get("at", ""),
                every_seconds=sched.get("every_seconds", 0),
                cron_expr=sched.get("cron_expr", ""),
                timezone=sched.get("timezone", ""),
            ),
            message=d.get("message", ""),
            enabled=d.get("enabled", True),
            delete_after_run=d.get("delete_after_run", False),
            created_at=d.get("created_at", ""),
            last_run_at=d.get("last_run_at", ""),
            next_run_at=d.get("next_run_at", ""),
            run_count=d.get("run_count", 0),
            consecutive_failures=d.get("consecutive_failures", 0),
        )


class CronStore:
    """Persistent cron job store backed by a JSON file."""

    def __init__(self, store_path: Path) -> None:
        self.store_path = store_path
        self._jobs: dict[str, CronJob] = {}
        self._load()

    def _load(self) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        if self.store_path.exists():
            try:
                data = json.loads(self.store_path.read_text())
                for item in data.get("jobs", []):
                    job = CronJob.from_dict(item)
                    self._jobs[job.job_id] = job
            except (json.JSONDecodeError, KeyError):
                self._jobs = {}

    def reload(self) -> None:
        """Re-read jobs.json from disk, picking up external edits (e.g. by cursor_agent)."""
        self._jobs.clear()
        self._load()

    def _save(self) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"jobs": [j.to_dict() for j in self._jobs.values()]}
        self.store_path.write_text(json.dumps(data, indent=2))

    def add(self, job: CronJob) -> CronJob:
        if not job.job_id:
            job.job_id = str(uuid4())
        if not job.created_at:
            job.created_at = _now_iso()
        job.next_run_at = _compute_next_run(job)
        self._jobs[job.job_id] = job
        self._save()
        return job

    def remove(self, job_id: str) -> bool:
        if job_id in self._jobs:
            del self._jobs[job_id]
            self._save()
            return True
        return False

    def get(self, job_id: str) -> Optional[CronJob]:
        return self._jobs.get(job_id)

    def list_all(self) -> list[CronJob]:
        return list(self._jobs.values())

    def list_enabled(self) -> list[CronJob]:
        return [j for j in self._jobs.values() if j.enabled]

    def update(self, job_id: str, **kwargs) -> Optional[CronJob]:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        for key, value in kwargs.items():
            if hasattr(job, key):
                setattr(job, key, value)
        self._save()
        return job

    def mark_run(self, job_id: str, success: bool) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        job.last_run_at = _now_iso()
        job.run_count += 1
        if success:
            job.consecutive_failures = 0
        else:
            job.consecutive_failures += 1

        if job.schedule.kind == "at" and success:
            if job.delete_after_run:
                del self._jobs[job_id]
            else:
                job.enabled = False
        else:
            job.next_run_at = _compute_next_run(job)

        self._save()


def get_due_jobs(store: CronStore) -> list[CronJob]:
    """Return jobs that are due to run now."""
    now = datetime.now(tz=timezone.utc)
    due = []
    for job in store.list_enabled():
        if not job.next_run_at:
            continue
        try:
            next_run = datetime.fromisoformat(job.next_run_at)
            if next_run.tzinfo is None:
                next_run = next_run.replace(tzinfo=timezone.utc)
            if now >= next_run:
                due.append(job)
        except ValueError:
            continue
    return due


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat()


def _compute_next_run(job: CronJob) -> str:
    now = datetime.now(tz=timezone.utc)

    if job.schedule.kind == "at":
        return job.schedule.at

    if job.schedule.kind == "every":
        if job.last_run_at:
            try:
                last = datetime.fromisoformat(job.last_run_at)
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                from datetime import timedelta
                nxt = last + timedelta(seconds=job.schedule.every_seconds)
                return nxt.isoformat()
            except ValueError:
                pass
        from datetime import timedelta
        return (now + timedelta(seconds=job.schedule.every_seconds)).isoformat()

    if job.schedule.kind == "cron":
        return _next_cron_time(job.schedule.cron_expr, job.schedule.timezone)

    return ""


def _next_cron_time(expr: str, tz_name: str = "") -> str:
    """Compute next run time from a 5-field cron expression.

    Uses a simple matcher for common patterns. For full cron support,
    install croniter: pip install croniter
    """
    try:
        from croniter import croniter
        base = datetime.now(tz=timezone.utc)
        if tz_name:
            try:
                import zoneinfo
                base = datetime.now(tz=zoneinfo.ZoneInfo(tz_name))
            except Exception:
                pass
        cron = croniter(expr, base)
        nxt = cron.get_next(datetime)
        if nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=timezone.utc)
        return nxt.isoformat()
    except ImportError:
        # Fallback: simple pattern matching for common cron patterns
        return _simple_cron_next(expr, tz_name)


def _simple_cron_next(expr: str, tz_name: str = "") -> str:
    """Basic fallback for cron expressions without croniter."""
    from datetime import timedelta
    now = datetime.now(tz=timezone.utc)
    fields = expr.strip().split()
    if len(fields) < 5:
        return (now + timedelta(hours=1)).isoformat()

    minute, hour = fields[0], fields[1]

    if minute == "0" and hour == "*":
        # Every hour at :00
        nxt = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        return nxt.isoformat()

    if minute.isdigit() and hour.isdigit():
        # Specific time daily
        target = now.replace(
            hour=int(hour), minute=int(minute), second=0, microsecond=0,
        )
        if target <= now:
            target += timedelta(days=1)
        return target.isoformat()

    # Default: run in 1 hour
    return (now + timedelta(hours=1)).isoformat()


def format_job_list(jobs: list[CronJob]) -> str:
    if not jobs:
        return "No cron jobs."
    lines = []
    for j in jobs:
        status = "enabled" if j.enabled else "disabled"
        sched = _format_schedule(j.schedule)
        lines.append(
            f"  {j.job_id[:8]} | {j.name} | {sched} | {status} | "
            f"runs={j.run_count}"
        )
    return f"Cron jobs ({len(jobs)}):\n" + "\n".join(lines)


def _format_schedule(s: CronSchedule) -> str:
    if s.kind == "at":
        return f"at {s.at}"
    if s.kind == "every":
        if s.every_seconds >= 3600:
            return f"every {s.every_seconds // 3600}h"
        return f"every {s.every_seconds // 60}m"
    if s.kind == "cron":
        return f"cron({s.cron_expr})"
    return s.kind
