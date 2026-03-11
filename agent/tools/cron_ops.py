"""Cron job management tools."""

from __future__ import annotations

from typing import Optional

from ..cron import CronJob, CronSchedule, CronStore, format_job_list


# Module-level reference, set by the executor at init time
_cron_store: Optional[CronStore] = None


def set_cron_store(store: CronStore) -> None:
    global _cron_store
    _cron_store = store


def cron_add(
    name: str,
    message: str,
    schedule_kind: str,
    schedule_at: str = "",
    schedule_every_seconds: int = 0,
    schedule_cron_expr: str = "",
    schedule_timezone: str = "",
    delete_after_run: bool = True,
) -> str:
    if _cron_store is None:
        return "Error: cron not enabled."

    schedule = CronSchedule(
        kind=schedule_kind,
        at=schedule_at,
        every_seconds=schedule_every_seconds,
        cron_expr=schedule_cron_expr,
        timezone=schedule_timezone,
    )

    if schedule_kind == "at" and not schedule_at:
        return "Error: schedule_at is required for 'at' jobs."
    if schedule_kind == "every" and schedule_every_seconds <= 0:
        return "Error: schedule_every_seconds must be > 0 for 'every' jobs."
    if schedule_kind == "cron" and not schedule_cron_expr:
        return "Error: schedule_cron_expr is required for 'cron' jobs."

    job = CronJob(
        job_id="",
        name=name,
        schedule=schedule,
        message=message,
        delete_after_run=delete_after_run if schedule_kind == "at" else False,
    )
    created = _cron_store.add(job)
    return (
        f"Cron job created: {created.job_id[:8]} ({name})\n"
        f"Next run: {created.next_run_at}"
    )


def cron_list() -> str:
    if _cron_store is None:
        return "Error: cron not enabled."
    jobs = _cron_store.list_all()
    return format_job_list(jobs)


def cron_remove(job_id: str) -> str:
    if _cron_store is None:
        return "Error: cron not enabled."

    # Support partial ID matching
    full_id = _resolve_job_id(job_id)
    if full_id is None:
        return f"Error: no job found matching '{job_id}'."

    if _cron_store.remove(full_id):
        return f"Removed cron job: {full_id[:8]}"
    return f"Error: failed to remove job '{job_id}'."


def _resolve_job_id(partial: str) -> Optional[str]:
    if _cron_store is None:
        return None
    for job in _cron_store.list_all():
        if job.job_id == partial or job.job_id.startswith(partial):
            return job.job_id
    return None
